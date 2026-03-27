"""Message source abstraction — pluggable adapters for receiving WeCom messages.

Two implementations:
- CallbackSource: WeCom pushes messages to our HTTP endpoint (requires public URL)
- RelayPollingSource: We poll a relay service that receives WeCom callbacks on our behalf
"""

from __future__ import annotations

import abc
import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from remote_control.config import WeComConfig
    from remote_control.core.store import Store
    from remote_control.wecom.gateway import MessageHandler

logger = logging.getLogger(__name__)


class MessageSource(abc.ABC):
    """Abstract base for receiving messages from WeCom."""

    @abc.abstractmethod
    async def start(self) -> None:
        """Start receiving messages (e.g., begin polling loop)."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop receiving messages and clean up resources."""

    @abc.abstractmethod
    def register_routes(self, app: web.Application) -> None:
        """Register any HTTP routes needed by this source."""


class CallbackSource(MessageSource):
    """Receives messages via WeCom callback (push-based).

    Requires a public URL (e.g., via ngrok).
    This wraps the existing WeComGateway.
    """

    def __init__(self, config: WeComConfig, on_message: MessageHandler):
        from remote_control.wecom.gateway import WeComGateway

        self._gateway = WeComGateway(config, on_message)
        self._agent_id = config.agent_id

    async def start(self) -> None:
        logger.info("CallbackSource started — waiting for WeCom callbacks (agent_id=%d)", self._agent_id)

    async def stop(self) -> None:
        logger.info("CallbackSource stopped (agent_id=%d)", self._agent_id)

    def register_routes(self, app: web.Application) -> None:
        path = f"/wecom/callback/{self._agent_id}"
        app.router.add_get(path, self._gateway.handle_verify)
        app.router.add_post(path, self._gateway.handle_message)


class RelayPollingSource(MessageSource):
    """Receives messages by polling a relay service.

    The relay service (e.g., AWS Lambda) receives raw WeCom callbacks
    and stores them as-is (encrypted XML + query params). This source
    polls the relay, decrypts messages locally, and dispatches them.
    No public URL needed on the local machine.

    Relay API contract:
        POST <relay_url>/messages/fetch
        Request:  {"cursor": "<last_cursor>", "limit": 100}
        Response: {
            "messages": [
                {
                    "msg_id": "...",
                    "seq": 42,
                    "query_params": {"msg_signature": "...", "timestamp": "...", "nonce": "..."},
                    "body": "<xml><Encrypt>...</Encrypt></xml>"
                },
                ...
            ],
            "next_cursor": "<cursor_for_next_poll>"
        }
    """

    def __init__(
        self,
        config: WeComConfig,
        relay_url: str,
        on_message: MessageHandler,
        store: Store,
        poll_interval: float = 5.0,
    ):
        from remote_control.wecom.gateway import IncomingMessage

        self._config = config
        self._relay_url = relay_url.rstrip("/")
        self._on_message = on_message
        self._store = store
        self._poll_interval = poll_interval
        self._cursor_key = f"relay_cursor_{config.agent_id}"
        self._cursor: str = store.get_kv(self._cursor_key, "")
        self._task: asyncio.Task | None = None
        self._IncomingMessage = IncomingMessage

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "RelayPollingSource started — polling %s every %.1fs",
            self._relay_url, self._poll_interval,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("RelayPollingSource stopped")

    def register_routes(self, app: web.Application) -> None:
        app.router.add_get(f"/relay/status/{self._config.agent_id}", self._status_handler)

    async def _poll_loop(self) -> None:
        """Continuously poll the relay for new messages."""
        import httpx

        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except (httpx.ConnectError, httpx.TimeoutException, OSError):
                logger.debug("Network unavailable, will retry next cycle")
            except Exception:
                logger.exception("Error during relay poll cycle")
            await asyncio.sleep(self._poll_interval)

    async def _poll_once(self) -> None:
        """Fetch new messages from the relay service."""
        url = f"{self._relay_url}/messages/fetch"
        payload: dict = {"cursor": self._cursor, "limit": 100}

        data = await self._fetch_messages(url, payload)

        next_cursor = data.get("next_cursor", "")
        if next_cursor:
            self._cursor = next_cursor
            self._store.set_kv(self._cursor_key, next_cursor)

        messages = data.get("messages", [])
        for msg_data in messages:
            await self._dispatch_message(msg_data)

    async def _fetch_messages(self, url: str, payload: dict) -> dict:
        """HTTP POST to the relay. Separated for testability."""
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        return resp.json()

    async def _dispatch_message(self, msg_data: dict) -> None:
        """Decrypt raw WeCom message from relay and dispatch."""
        body = msg_data.get("body", "")
        query_params = msg_data.get("query_params", {})
        if not body:
            return

        from remote_control.wecom.crypto import (
            decrypt_message,
            parse_message_xml,
            verify_signature,
        )

        # Extract encrypted content from outer XML
        outer_xml = parse_message_xml(body)

        # Filter by AgentID — skip messages for other agents
        msg_agent_id = outer_xml.get("AgentID", "")
        if msg_agent_id and str(msg_agent_id) != str(self._config.agent_id):
            return  # silently skip — belongs to a different agent

        encrypt = outer_xml.get("Encrypt", "")
        if not encrypt:
            logger.warning("Relay message missing Encrypt field: %s", msg_data.get("msg_id", ""))
            return

        # Verify signature
        if not verify_signature(
            self._config.token,
            query_params.get("timestamp", ""),
            query_params.get("nonce", ""),
            encrypt,
            query_params.get("msg_signature", ""),
        ):
            logger.warning("Invalid signature for relay message %s", msg_data.get("msg_id", ""))
            return

        # Decrypt
        try:
            decrypted = decrypt_message(self._config.encoding_aes_key, encrypt)
        except Exception:
            logger.exception("Failed to decrypt relay message %s", msg_data.get("msg_id", ""))
            return

        # Parse inner XML
        inner_xml = parse_message_xml(decrypted.content)
        msg_type = inner_xml.get("MsgType", "")

        from remote_control.wecom.gateway import _SUPPORTED_MSG_TYPES, _parse_incoming_message

        if msg_type not in _SUPPORTED_MSG_TYPES:
            logger.debug("Ignoring unsupported relay message type: %s", msg_type)
            return

        wecom_msg_id = inner_xml.get("MsgId", msg_data.get("msg_id", ""))
        # Use shared parser for consistent handling of all message types
        msg = _parse_incoming_message(inner_xml, msg_type)
        msg.msg_id = wecom_msg_id

        # For text messages, skip empty content
        if msg_type == "text" and not msg.content:
            return

        logger.info("Relay %s message from %s: %s", msg_type, msg.user_id, msg.content[:50])

        try:
            await self._on_message(msg)
        except Exception:
            logger.exception("Error handling relay message %s", msg.msg_id)

    async def _status_handler(self, request: web.Request) -> web.Response:
        return web.json_response({
            "source": "relay",
            "relay_url": self._relay_url,
            "cursor": self._cursor,
            "poll_interval": self._poll_interval,
        })

"""Message source abstraction — pluggable adapters for receiving WeCom messages.

Currently a single implementation:
- CallbackSource: WeCom pushes messages to our HTTP endpoint. Requires a public
  URL — typically provided by the existing rc-dashboard-tunnel reverse SSH
  tunnel (EC2:80 → desktop:8080). See docs/architecture-decisions/0002.
"""

from __future__ import annotations

import abc
import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from remote_control.config import WeComConfig
    from remote_control.wecom.gateway import MessageHandler

logger = logging.getLogger(__name__)


class MessageSource(abc.ABC):
    """Abstract base for receiving messages from WeCom."""

    @abc.abstractmethod
    async def start(self) -> None:
        """Start receiving messages."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop receiving messages and clean up resources."""

    @abc.abstractmethod
    def register_routes(self, app: web.Application) -> None:
        """Register any HTTP routes needed by this source."""


class CallbackSource(MessageSource):
    """Receives messages via WeCom callback (push-based).

    WeCom POSTs to /wecom/callback/{agent_id}. The wrapped WeComGateway
    verifies the WeCom signature + 5-min timestamp freshness, decrypts, and
    dispatches to the on_message handler.
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

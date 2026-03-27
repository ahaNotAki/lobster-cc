"""WeCom callback HTTP handler — receives and dispatches messages."""

import logging
from dataclasses import dataclass
from typing import Callable, Awaitable

from aiohttp import web

from remote_control.config import WeComConfig
from remote_control.wecom.crypto import (
    decrypt_message,
    parse_message_xml,
    verify_signature,
)

logger = logging.getLogger(__name__)

# WeCom message types we handle (text, image, voice, video, file)
_SUPPORTED_MSG_TYPES = {"text", "image", "voice", "video", "file"}


@dataclass
class IncomingMessage:
    user_id: str
    content: str
    msg_id: str
    agent_id: str
    msg_type: str = "text"
    media_id: str = ""
    # For file messages, WeCom provides a filename in the Title field
    file_name: str = ""


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class WeComGateway:
    def __init__(self, config: WeComConfig, on_message: MessageHandler):
        self._config = config
        self._on_message = on_message

    async def handle_verify(self, request: web.Request) -> web.Response:
        """Handle WeCom callback URL verification (GET)."""
        msg_signature = request.query.get("msg_signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")
        echostr = request.query.get("echostr", "")

        if not verify_signature(self._config.token, timestamp, nonce, echostr, msg_signature):
            logger.warning("Callback verification failed: invalid signature")
            return web.Response(status=403, text="invalid signature")

        decrypted = decrypt_message(self._config.encoding_aes_key, echostr)
        logger.info("Callback verification successful")
        return web.Response(text=decrypted.content)

    async def handle_message(self, request: web.Request) -> web.Response:
        """Handle incoming WeCom message (POST). Must respond within 5 seconds."""
        msg_signature = request.query.get("msg_signature", "")
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")

        body = await request.text()
        xml_data = parse_message_xml(body)
        encrypt = xml_data.get("Encrypt", "")

        if not verify_signature(self._config.token, timestamp, nonce, encrypt, msg_signature):
            logger.warning("Message signature verification failed")
            return web.Response(status=403, text="invalid signature")

        decrypted = decrypt_message(self._config.encoding_aes_key, encrypt)
        inner_xml = parse_message_xml(decrypted.content)

        msg_type = inner_xml.get("MsgType", "")
        if msg_type not in _SUPPORTED_MSG_TYPES:
            logger.info("Ignoring unsupported message type: %s", msg_type)
            return web.Response(text="success")

        msg = _parse_incoming_message(inner_xml, msg_type)
        logger.info("Received %s message from %s: %s", msg_type, msg.user_id, msg.content[:50])

        # Dispatch asynchronously — don't block the response
        import asyncio
        asyncio.create_task(self._safe_handle(msg))

        return web.Response(text="success")

    async def _safe_handle(self, msg: IncomingMessage) -> None:
        try:
            await self._on_message(msg)
        except Exception:
            logger.exception("Error handling message %s", msg.msg_id)


def _parse_incoming_message(xml: dict, msg_type: str) -> IncomingMessage:
    """Parse an IncomingMessage from decrypted WeCom XML fields."""
    msg = IncomingMessage(
        user_id=xml.get("FromUserName", ""),
        content=xml.get("Content", ""),
        msg_id=xml.get("MsgId", ""),
        agent_id=xml.get("AgentID", ""),
        msg_type=msg_type,
    )
    if msg_type == "image":
        msg.media_id = xml.get("MediaId", "")
        # PicUrl is a direct URL but may expire; prefer MediaId download
        if not msg.content:
            msg.content = xml.get("PicUrl", "")
    elif msg_type == "voice":
        msg.media_id = xml.get("MediaId", "")
    elif msg_type == "video":
        msg.media_id = xml.get("MediaId", "")
    elif msg_type == "file":
        msg.media_id = xml.get("MediaId", "")
        msg.file_name = xml.get("Title", "") or xml.get("FileName", "")
    return msg

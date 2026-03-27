"""WeCom API client for sending messages and managing access tokens."""

import asyncio
import logging
import time
from pathlib import Path

import httpx

from remote_control.config import WeComConfig

logger = logging.getLogger(__name__)

WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
WECOM_MAX_TEXT_BYTES = 2048


class WeComAPI:
    def __init__(self, config: WeComConfig):
        self._config = config
        self._client = httpx.AsyncClient(
            timeout=30,
            proxy=config.proxy or None,
        )
        self._token: str | None = None
        self._token_expires_at: float = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def get_access_token(self) -> str:
        """Get a cached access token, refreshing if expired."""
        if self._token and time.monotonic() < self._token_expires_at - 300:
            return self._token
        resp = await self._client.get(
            f"{WECOM_API_BASE}/gettoken",
            params={"corpid": self._config.corp_id, "corpsecret": self._config.secret},
        )
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"Failed to get access token: {data}")
        self._token = data["access_token"]
        self._token_expires_at = time.monotonic() + data["expires_in"]
        logger.info("WeCom access token refreshed, expires in %ds", data["expires_in"])
        return self._token

    async def send_text(self, user_id: str, content: str) -> dict:
        """Send a text message to a user. Auto-splits if over the byte limit."""
        if not content or not content.strip():
            return {}
        if len(content.encode("utf-8")) <= WECOM_MAX_TEXT_BYTES:
            return await self._send_message(
                user_id, msgtype="text", body={"text": {"content": content}}
            )
        return await self._send_chunks(
            user_id, _split_by_bytes(content, WECOM_MAX_TEXT_BYTES),
            msgtype="text", body_key="text", content_key="content",
        )

    async def send_markdown(self, user_id: str, content: str) -> dict:
        """Send a markdown message to a user. Auto-splits if over the byte limit."""
        if not content or not content.strip():
            return {}
        if len(content.encode("utf-8")) <= WECOM_MAX_TEXT_BYTES:
            return await self._send_message(
                user_id, msgtype="markdown", body={"markdown": {"content": content}}
            )
        return await self._send_chunks(
            user_id, _split_by_bytes(content, WECOM_MAX_TEXT_BYTES),
            msgtype="markdown", body_key="markdown", content_key="content",
        )

    async def _send_chunks(
        self, user_id: str, chunks: list[str],
        msgtype: str, body_key: str, content_key: str,
    ) -> dict:
        """Send multiple message chunks with inter-chunk delay.

        Retry logic is handled by _send_message itself.
        """
        result = {}
        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
            if i > 0:
                await asyncio.sleep(0.5)
            body = {body_key: {content_key: chunk}}
            try:
                result = await self._send_message(user_id, msgtype=msgtype, body=body)
            except Exception:
                logger.exception(
                    "Chunk %d/%d LOST (network error, content=%d bytes)",
                    i + 1, len(chunks), len(chunk.encode("utf-8")),
                )
        return result

    async def send_file(self, user_id: str, media_id: str) -> dict:
        """Send a file message using a previously uploaded media_id."""
        return await self._send_message(
            user_id, msgtype="file", body={"file": {"media_id": media_id}}
        )

    async def send_image(self, user_id: str, media_id: str) -> dict:
        """Send an image message using a previously uploaded media_id."""
        return await self._send_message(
            user_id, msgtype="image", body={"image": {"media_id": media_id}}
        )

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Download a temporary media file from WeCom.

        Returns:
            Tuple of (file_content_bytes, content_type).
        """
        token = await self.get_access_token()
        resp = await self._client.get(
            f"{WECOM_API_BASE}/media/get",
            params={"access_token": token, "media_id": media_id},
        )
        content_type = resp.headers.get("Content-Type", "application/octet-stream")
        # If the response is JSON, it's an error
        if "json" in content_type or "text" in content_type:
            data = resp.json()
            raise RuntimeError(f"Failed to download media: {data}")
        logger.info("Downloaded media %s (%d bytes, %s)", media_id[:20], len(resp.content), content_type)
        return resp.content, content_type

    async def upload_media(self, media_type: str, file_path: str | Path, filename: str | None = None) -> str:
        """Upload a temporary media file to WeCom and return the media_id.

        Args:
            media_type: "file" or "image"
            file_path: Local path to the file
            filename: Optional filename override (defaults to file basename)

        Returns:
            The media_id for use in send_file/send_image.
        """
        token = await self.get_access_token()
        path = Path(file_path)
        fname = filename or path.name

        with open(path, "rb") as f:
            files = {"media": (fname, f)}
            resp = await self._client.post(
                f"{WECOM_API_BASE}/media/upload",
                params={"access_token": token, "type": media_type},
                files=files,
            )

        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"Failed to upload media: {data}")
        media_id = data["media_id"]
        logger.info("Uploaded %s '%s' → media_id=%s", media_type, fname, media_id[:20])
        return media_id

    async def upload_and_send_file(self, user_id: str, file_path: str | Path, filename: str | None = None) -> dict:
        """Upload a file and send it to the user in one step."""
        media_id = await self.upload_media("file", file_path, filename)
        return await self.send_file(user_id, media_id)

    async def upload_and_send_image(self, user_id: str, file_path: str | Path) -> dict:
        """Upload an image and send it to the user in one step."""
        media_id = await self.upload_media("image", file_path)
        return await self.send_image(user_id, media_id)

    async def _send_message(self, user_id: str, msgtype: str, body: dict) -> dict:
        token = await self.get_access_token()
        payload = {
            "touser": user_id,
            "msgtype": msgtype,
            "agentid": self._config.agent_id,
            **body,
        }
        resp = await self._client.post(
            f"{WECOM_API_BASE}/message/send", params={"access_token": token}, json=payload
        )
        data = resp.json()
        errcode = data.get("errcode", 0)
        if errcode == 42001:
            # Token expired, refresh and retry once
            logger.warning("Access token expired, refreshing...")
            self._token = None
            token = await self.get_access_token()
            resp = await self._client.post(
                f"{WECOM_API_BASE}/message/send", params={"access_token": token}, json=payload
            )
            data = resp.json()
            errcode = data.get("errcode", 0)
        if errcode != 0:
            # Retry once on transient/rate-limit errors (any non-zero errcode)
            logger.warning("send_message errcode=%d, retrying after 1s: %s", errcode, data)
            await asyncio.sleep(1.0)
            token = await self.get_access_token()
            resp = await self._client.post(
                f"{WECOM_API_BASE}/message/send", params={"access_token": token}, json=payload
            )
            data = resp.json()
            if data.get("errcode", 0) != 0:
                logger.error("send_message FAILED after retry: %s", data)
        return data


def _split_by_bytes(text: str, max_bytes: int) -> list[str]:
    """Split text into chunks that each fit within the byte limit.

    Splits on character boundaries (never mid-character) and prefers newlines.
    """
    if not text:
        return []

    chunks: list[str] = []
    remaining = text

    while remaining:
        encoded = remaining.encode("utf-8")
        if len(encoded) <= max_bytes:
            chunks.append(remaining)
            break

        # Find the character boundary that fits within max_bytes.
        # Walk characters to find the safe cut point (never split mid-char).
        byte_count = 0
        char_count = 0
        for ch in remaining:
            ch_bytes = len(ch.encode("utf-8"))
            if byte_count + ch_bytes > max_bytes:
                break
            byte_count += ch_bytes
            char_count += 1

        if char_count == 0:
            # Single character exceeds limit (shouldn't happen with 2048 limit)
            chunks.append(remaining[0])
            remaining = remaining[1:]
            continue

        cut = remaining[:char_count]

        # Try to break at a newline in the second half for readability
        last_nl = cut.rfind("\n", len(cut) // 2)
        if last_nl > 0:
            chunks.append(remaining[:last_nl + 1])
            remaining = remaining[last_nl + 1:]
        else:
            chunks.append(cut)
            remaining = remaining[char_count:]

    return chunks

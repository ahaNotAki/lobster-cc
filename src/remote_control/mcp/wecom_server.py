"""WeCom MCP Server — exposes WeCom message sending as MCP tools.

This is a standalone stdio-based MCP server. It is registered in .mcp.json
so that any Claude Code process in the working directory (including
scheduler-spawned ones) can send messages back to users via WeCom.

Configuration is via environment variables (set in .mcp.json):
    WECOM_CORP_ID, WECOM_AGENT_ID, WECOM_SECRET, WECOM_PROXY (optional)
"""

import os
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"

# --- Lightweight WeCom client (standalone, no dependency on main app) ---

_client: httpx.Client | None = None
_token: str | None = None
_token_expires_at: float = 0


def _get_config():
    corp_id = os.environ.get("WECOM_CORP_ID", "")
    agent_id = int(os.environ.get("WECOM_AGENT_ID", "0"))
    secret = os.environ.get("WECOM_SECRET", "")
    proxy = os.environ.get("WECOM_PROXY", "") or None
    if not corp_id or not secret or not agent_id:
        raise RuntimeError(
            "WECOM_CORP_ID, WECOM_AGENT_ID, and WECOM_SECRET must be set."
        )
    return corp_id, agent_id, secret, proxy


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _, _, _, proxy = _get_config()
        _client = httpx.Client(timeout=30, proxy=proxy)
    return _client


def _get_access_token() -> str:
    global _token, _token_expires_at
    if _token and time.monotonic() < _token_expires_at - 300:
        return _token
    corp_id, _, secret, _ = _get_config()
    client = _get_client()
    resp = client.get(
        f"{WECOM_API_BASE}/gettoken",
        params={"corpid": corp_id, "corpsecret": secret},
    )
    data = resp.json()
    if data.get("errcode", 0) != 0:
        raise RuntimeError(f"Failed to get access token: {data}")
    _token = data["access_token"]
    _token_expires_at = time.monotonic() + data["expires_in"]
    return _token


def _send_message(user_id: str, msgtype: str, body: dict) -> dict:
    _, agent_id, _, _ = _get_config()
    token = _get_access_token()
    payload = {
        "touser": user_id,
        "msgtype": msgtype,
        "agentid": agent_id,
        **body,
    }
    client = _get_client()
    resp = client.post(
        f"{WECOM_API_BASE}/message/send",
        params={"access_token": token},
        json=payload,
    )
    data = resp.json()
    if data.get("errcode", 0) != 0:
        raise RuntimeError(f"WeCom send failed: {data}")
    return data


def _upload_media(media_type: str, file_path: str) -> str:
    token = _get_access_token()
    client = _get_client()
    path = Path(file_path)
    with open(path, "rb") as f:
        resp = client.post(
            f"{WECOM_API_BASE}/media/upload",
            params={"access_token": token, "type": media_type},
            files={"media": (path.name, f)},
        )
    data = resp.json()
    if data.get("errcode", 0) != 0:
        raise RuntimeError(f"WeCom upload failed: {data}")
    return data["media_id"]


# --- MCP Server ---

mcp = FastMCP("wecom", instructions="Send messages to users via WeCom (企业微信).")


@mcp.tool()
def send_wecom_message(user_id: str, content: str) -> str:
    """Send a text message to a WeCom user.

    Args:
        user_id: The WeCom user ID to send to.
        content: The message content (plain text, max ~2KB).
    """
    _send_message(user_id, "text", {"text": {"content": content}})
    return f"Message sent to {user_id}."


@mcp.tool()
def send_wecom_image(user_id: str, file_path: str) -> str:
    """Upload and send an image to a WeCom user.

    Args:
        user_id: The WeCom user ID to send to.
        file_path: Absolute path to the image file (png, jpg, gif, etc.).
    """
    media_id = _upload_media("image", file_path)
    _send_message(user_id, "image", {"image": {"media_id": media_id}})
    return f"Image {file_path} sent to {user_id}."


@mcp.tool()
def send_wecom_file(user_id: str, file_path: str) -> str:
    """Upload and send a file to a WeCom user.

    Args:
        user_id: The WeCom user ID to send to.
        file_path: Absolute path to the file.
    """
    media_id = _upload_media("file", file_path)
    _send_message(user_id, "file", {"file": {"media_id": media_id}})
    return f"File {file_path} sent to {user_id}."


if __name__ == "__main__":
    mcp.run()

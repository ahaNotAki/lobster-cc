"""Tests for the WeCom MCP server tools."""

import os
import pytest
from unittest.mock import patch, MagicMock

from remote_control.mcp.wecom_server import (
    send_wecom_message,
    send_wecom_image,
    send_wecom_file,
    _get_config,
    _send_message,
    _upload_media,
)


@pytest.fixture(autouse=True)
def reset_globals():
    """Reset module-level globals between tests."""
    import remote_control.mcp.wecom_server as mod
    mod._client = None
    mod._token = None
    mod._token_expires_at = 0
    yield
    mod._client = None
    mod._token = None
    mod._token_expires_at = 0


@pytest.fixture
def wecom_env(monkeypatch):
    monkeypatch.setenv("WECOM_CORP_ID", "test_corp")
    monkeypatch.setenv("WECOM_AGENT_ID", "1000002")
    monkeypatch.setenv("WECOM_SECRET", "test_secret")
    monkeypatch.setenv("WECOM_PROXY", "")


# --- _get_config ---


def test_get_config_success(wecom_env):
    corp_id, agent_id, secret, proxy = _get_config()
    assert corp_id == "test_corp"
    assert agent_id == 1000002
    assert secret == "test_secret"
    assert proxy is None


def test_get_config_missing_env():
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="WECOM_CORP_ID"):
            _get_config()


def test_get_config_with_proxy(monkeypatch):
    monkeypatch.setenv("WECOM_CORP_ID", "c")
    monkeypatch.setenv("WECOM_AGENT_ID", "1")
    monkeypatch.setenv("WECOM_SECRET", "s")
    monkeypatch.setenv("WECOM_PROXY", "socks5://127.0.0.1:1080")
    _, _, _, proxy = _get_config()
    assert proxy == "socks5://127.0.0.1:1080"


# --- send_wecom_message ---


def test_send_message(wecom_env):
    with patch("remote_control.mcp.wecom_server._send_message") as mock_send:
        result = send_wecom_message("user1", "hello")
        mock_send.assert_called_once_with("user1", "text", {"text": {"content": "hello"}})
        assert "user1" in result


# --- send_wecom_image ---


def test_send_image(wecom_env):
    with patch("remote_control.mcp.wecom_server._upload_media", return_value="media123") as mock_up, \
         patch("remote_control.mcp.wecom_server._send_message") as mock_send:
        result = send_wecom_image("user1", "/path/to/img.png")
        mock_up.assert_called_once_with("image", "/path/to/img.png")
        mock_send.assert_called_once_with("user1", "image", {"image": {"media_id": "media123"}})
        assert "img.png" in result


# --- send_wecom_file ---


def test_send_file(wecom_env):
    with patch("remote_control.mcp.wecom_server._upload_media", return_value="media456") as mock_up, \
         patch("remote_control.mcp.wecom_server._send_message") as mock_send:
        result = send_wecom_file("user1", "/path/to/report.pdf")
        mock_up.assert_called_once_with("file", "/path/to/report.pdf")
        mock_send.assert_called_once_with("user1", "file", {"file": {"media_id": "media456"}})
        assert "report.pdf" in result


# --- _send_message (integration-level mock) ---


def test_send_message_api_call(wecom_env):
    mock_client = MagicMock()
    mock_client.get.return_value = MagicMock(
        json=MagicMock(return_value={"errcode": 0, "access_token": "tok", "expires_in": 7200})
    )
    mock_client.post.return_value = MagicMock(
        json=MagicMock(return_value={"errcode": 0, "errmsg": "ok"})
    )

    with patch("remote_control.mcp.wecom_server._get_client", return_value=mock_client):
        result = _send_message("user1", "text", {"text": {"content": "hi"}})
        assert result["errcode"] == 0
        # Verify POST was called with correct payload
        call_kwargs = mock_client.post.call_args
        payload = call_kwargs[1]["json"]
        assert payload["touser"] == "user1"
        assert payload["agentid"] == 1000002
        assert payload["msgtype"] == "text"


def test_send_message_api_error(wecom_env):
    mock_client = MagicMock()
    mock_client.get.return_value = MagicMock(
        json=MagicMock(return_value={"errcode": 0, "access_token": "tok", "expires_in": 7200})
    )
    mock_client.post.return_value = MagicMock(
        json=MagicMock(return_value={"errcode": 60020, "errmsg": "invalid user"})
    )

    with patch("remote_control.mcp.wecom_server._get_client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="WeCom send failed"):
            _send_message("bad_user", "text", {"text": {"content": "hi"}})


# --- _upload_media ---


def test_upload_media(wecom_env, tmp_path):
    test_file = tmp_path / "test.png"
    test_file.write_bytes(b"\x89PNG")

    mock_client = MagicMock()
    mock_client.get.return_value = MagicMock(
        json=MagicMock(return_value={"errcode": 0, "access_token": "tok", "expires_in": 7200})
    )
    mock_client.post.return_value = MagicMock(
        json=MagicMock(return_value={"errcode": 0, "media_id": "m123"})
    )

    with patch("remote_control.mcp.wecom_server._get_client", return_value=mock_client):
        media_id = _upload_media("image", str(test_file))
        assert media_id == "m123"

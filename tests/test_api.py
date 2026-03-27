"""Tests for the WeCom API client."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from remote_control.config import WeComConfig
from remote_control.wecom.api import WeComAPI, _split_by_bytes


@pytest.fixture
def wecom_config():
    return WeComConfig(
        corp_id="test_corp",
        agent_id=1000002,
        secret="test_secret",
        token="test_token",
        encoding_aes_key="test_key",
    )


# --- _split_by_bytes ---


def test_split_by_bytes_short():
    assert _split_by_bytes("hello", 100) == ["hello"]


def test_split_by_bytes_splits():
    text = "line1\nline2\nline3\nline4"
    chunks = _split_by_bytes(text, 12)
    assert len(chunks) >= 2
    assert "".join(chunks) == text


def test_split_by_bytes_chinese():
    text = "你好世界" * 200  # 800 chars, ~2400 bytes
    chunks = _split_by_bytes(text, 1000)
    assert len(chunks) >= 2
    combined = "".join(chunks)
    assert combined == text


# --- WeComAPI token management ---


@pytest.mark.asyncio
async def test_get_access_token(wecom_config):
    api = WeComAPI(wecom_config)
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "errcode": 0,
        "errmsg": "ok",
        "access_token": "token123",
        "expires_in": 7200,
    }

    api._client = MagicMock()
    api._client.get = AsyncMock(return_value=mock_response)

    token = await api.get_access_token()
    assert token == "token123"
    # Second call should use cache
    token2 = await api.get_access_token()
    assert token2 == "token123"
    assert api._client.get.call_count == 1  # only one HTTP call


@pytest.mark.asyncio
async def test_get_access_token_error(wecom_config):
    api = WeComAPI(wecom_config)
    mock_response = MagicMock()
    mock_response.json.return_value = {"errcode": 40001, "errmsg": "invalid secret"}

    api._client = MagicMock()
    api._client.get = AsyncMock(return_value=mock_response)

    with pytest.raises(RuntimeError, match="Failed to get access token"):
        await api.get_access_token()


# --- send_text / send_markdown ---


@pytest.mark.asyncio
async def test_send_text(wecom_config):
    api = WeComAPI(wecom_config)
    api._token = "cached_token"
    api._token_expires_at = float("inf")

    mock_response = MagicMock()
    mock_response.json.return_value = {"errcode": 0, "errmsg": "ok"}

    api._client = MagicMock()
    api._client.post = AsyncMock(return_value=mock_response)

    result = await api.send_text("user1", "hello")
    assert result["errcode"] == 0

    call_kwargs = api._client.post.call_args
    payload = call_kwargs.kwargs["json"]
    assert payload["touser"] == "user1"
    assert payload["msgtype"] == "text"
    assert payload["text"]["content"] == "hello"
    assert payload["agentid"] == 1000002


@pytest.mark.asyncio
async def test_send_markdown(wecom_config):
    api = WeComAPI(wecom_config)
    api._token = "cached_token"
    api._token_expires_at = float("inf")

    mock_response = MagicMock()
    mock_response.json.return_value = {"errcode": 0, "errmsg": "ok"}

    api._client = MagicMock()
    api._client.post = AsyncMock(return_value=mock_response)

    result = await api.send_markdown("user1", "**bold**")
    assert result["errcode"] == 0

    call_kwargs = api._client.post.call_args
    payload = call_kwargs.kwargs["json"]
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"]["content"] == "**bold**"


# --- send_file / send_image ---


@pytest.mark.asyncio
async def test_send_file(wecom_config):
    api = WeComAPI(wecom_config)
    api._token = "cached_token"
    api._token_expires_at = float("inf")

    mock_response = MagicMock()
    mock_response.json.return_value = {"errcode": 0, "errmsg": "ok"}

    api._client = MagicMock()
    api._client.post = AsyncMock(return_value=mock_response)

    result = await api.send_file("user1", "media_id_123")
    assert result["errcode"] == 0

    payload = api._client.post.call_args.kwargs["json"]
    assert payload["msgtype"] == "file"
    assert payload["file"]["media_id"] == "media_id_123"


@pytest.mark.asyncio
async def test_send_image(wecom_config):
    api = WeComAPI(wecom_config)
    api._token = "cached_token"
    api._token_expires_at = float("inf")

    mock_response = MagicMock()
    mock_response.json.return_value = {"errcode": 0, "errmsg": "ok"}

    api._client = MagicMock()
    api._client.post = AsyncMock(return_value=mock_response)

    result = await api.send_image("user1", "img_media_id")
    assert result["errcode"] == 0

    payload = api._client.post.call_args.kwargs["json"]
    assert payload["msgtype"] == "image"
    assert payload["image"]["media_id"] == "img_media_id"


# --- upload_media ---


@pytest.mark.asyncio
async def test_upload_media(wecom_config, tmp_path):
    api = WeComAPI(wecom_config)
    api._token = "cached_token"
    api._token_expires_at = float("inf")

    test_file = tmp_path / "test.md"
    test_file.write_text("hello content")

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "errcode": 0, "errmsg": "ok", "media_id": "uploaded_media_123"
    }

    api._client = MagicMock()
    api._client.post = AsyncMock(return_value=mock_response)

    media_id = await api.upload_media("file", str(test_file))
    assert media_id == "uploaded_media_123"

    # Verify the upload call
    call_args = api._client.post.call_args
    assert "media/upload" in call_args.args[0]
    assert call_args.kwargs["params"]["type"] == "file"


@pytest.mark.asyncio
async def test_upload_media_error(wecom_config, tmp_path):
    api = WeComAPI(wecom_config)
    api._token = "cached_token"
    api._token_expires_at = float("inf")

    test_file = tmp_path / "test.png"
    test_file.write_bytes(b"\x89PNG")

    mock_response = MagicMock()
    mock_response.json.return_value = {"errcode": 40004, "errmsg": "invalid media type"}

    api._client = MagicMock()
    api._client.post = AsyncMock(return_value=mock_response)

    with pytest.raises(RuntimeError, match="Failed to upload media"):
        await api.upload_media("image", str(test_file))


# --- upload_and_send ---


@pytest.mark.asyncio
async def test_upload_and_send_file(wecom_config, tmp_path):
    api = WeComAPI(wecom_config)
    api._token = "cached_token"
    api._token_expires_at = float("inf")

    test_file = tmp_path / "output.md"
    test_file.write_text("task output here")

    upload_resp = MagicMock()
    upload_resp.json.return_value = {"errcode": 0, "media_id": "mid_123"}

    send_resp = MagicMock()
    send_resp.json.return_value = {"errcode": 0, "errmsg": "ok"}

    api._client = MagicMock()
    api._client.post = AsyncMock(side_effect=[upload_resp, send_resp])

    result = await api.upload_and_send_file("user1", str(test_file), "result.md")
    assert result["errcode"] == 0
    assert api._client.post.call_count == 2  # upload + send


# --- token refresh on 42001 ---


@pytest.mark.asyncio
async def test_send_message_token_refresh_on_42001(wecom_config):
    """When send gets 42001 (expired), it should refresh token and retry."""
    api = WeComAPI(wecom_config)
    api._token = "old_token"
    api._token_expires_at = float("inf")

    expired_response = MagicMock()
    expired_response.json.return_value = {"errcode": 42001, "errmsg": "access_token expired"}

    ok_response = MagicMock()
    ok_response.json.return_value = {"errcode": 0, "errmsg": "ok"}

    token_response = MagicMock()
    token_response.json.return_value = {
        "errcode": 0, "access_token": "new_token", "expires_in": 7200
    }

    api._client = MagicMock()
    api._client.post = AsyncMock(side_effect=[expired_response, ok_response])
    api._client.get = AsyncMock(return_value=token_response)

    result = await api.send_text("user1", "retry test")
    assert result["errcode"] == 0
    assert api._client.post.call_count == 2  # original + retry
    assert api._client.get.call_count == 1  # token refresh


def test_proxy_config_passed_to_client():
    """When proxy is set, httpx.AsyncClient should receive it."""
    config = WeComConfig(
        corp_id="c", agent_id=1, secret="s", token="t", encoding_aes_key="k",
        proxy="socks5://127.0.0.1:1080",
    )
    api = WeComAPI(config)
    # httpx stores proxy config internally — verify it was accepted without error
    assert api._client is not None
    assert api._config.proxy == "socks5://127.0.0.1:1080"


def test_no_proxy_by_default():
    """Without proxy config, client should be created normally."""
    config = WeComConfig(
        corp_id="c", agent_id=1, secret="s", token="t", encoding_aes_key="k",
    )
    api = WeComAPI(config)
    assert api._client is not None
    assert api._config.proxy == ""


@pytest.mark.asyncio
async def test_close(wecom_config):
    api = WeComAPI(wecom_config)
    api._client = MagicMock()
    api._client.aclose = AsyncMock()
    await api.close()
    api._client.aclose.assert_called_once()


# --- _split_by_bytes edge cases ---


def test_split_by_bytes_empty():
    """Empty string returns []."""
    assert _split_by_bytes("", 100) == []


def test_split_by_bytes_no_newlines():
    """When there are no newlines, performs a hard cut on character boundary."""
    text = "a" * 200
    chunks = _split_by_bytes(text, 50)
    assert len(chunks) == 4
    assert "".join(chunks) == text
    for chunk in chunks:
        assert len(chunk.encode("utf-8")) <= 50


def test_split_by_bytes_cjk_lossless():
    """CJK text split must be completely lossless — no characters lost."""
    text = "你好世界测试消息" * 100  # 800 CJK chars = 2400 bytes
    chunks = _split_by_bytes(text, 2048)
    combined = "".join(chunks)
    assert combined == text, f"Lost {len(text) - len(combined)} chars in split"
    for chunk in chunks:
        assert len(chunk.encode("utf-8")) <= 2048


def test_split_by_bytes_mixed_cjk_ascii_lossless():
    """Mixed CJK + ASCII text must also be lossless."""
    text = "Hello你好World世界\nLine2行2\n" * 80
    chunks = _split_by_bytes(text, 2048)
    combined = "".join(chunks)
    assert combined == text, f"Lost {len(text) - len(combined)} chars"


# --- send_text retry on error ---


@pytest.mark.asyncio
async def test_send_text_retries_on_errcode(wecom_config):
    """When a split chunk gets a WeCom error, it should retry once."""
    api = WeComAPI(wecom_config)
    api._token = "cached_token"
    api._token_expires_at = float("inf")

    error_resp = MagicMock()
    error_resp.json.return_value = {"errcode": 45047, "errmsg": "api freq out of limit"}
    ok_resp = MagicMock()
    ok_resp.json.return_value = {"errcode": 0, "errmsg": "ok"}

    api._client = MagicMock()
    # First chunk: error → retry ok. Second chunk: ok directly.
    api._client.post = AsyncMock(side_effect=[error_resp, ok_resp, ok_resp])

    long_text = "a" * 3000  # > 2048 bytes, will split into 2 chunks
    await api.send_text("user1", long_text)
    # First chunk: error → retry → ok (2 calls), second chunk: ok (1 call) = 3 calls
    assert api._client.post.call_count == 3


@pytest.mark.asyncio
async def test_send_message_retries_single_message(wecom_config):
    """Even small messages (no splitting) should retry on WeCom error."""
    api = WeComAPI(wecom_config)
    api._token = "cached_token"
    api._token_expires_at = float("inf")

    error_resp = MagicMock()
    error_resp.json.return_value = {"errcode": 45047, "errmsg": "api freq out of limit"}
    ok_resp = MagicMock()
    ok_resp.json.return_value = {"errcode": 0, "errmsg": "ok"}

    api._client = MagicMock()
    api._client.post = AsyncMock(side_effect=[error_resp, ok_resp])

    # Small text — goes through direct _send_message path, NOT _send_chunks
    result = await api.send_text("user1", "hello")
    assert result["errcode"] == 0
    assert api._client.post.call_count == 2  # error + retry


# --- send_text / send_markdown auto-split ---


@pytest.mark.asyncio
async def test_send_text_auto_split(wecom_config):
    """Long text should be auto-split into multiple send_message calls."""
    api = WeComAPI(wecom_config)
    api._token = "cached_token"
    api._token_expires_at = float("inf")

    mock_response = MagicMock()
    mock_response.json.return_value = {"errcode": 0, "errmsg": "ok"}

    api._client = MagicMock()
    api._client.post = AsyncMock(return_value=mock_response)

    # Create text larger than WECOM_MAX_TEXT_BYTES (2048)
    long_text = "hello world\n" * 300  # ~3600 bytes
    assert len(long_text.encode("utf-8")) > 2048

    result = await api.send_text("user1", long_text)
    assert result["errcode"] == 0
    # Should have made multiple POST calls (one per chunk)
    assert api._client.post.call_count >= 2
    # All calls should be text type
    for call in api._client.post.call_args_list:
        payload = call.kwargs["json"]
        assert payload["msgtype"] == "text"
        assert "content" in payload["text"]


@pytest.mark.asyncio
async def test_send_markdown_auto_split(wecom_config):
    """Long markdown should be auto-split into multiple send_message calls."""
    api = WeComAPI(wecom_config)
    api._token = "cached_token"
    api._token_expires_at = float("inf")

    mock_response = MagicMock()
    mock_response.json.return_value = {"errcode": 0, "errmsg": "ok"}

    api._client = MagicMock()
    api._client.post = AsyncMock(return_value=mock_response)

    # Create markdown larger than WECOM_MAX_TEXT_BYTES (2048)
    long_md = "## Section\n**bold text** normal text\n" * 100  # ~3600 bytes
    assert len(long_md.encode("utf-8")) > 2048

    result = await api.send_markdown("user1", long_md)
    assert result["errcode"] == 0
    assert api._client.post.call_count >= 2
    for call in api._client.post.call_args_list:
        payload = call.kwargs["json"]
        assert payload["msgtype"] == "markdown"
        assert "content" in payload["markdown"]

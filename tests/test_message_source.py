"""Tests for the message source abstraction and implementations."""


import pytest
from unittest.mock import AsyncMock, MagicMock
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from remote_control.config import WeComConfig
from remote_control.wecom.crypto import encrypt_message, make_signature
from remote_control.wecom.message_source import CallbackSource, RelayPollingSource


TEST_AES_KEY = "kWxPEV2UEDyxWpmPB8jfIqLfNjGjRiIpG2lMGKEQCTm"
TEST_TOKEN = "test_token"
TEST_CORP_ID = "test_corp"


@pytest.fixture
def wecom_config():
    return WeComConfig(
        corp_id=TEST_CORP_ID,
        agent_id=1000002,
        secret="test_secret",
        token=TEST_TOKEN,
        encoding_aes_key=TEST_AES_KEY,
    )


@pytest.fixture
def on_message():
    return AsyncMock()


@pytest.fixture
def mock_store():
    s = MagicMock()
    s.get_kv = MagicMock(return_value="")
    s.set_kv = MagicMock()
    return s


def _make_encrypted_xml(content_xml: str) -> tuple[str, str, str, str]:
    """Create a properly encrypted WeCom message with valid signature.

    Returns (body_xml, msg_signature, timestamp, nonce).
    """
    encrypted = encrypt_message(TEST_AES_KEY, TEST_CORP_ID, content_xml)
    timestamp = "1234567890"
    nonce = "testnonce"
    signature = make_signature(TEST_TOKEN, timestamp, nonce, encrypted)
    body = f"<xml><Encrypt>{encrypted}</Encrypt></xml>"
    return body, signature, timestamp, nonce


# --- CallbackSource ---


def test_callback_source_registers_routes(wecom_config, on_message):
    source = CallbackSource(wecom_config, on_message)
    app = web.Application()
    source.register_routes(app)
    route_paths = [r.resource.canonical for r in app.router.routes()]
    assert "/wecom/callback/1000002" in route_paths


@pytest.mark.asyncio
async def test_callback_source_start_stop(wecom_config, on_message):
    source = CallbackSource(wecom_config, on_message)
    await source.start()  # no-op for callback
    await source.stop()  # no-op for callback


# --- RelayPollingSource ---


def test_relay_source_registers_status_route(wecom_config, on_message, mock_store):
    source = RelayPollingSource(wecom_config, "http://relay.example.com", on_message, store=mock_store)
    app = web.Application()
    source.register_routes(app)
    route_paths = [r.resource.canonical for r in app.router.routes()]
    assert "/relay/status/1000002" in route_paths


@pytest.mark.asyncio
async def test_relay_source_start_stop(wecom_config, on_message, mock_store):
    source = RelayPollingSource(wecom_config, "http://relay.example.com", on_message, store=mock_store, poll_interval=10.0)
    await source.start()
    assert source._task is not None
    assert not source._task.done()
    await source.stop()
    assert source._task.done() or source._task.cancelled()


@pytest.mark.asyncio
async def test_relay_source_stop_without_start(wecom_config, on_message, mock_store):
    source = RelayPollingSource(wecom_config, "http://relay.example.com", on_message, store=mock_store)
    await source.stop()  # should not raise


@pytest.mark.asyncio
async def test_relay_source_dispatch_encrypted_message(wecom_config, on_message, mock_store):
    """Full decrypt path: raw WeCom XML → verify → decrypt → parse → dispatch."""
    source = RelayPollingSource(wecom_config, "http://relay.example.com", on_message, store=mock_store)

    inner_xml = (
        "<xml>"
        "<MsgType>text</MsgType>"
        "<Content>hello from relay</Content>"
        "<FromUserName>user1</FromUserName>"
        "<MsgId>msg123</MsgId>"
        "</xml>"
    )
    body, sig, ts, nonce = _make_encrypted_xml(inner_xml)

    msg_data = {
        "msg_id": "relay-uuid-1",
        "seq": 1,
        "query_params": {"msg_signature": sig, "timestamp": ts, "nonce": nonce},
        "body": body,
    }
    await source._dispatch_message(msg_data)

    on_message.assert_called_once()
    msg = on_message.call_args[0][0]
    assert msg.user_id == "user1"
    assert msg.content == "hello from relay"
    assert msg.msg_id == "msg123"


@pytest.mark.asyncio
async def test_relay_source_ignores_empty_body(wecom_config, on_message, mock_store):
    source = RelayPollingSource(wecom_config, "http://relay.example.com", on_message, store=mock_store)
    await source._dispatch_message({"msg_id": "1", "body": "", "query_params": {}})
    on_message.assert_not_called()


@pytest.mark.asyncio
async def test_relay_source_dispatches_image(wecom_config, on_message, mock_store):
    """Image messages should be dispatched (not ignored)."""
    source = RelayPollingSource(wecom_config, "http://relay.example.com", on_message, store=mock_store)

    inner_xml = "<xml><MsgType>image</MsgType><FromUserName>u1</FromUserName><MediaId>m1</MediaId></xml>"
    body, sig, ts, nonce = _make_encrypted_xml(inner_xml)

    msg_data = {
        "msg_id": "1",
        "query_params": {"msg_signature": sig, "timestamp": ts, "nonce": nonce},
        "body": body,
    }
    await source._dispatch_message(msg_data)
    on_message.assert_called_once()
    msg = on_message.call_args[0][0]
    assert msg.msg_type == "image"


@pytest.mark.asyncio
async def test_relay_source_ignores_unsupported_type(wecom_config, on_message, mock_store):
    """Unsupported message types (e.g. location) should still be ignored."""
    source = RelayPollingSource(wecom_config, "http://relay.example.com", on_message, store=mock_store)

    inner_xml = "<xml><MsgType>location</MsgType><FromUserName>u1</FromUserName></xml>"
    body, sig, ts, nonce = _make_encrypted_xml(inner_xml)

    msg_data = {
        "msg_id": "1",
        "query_params": {"msg_signature": sig, "timestamp": ts, "nonce": nonce},
        "body": body,
    }
    await source._dispatch_message(msg_data)
    on_message.assert_not_called()


@pytest.mark.asyncio
async def test_relay_source_ignores_invalid_signature(wecom_config, on_message, mock_store):
    source = RelayPollingSource(wecom_config, "http://relay.example.com", on_message, store=mock_store)

    inner_xml = "<xml><MsgType>text</MsgType><Content>test</Content></xml>"
    body, _, ts, nonce = _make_encrypted_xml(inner_xml)

    msg_data = {
        "msg_id": "1",
        "query_params": {"msg_signature": "bad_signature", "timestamp": ts, "nonce": nonce},
        "body": body,
    }
    await source._dispatch_message(msg_data)
    on_message.assert_not_called()


@pytest.mark.asyncio
async def test_relay_source_dispatch_handler_error(wecom_config, on_message, mock_store):
    """Handler errors should be caught, not crash the poller."""
    on_message.side_effect = RuntimeError("handler failed")
    source = RelayPollingSource(wecom_config, "http://relay.example.com", on_message, store=mock_store)

    inner_xml = "<xml><MsgType>text</MsgType><Content>test</Content><FromUserName>u1</FromUserName></xml>"
    body, sig, ts, nonce = _make_encrypted_xml(inner_xml)

    msg_data = {
        "msg_id": "m1",
        "query_params": {"msg_signature": sig, "timestamp": ts, "nonce": nonce},
        "body": body,
    }
    # Should not raise
    await source._dispatch_message(msg_data)


@pytest.mark.asyncio
async def test_relay_status_endpoint(wecom_config, on_message, mock_store):
    source = RelayPollingSource(wecom_config, "http://relay.example.com", on_message, store=mock_store, poll_interval=5.0)
    source._cursor = "cursor_abc"

    app = web.Application()
    source.register_routes(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    resp = await client.get("/relay/status/1000002")
    assert resp.status == 200
    data = await resp.json()
    assert data["source"] == "relay"
    assert data["relay_url"] == "http://relay.example.com"
    assert data["cursor"] == "cursor_abc"
    assert data["poll_interval"] == 5.0

    await client.close()


@pytest.mark.asyncio
async def test_relay_source_poll_once_success(wecom_config, on_message, mock_store):
    """Test _poll_once with a mocked _fetch_messages."""
    source = RelayPollingSource(wecom_config, "http://relay.example.com", on_message, store=mock_store)

    inner_xml = "<xml><MsgType>text</MsgType><Content>polled</Content><FromUserName>u1</FromUserName></xml>"
    body, sig, ts, nonce = _make_encrypted_xml(inner_xml)

    source._fetch_messages = AsyncMock(return_value={
        "next_cursor": "cursor_2",
        "messages": [{
            "msg_id": "m1",
            "seq": 1,
            "query_params": {"msg_signature": sig, "timestamp": ts, "nonce": nonce},
            "body": body,
        }],
    })

    await source._poll_once()

    assert source._cursor == "cursor_2"
    mock_store.set_kv.assert_called_with("relay_cursor_1000002", "cursor_2")
    on_message.assert_called_once()


@pytest.mark.asyncio
async def test_relay_source_poll_once_empty(wecom_config, on_message, mock_store):
    """Test _poll_once when relay returns no messages."""
    source = RelayPollingSource(wecom_config, "http://relay.example.com", on_message, store=mock_store)
    source._fetch_messages = AsyncMock(return_value={
        "next_cursor": "",
        "messages": [],
    })

    await source._poll_once()

    assert source._cursor == ""
    on_message.assert_not_called()


@pytest.mark.asyncio
async def test_relay_source_poll_once_http_error(wecom_config, on_message, mock_store):
    """Test _poll_once when relay returns HTTP error."""
    import httpx

    source = RelayPollingSource(wecom_config, "http://relay.example.com", on_message, store=mock_store)
    source._fetch_messages = AsyncMock(
        side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
    )

    with pytest.raises(httpx.HTTPStatusError):
        await source._poll_once()

    on_message.assert_not_called()


@pytest.mark.asyncio
async def test_relay_source_strips_trailing_slash(wecom_config, on_message, mock_store):
    """Trailing slash in relay_url should be stripped."""
    source = RelayPollingSource(wecom_config, "http://relay.example.com/", on_message, store=mock_store)
    assert source._relay_url == "http://relay.example.com"


@pytest.mark.asyncio
async def test_relay_source_restores_cursor_from_store(wecom_config, on_message):
    """Cursor should be loaded from store on init."""
    store = MagicMock()
    store.get_kv = MagicMock(return_value="saved_cursor_42")
    store.set_kv = MagicMock()
    source = RelayPollingSource(wecom_config, "http://relay.example.com", on_message, store=store)
    assert source._cursor == "saved_cursor_42"

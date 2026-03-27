"""Tests for the WeCom gateway HTTP handler."""

import pytest
from unittest.mock import AsyncMock
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from remote_control.config import WeComConfig
from remote_control.wecom.crypto import encrypt_message, make_signature
from remote_control.wecom.gateway import IncomingMessage, WeComGateway


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
def gateway(wecom_config, on_message):
    return WeComGateway(wecom_config, on_message)


def _make_encrypted_callback_body(content_xml: str) -> tuple[str, str]:
    """Encrypt content and return (encrypted_text, xml_body)."""
    encrypted = encrypt_message(TEST_AES_KEY, TEST_CORP_ID, content_xml)
    body = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"
    return encrypted, body


@pytest.fixture
async def client(gateway):
    app = web.Application()
    app.router.add_get("/wecom/callback", gateway.handle_verify)
    app.router.add_post("/wecom/callback", gateway.handle_message)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


# --- Callback verification (GET) ---


@pytest.mark.asyncio
async def test_verify_valid_signature(client):
    echostr = encrypt_message(TEST_AES_KEY, TEST_CORP_ID, "echo_content")
    timestamp = "1409659813"
    nonce = "nonce123"
    sig = make_signature(TEST_TOKEN, timestamp, nonce, echostr)

    resp = await client.get("/wecom/callback", params={
        "msg_signature": sig,
        "timestamp": timestamp,
        "nonce": nonce,
        "echostr": echostr,
    })
    assert resp.status == 200
    text = await resp.text()
    assert text == "echo_content"


@pytest.mark.asyncio
async def test_verify_invalid_signature(client):
    resp = await client.get("/wecom/callback", params={
        "msg_signature": "wrong",
        "timestamp": "123",
        "nonce": "abc",
        "echostr": "data",
    })
    assert resp.status == 403


# --- Message receiving (POST) ---


@pytest.mark.asyncio
async def test_handle_text_message(client, on_message):
    content_xml = """<xml>
        <ToUserName><![CDATA[test_corp]]></ToUserName>
        <FromUserName><![CDATA[user1]]></FromUserName>
        <CreateTime>1348831860</CreateTime>
        <MsgType><![CDATA[text]]></MsgType>
        <Content><![CDATA[hello world]]></Content>
        <MsgId>123456</MsgId>
        <AgentID>1000002</AgentID>
    </xml>"""
    encrypted, body = _make_encrypted_callback_body(content_xml)
    timestamp = "1409659813"
    nonce = "nonce123"
    sig = make_signature(TEST_TOKEN, timestamp, nonce, encrypted)

    resp = await client.post(
        "/wecom/callback",
        params={"msg_signature": sig, "timestamp": timestamp, "nonce": nonce},
        data=body,
    )
    assert resp.status == 200
    assert await resp.text() == "success"

    # Wait for async dispatch
    import asyncio
    await asyncio.sleep(0.1)

    on_message.assert_called_once()
    msg = on_message.call_args[0][0]
    assert isinstance(msg, IncomingMessage)
    assert msg.user_id == "user1"
    assert msg.content == "hello world"


@pytest.mark.asyncio
async def test_handle_image_message(client, on_message):
    """Image messages should be dispatched with msg_type='image' and media_id."""
    content_xml = """<xml>
        <MsgType><![CDATA[image]]></MsgType>
        <FromUserName><![CDATA[user1]]></FromUserName>
        <MediaId><![CDATA[media123]]></MediaId>
        <PicUrl><![CDATA[http://example.com/pic.jpg]]></PicUrl>
        <MsgId>img456</MsgId>
    </xml>"""
    encrypted, body = _make_encrypted_callback_body(content_xml)
    timestamp = "123"
    nonce = "abc"
    sig = make_signature(TEST_TOKEN, timestamp, nonce, encrypted)

    resp = await client.post(
        "/wecom/callback",
        params={"msg_signature": sig, "timestamp": timestamp, "nonce": nonce},
        data=body,
    )
    assert resp.status == 200

    import asyncio
    await asyncio.sleep(0.1)
    on_message.assert_called_once()
    msg = on_message.call_args[0][0]
    assert msg.msg_type == "image"
    assert msg.media_id == "media123"
    assert msg.user_id == "user1"


@pytest.mark.asyncio
async def test_handle_unsupported_message_type(client, on_message):
    """Unsupported message types (e.g. link, location) should be ignored."""
    content_xml = """<xml>
        <MsgType><![CDATA[location]]></MsgType>
        <FromUserName><![CDATA[user1]]></FromUserName>
    </xml>"""
    encrypted, body = _make_encrypted_callback_body(content_xml)
    timestamp = "123"
    nonce = "abc"
    sig = make_signature(TEST_TOKEN, timestamp, nonce, encrypted)

    resp = await client.post(
        "/wecom/callback",
        params={"msg_signature": sig, "timestamp": timestamp, "nonce": nonce},
        data=body,
    )
    assert resp.status == 200

    import asyncio
    await asyncio.sleep(0.1)
    on_message.assert_not_called()


@pytest.mark.asyncio
async def test_handle_message_invalid_signature(client, on_message):
    body = "<xml><Encrypt>data</Encrypt></xml>"
    resp = await client.post(
        "/wecom/callback",
        params={"msg_signature": "wrong", "timestamp": "123", "nonce": "abc"},
        data=body,
    )
    assert resp.status == 403
    on_message.assert_not_called()


# --- _safe_handle error handling ---


@pytest.mark.asyncio
async def test_safe_handle_exception(gateway, on_message):
    on_message.side_effect = ValueError("boom")
    msg = IncomingMessage(user_id="u", content="c", msg_id="m", agent_id="a")
    # Should not raise
    await gateway._safe_handle(msg)

"""Tests for the self-hosted relay app."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from remote_control.relay.app import RelayConfig, RelayStore, create_relay_app
from remote_control.wecom.crypto import encrypt_message, make_signature

R_AES = "kWxPEV2UEDyxWpmPB8jfIqLfNjGjRiIpG2lMGKEQCTm"
R_TOKEN = "relay_wecom_token"
R_CORP = "relay_corp"


# --- RelayStore ---


def test_relay_store_put_and_fetch(tmp_path):
    store = RelayStore(str(tmp_path / "relay.db"), ttl_seconds=3600)
    store.open()
    seq1 = store.put(agent_id="1000002", body="<xml>a</xml>",
                     query_params={"msg_signature": "s", "timestamp": "1", "nonce": "n"})
    seq2 = store.put(agent_id="1000002", body="<xml>b</xml>", query_params={})
    assert seq2 > seq1

    msgs, next_cursor = store.fetch(cursor=0, limit=100)
    assert len(msgs) == 2
    assert msgs[0]["body"] == "<xml>a</xml>"
    assert msgs[0]["query_params"]["msg_signature"] == "s"
    assert int(next_cursor) == seq2
    store.close()


def test_relay_store_fetch_after_cursor(tmp_path):
    store = RelayStore(str(tmp_path / "relay.db"), ttl_seconds=3600)
    store.open()
    s1 = store.put(agent_id="a", body="1", query_params={})
    store.put(agent_id="a", body="2", query_params={})
    msgs, _ = store.fetch(cursor=s1, limit=100)
    assert len(msgs) == 1
    assert msgs[0]["body"] == "2"
    store.close()


def test_relay_store_fetch_respects_limit(tmp_path):
    store = RelayStore(str(tmp_path / "relay.db"), ttl_seconds=3600)
    store.open()
    for i in range(5):
        store.put(agent_id="a", body=str(i), query_params={})
    msgs, next_cursor = store.fetch(cursor=0, limit=2)
    assert len(msgs) == 2
    msgs2, _ = store.fetch(cursor=int(next_cursor), limit=2)
    assert len(msgs2) == 2
    store.close()


def test_relay_store_purges_expired(tmp_path):
    store = RelayStore(str(tmp_path / "relay.db"), ttl_seconds=3600)
    store.open()
    store.put(agent_id="a", body="old", query_params={}, _now=1000)
    store.put(agent_id="a", body="new", query_params={}, _now=10_000_000_000)
    store.purge_expired(_now=1000 + 3600 + 1)
    msgs, _ = store.fetch(cursor=0, limit=100)
    bodies = [m["body"] for m in msgs]
    assert "old" not in bodies
    assert "new" in bodies
    store.close()


def test_relay_store_queue_depth(tmp_path):
    store = RelayStore(str(tmp_path / "relay.db"), ttl_seconds=3600)
    store.open()
    assert store.queue_depth() == 0
    store.put(agent_id="a", body="1", query_params={})
    assert store.queue_depth() == 1
    store.close()


# --- RelayConfig ---


def test_relay_config_single_agent(monkeypatch):
    monkeypatch.setenv("RELAY_FETCH_TOKEN", "bearer-xyz")
    monkeypatch.setenv("WECOM_TOKEN", "tok")
    monkeypatch.setenv("WECOM_AES_KEY", "aes")
    monkeypatch.delenv("AGENT_CONFIGS", raising=False)
    cfg = RelayConfig.from_env()
    assert cfg.fetch_token == "bearer-xyz"
    assert cfg.agent_creds("") == ("tok", "aes")
    assert cfg.agent_creds("anything") == ("tok", "aes")


def test_relay_config_multi_agent(monkeypatch):
    monkeypatch.setenv("RELAY_FETCH_TOKEN", "bearer-xyz")
    monkeypatch.setenv("AGENT_CONFIGS", '{"1000002": {"token": "t2", "aes_key": "k2"}}')
    monkeypatch.delenv("WECOM_TOKEN", raising=False)
    monkeypatch.delenv("WECOM_AES_KEY", raising=False)
    cfg = RelayConfig.from_env()
    assert cfg.agent_creds("1000002") == ("t2", "k2")
    assert cfg.agent_creds("9999") == ("", "")


def test_relay_config_requires_fetch_token(monkeypatch):
    monkeypatch.delenv("RELAY_FETCH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="RELAY_FETCH_TOKEN"):
        RelayConfig.from_env()


# --- Handlers ---


def _signed_callback_body(content_xml: str, now: int):
    encrypted = encrypt_message(R_AES, R_CORP, content_xml)
    ts = str(now)
    nonce = "nonce1"
    sig = make_signature(R_TOKEN, ts, nonce, encrypted)
    body = f"<xml><Encrypt>{encrypted}</Encrypt><AgentID>1000002</AgentID></xml>"
    return body, sig, ts, nonce


@pytest.fixture
def relay_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_FETCH_TOKEN", "fetch-secret")
    monkeypatch.setenv("WECOM_TOKEN", R_TOKEN)
    monkeypatch.setenv("WECOM_AES_KEY", R_AES)
    monkeypatch.setenv("RELAY_DB_PATH", str(tmp_path / "relay.db"))
    monkeypatch.delenv("AGENT_CONFIGS", raising=False)
    return RelayConfig.from_env()


async def _client(relay_env, now_fn):
    app = create_relay_app(relay_env, now_fn=now_fn)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_callback_stores_valid_message(relay_env):
    now = 1_700_000_000
    client = await _client(relay_env, now_fn=lambda: now)
    body, sig, ts, nonce = _signed_callback_body(
        "<xml><MsgType>text</MsgType><Content>hi</Content></xml>", now)
    resp = await client.post(
        f"/callback/1000002?msg_signature={sig}&timestamp={ts}&nonce={nonce}",
        data=body)
    assert resp.status == 200
    assert await resp.text() == "success"
    await client.close()


@pytest.mark.asyncio
async def test_callback_rejects_bad_signature(relay_env):
    now = 1_700_000_000
    client = await _client(relay_env, now_fn=lambda: now)
    body, _, ts, nonce = _signed_callback_body(
        "<xml><MsgType>text</MsgType><Content>hi</Content></xml>", now)
    resp = await client.post(
        f"/callback/1000002?msg_signature=BADSIG&timestamp={ts}&nonce={nonce}",
        data=body)
    assert resp.status == 403
    await client.close()


@pytest.mark.asyncio
async def test_callback_rejects_stale_timestamp(relay_env):
    now = 1_700_000_000
    client = await _client(relay_env, now_fn=lambda: now)
    stale = now - 600
    body, sig, ts, nonce = _signed_callback_body(
        "<xml><MsgType>text</MsgType><Content>hi</Content></xml>", stale)
    resp = await client.post(
        f"/callback/1000002?msg_signature={sig}&timestamp={ts}&nonce={nonce}",
        data=body)
    assert resp.status == 403
    await client.close()


@pytest.mark.asyncio
async def test_callback_empty_body_rejected(relay_env):
    now = 1_700_000_000
    client = await _client(relay_env, now_fn=lambda: now)
    resp = await client.post(
        "/callback/1000002?msg_signature=x&timestamp=1&nonce=n", data="")
    assert resp.status == 403
    await client.close()


@pytest.mark.asyncio
async def test_fetch_requires_bearer_token(relay_env):
    now = 1_700_000_000
    client = await _client(relay_env, now_fn=lambda: now)
    resp = await client.post("/messages/fetch", json={"cursor": 0, "limit": 100})
    assert resp.status == 401
    await client.close()


@pytest.mark.asyncio
async def test_fetch_rejects_wrong_token(relay_env):
    now = 1_700_000_000
    client = await _client(relay_env, now_fn=lambda: now)
    resp = await client.post("/messages/fetch", json={"cursor": 0, "limit": 100},
                             headers={"Authorization": "Bearer wrong"})
    assert resp.status == 401
    await client.close()


@pytest.mark.asyncio
async def test_fetch_returns_buffered_messages(relay_env):
    now = 1_700_000_000
    client = await _client(relay_env, now_fn=lambda: now)
    body, sig, ts, nonce = _signed_callback_body(
        "<xml><MsgType>text</MsgType><Content>hi</Content></xml>", now)
    await client.post(f"/callback/1000002?msg_signature={sig}&timestamp={ts}&nonce={nonce}", data=body)

    resp = await client.post("/messages/fetch", json={"cursor": 0, "limit": 100},
                             headers={"Authorization": "Bearer fetch-secret"})
    assert resp.status == 200
    data = await resp.json()
    assert len(data["messages"]) == 1
    assert data["messages"][0]["body"] == body
    assert int(data["next_cursor"]) >= 1
    await client.close()


@pytest.mark.asyncio
async def test_health_endpoint(relay_env):
    now = 1_700_000_000
    client = await _client(relay_env, now_fn=lambda: now)
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert "queue_depth" in data
    assert "rejected_count" in data
    await client.close()


@pytest.mark.asyncio
async def test_fetch_rejects_bad_cursor(relay_env):
    now = 1_700_000_000
    client = await _client(relay_env, now_fn=lambda: now)
    resp = await client.post("/messages/fetch", json={"cursor": "abc", "limit": 100},
                             headers={"Authorization": "Bearer fetch-secret"})
    assert resp.status == 400
    await client.close()


@pytest.mark.asyncio
async def test_callback_rejects_bad_timestamp(relay_env):
    now = 1_700_000_000
    client = await _client(relay_env, now_fn=lambda: now)
    body, sig, _ts, nonce = _signed_callback_body(
        "<xml><MsgType>text</MsgType><Content>hi</Content></xml>", now)
    resp = await client.post(
        f"/callback/1000002?msg_signature={sig}&timestamp=notanumber&nonce={nonce}",
        data=body)
    assert resp.status == 403
    await client.close()


@pytest.mark.asyncio
async def test_purge_task_registered(relay_env):
    app = create_relay_app(relay_env, now_fn=lambda: 1_700_000_000, run_purge=True)
    assert len(app.on_startup) >= 1
    assert len(app.on_cleanup) >= 1


def test_relay_store_seq_not_reused_after_purge(tmp_path):
    """AUTOINCREMENT seq must not be reused after a purge — protects poller cursor.

    Regression guard for the load-bearing AUTOINCREMENT choice (see schema comment).
    """
    store = RelayStore(str(tmp_path / "relay.db"), ttl_seconds=3600)
    store.open()
    last = 0
    for i in range(3):
        last = store.put(agent_id="a", body=str(i), query_params={}, _now=1000)
    # Purge everything (all older than TTL relative to a far-future now).
    store.purge_expired(_now=1000 + 3600 + 1)
    assert store.queue_depth() == 0
    new_seq = store.put(agent_id="a", body="after-purge", query_params={})
    assert new_seq > last  # seq advanced, never reused
    store.close()


def test_relay_config_multi_agent_verify_creds(monkeypatch):
    """Multi-agent: each agent resolves its own (token, aes_key) for /callback verify."""
    monkeypatch.setenv("RELAY_FETCH_TOKEN", "fetch")
    monkeypatch.setenv("AGENT_CONFIGS",
                       '{"1000002": {"token": "t2", "aes_key": "k2"}, '
                       '"1000003": {"token": "t3", "aes_key": "k3"}}')
    monkeypatch.delenv("WECOM_TOKEN", raising=False)
    monkeypatch.delenv("WECOM_AES_KEY", raising=False)
    cfg = RelayConfig.from_env()
    assert cfg.agent_creds("1000002") == ("t2", "k2")
    assert cfg.agent_creds("1000003") == ("t3", "k3")
    assert cfg.agent_creds("1000099") == ("", "")

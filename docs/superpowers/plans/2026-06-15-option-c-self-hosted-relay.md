# Option C — Self-Hosted Secured Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement each task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the AWS API Gateway + Lambda + DynamoDB relay (flagged by AppSec finding `APIGAuthenticationCheck`) with a small self-hosted aiohttp relay on the existing always-on EC2 box, with authenticated `/callback` (WeCom signature + timestamp freshness) and authenticated `/messages/fetch` (Bearer token), backed by a short-TTL SQLite buffer.

**Architecture:** WeCom → `http://<elastic-ip>:PORT/callback/{agent_id}` → self-hosted aiohttp relay (verifies WeCom signature + 5-min timestamp freshness, stores to SQLite WAL with TTL) → local server polls `POST /messages/fetch` with `Authorization: Bearer <token>` → existing `RelayPollingSource` decrypts + dispatches. The relay's only job is to catch the callback within WeCom's 5s window and buffer it; the local server drains at its own pace. No managed AWS services remain.

**Tech Stack:** Python 3.11+, aiohttp (already a dependency), SQLite (stdlib), pycryptodome (already used). Bash + AWS CLI for infra scripts. systemd for process management.

**Key design decisions (locked):**
- **Freshness enforced at relay ingress ONLY**, not local dispatch. Enforcing locally would discard messages buffered during a local outage >5 min — reintroducing the permanent-loss problem Option C exists to avoid. The relay sits at the trust boundary where WeCom delivers within seconds, so freshness there is correct; the local poller relies on signature verification + cursor/seq monotonicity (already implemented).
- **Buffer = SQLite WAL** (not in-memory) for crash/restart durability, short TTL (default 2 days), owned by a dedicated non-root `lobster-relay` user, `0600` perms.
- **TLS deferred to Phase 2** — WeCom payloads are AES-CBC encrypted + HMAC-signed + timestamp-bound, and WeCom officially permits plain HTTP. Documented in `docs/security.md`.
- **IP allowlist is the mandatory first defense layer**; signature verification is the second. Deploy script fails closed if the relay port would be `0.0.0.0/0`.
- Local-side code change is minimal: `RelayPollingSource._fetch_messages` gains a Bearer header; `WeComConfig` gains `relay_token`.

---

## File Structure

**New files:**
- `src/remote_control/relay/__init__.py` — package marker
- `src/remote_control/relay/app.py` — the self-hosted relay aiohttp app (handlers: `/callback`, `/callback/{agent_id}`, `/messages/fetch`, `/health`), `RelayStore` (SQLite buffer), `RelayConfig` (env-driven). Single focused module — the relay is small and its parts change together.
- `src/remote_control/relay/__main__.py` — `python -m remote_control.relay` entry point (reads env, runs the app)
- `tests/test_relay_app.py` — unit tests for relay handlers, store, auth, freshness
- `scripts/setup-self-relay.sh` — provision/update the relay's EC2 inbound SG rule (WeCom IPs only), deploy relay code + systemd unit to EC2
- `scripts/audit-sg.sh` — pre-deploy gate: fail if relay port or SOCKS 1080 open to `0.0.0.0/0`
- `scripts/templates/lobster-relay.service` — systemd unit template (Restart=always, OnFailure alert)
- `scripts/relay-monitor.sh` — 1-min health-check cron script (alerts on unreachable / queue depth / 403 rate)
- `docs/self-hosted-relay.md` — deployment + operations guide for the new relay
- `docs/security.md` — auth model, TLS decision, token rotation procedure
- `docs/architecture-decisions/0001-self-hosted-relay.md` — ADR recording Option C chosen, Option A rejected (FACT 2)

**Modified files:**
- `src/remote_control/config.py` — add `relay_token: str = ""` to `WeComConfig`
- `src/remote_control/wecom/message_source.py` — `RelayPollingSource` sends `Authorization: Bearer` header; surface 401 as a clear error
- `tests/test_message_source.py` — assert auth header is sent; 401 handling
- `relay/lambda_function.py` — **DELETE** (after cutover; the file stays until teardown step)
- `scripts/setup-relay.sh` — mark deprecated (header notice pointing to setup-self-relay.sh); keep `--teardown` working for the cleanup step
- `README.md` — architecture diagram + relay section rewrite
- `DESIGN.md` — relay section: describe self-hosted relay, mark AWS relay removed
- `CLAUDE.md` — update relay description
- `relay/README.md` — rewrite: remove DynamoDB/APIGW, point to self-hosted relay
- `MEMORY.md` + memory file — record the migration

---

## Task 1: Add `relay_token` to config

**Files:**
- Modify: `src/remote_control/config.py:10-23`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_wecom_config_relay_token_defaults_empty():
    from remote_control.config import WeComConfig
    cfg = WeComConfig(
        corp_id="c", agent_id=1, secret="s", token="t", encoding_aes_key="k",
    )
    assert cfg.relay_token == ""


def test_wecom_config_relay_token_set():
    from remote_control.config import WeComConfig
    cfg = WeComConfig(
        corp_id="c", agent_id=1, secret="s", token="t", encoding_aes_key="k",
        relay_token="bearer-secret-xyz",
    )
    assert cfg.relay_token == "bearer-secret-xyz"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py::test_wecom_config_relay_token_set -v`
Expected: PASS already if pydantic ignores extra — NO. pydantic BaseModel rejects unknown kwargs by default → FAIL with validation error on `relay_token`.

- [ ] **Step 3: Add the field**

In `src/remote_control/config.py`, in `WeComConfig`, after the `relay_poll_interval_seconds` line (line 19):

```python
    relay_token: str = ""  # Bearer token for authenticating to the self-hosted relay's /messages/fetch
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/remote_control/config.py tests/test_config.py
git commit -m "feat(config): add relay_token to WeComConfig for self-hosted relay auth"
```

---

## Task 2: `RelayStore` — SQLite buffer with TTL

**Files:**
- Create: `src/remote_control/relay/__init__.py`
- Create: `src/remote_control/relay/app.py` (RelayStore portion)
- Test: `tests/test_relay_app.py`

- [ ] **Step 1: Create the package marker**

Create `src/remote_control/relay/__init__.py`:

```python
"""Self-hosted WeCom relay — replaces the AWS API Gateway + Lambda + DynamoDB stack."""
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_relay_app.py`:

```python
"""Tests for the self-hosted relay app."""

import pytest

from remote_control.relay.app import RelayStore


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
    # Insert a row that is already expired by stamping created_at in the past.
    store.put(agent_id="a", body="old", query_params={}, _now=1000)
    store.put(agent_id="a", body="new", query_params={}, _now=10_000_000_000)
    # Purge relative to a now well past the first row's TTL but before the second's.
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_relay_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'remote_control.relay.app'`

- [ ] **Step 4: Implement `RelayStore`**

Create `src/remote_control/relay/app.py` with this content (handlers added in later tasks):

```python
"""Self-hosted WeCom relay app.

Replaces the AWS API Gateway + Lambda + DynamoDB relay. Receives raw WeCom
callbacks, verifies the WeCom signature + timestamp freshness, buffers the raw
encrypted message in a short-TTL SQLite store, and serves it to the local
server's poller over an authenticated /messages/fetch endpoint.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL,
    query_params TEXT NOT NULL DEFAULT '{}',
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
"""


class RelayStore:
    """SQLite-backed buffer for raw WeCom callbacks. WAL mode, short TTL."""

    def __init__(self, db_path: str, ttl_seconds: int = 172800):
        self._db_path = db_path
        self._ttl = ttl_seconds
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("RelayStore not opened")
        return self._conn

    def put(self, agent_id: str, body: str, query_params: dict, _now: int | None = None) -> int:
        now = _now if _now is not None else int(time.time())
        cur = self.conn.execute(
            "INSERT INTO messages (agent_id, body, query_params, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, body, json.dumps(query_params), now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def fetch(self, cursor: int, limit: int) -> tuple[list[dict], str]:
        rows = self.conn.execute(
            "SELECT seq, agent_id, body, query_params FROM messages "
            "WHERE seq > ? ORDER BY seq ASC LIMIT ?",
            (cursor, limit),
        ).fetchall()
        messages = []
        next_cursor = cursor
        for row in rows:
            messages.append({
                "msg_id": str(row["seq"]),
                "seq": int(row["seq"]),
                "query_params": json.loads(row["query_params"]),
                "body": row["body"],
                "agent_id": row["agent_id"],
            })
            next_cursor = int(row["seq"])
        return messages, str(next_cursor)

    def purge_expired(self, _now: int | None = None) -> int:
        now = _now if _now is not None else int(time.time())
        cutoff = now - self._ttl
        cur = self.conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    def queue_depth(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
        return int(row["n"])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_relay_app.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add src/remote_control/relay/__init__.py src/remote_control/relay/app.py tests/test_relay_app.py
git commit -m "feat(relay): add RelayStore SQLite buffer with TTL"
```

---

## Task 3: `RelayConfig` — env-driven multi-agent config

**Files:**
- Modify: `src/remote_control/relay/app.py`
- Test: `tests/test_relay_app.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_relay_app.py`:

```python
from remote_control.relay.app import RelayConfig


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_relay_app.py -k relay_config -v`
Expected: FAIL with `ImportError: cannot import name 'RelayConfig'`

- [ ] **Step 3: Implement `RelayConfig`**

Add to `src/remote_control/relay/app.py` (after imports, before `RelayStore`):

```python
import os
from dataclasses import dataclass


@dataclass
class RelayConfig:
    fetch_token: str
    ttl_days: int
    db_path: str
    freshness_seconds: int
    legacy_token: str
    legacy_aes_key: str
    agent_configs: dict

    @classmethod
    def from_env(cls) -> "RelayConfig":
        fetch_token = os.environ.get("RELAY_FETCH_TOKEN", "")
        if not fetch_token:
            raise ValueError("RELAY_FETCH_TOKEN environment variable is required")
        agent_configs = {}
        raw = os.environ.get("AGENT_CONFIGS", "")
        if raw:
            try:
                agent_configs = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("AGENT_CONFIGS is not valid JSON; ignoring")
        return cls(
            fetch_token=fetch_token,
            ttl_days=int(os.environ.get("TTL_DAYS", "2")),
            db_path=os.environ.get("RELAY_DB_PATH", "/var/lib/lobster-relay/relay.db"),
            freshness_seconds=int(os.environ.get("FRESHNESS_SECONDS", "300")),
            legacy_token=os.environ.get("WECOM_TOKEN", ""),
            legacy_aes_key=os.environ.get("WECOM_AES_KEY", ""),
            agent_configs=agent_configs,
        )

    def agent_creds(self, agent_id: str) -> tuple[str, str]:
        if agent_id and agent_id in self.agent_configs:
            c = self.agent_configs[agent_id]
            return c.get("token", ""), c.get("aes_key", "")
        return self.legacy_token, self.legacy_aes_key
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_relay_app.py -k relay_config -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/remote_control/relay/app.py tests/test_relay_app.py
git commit -m "feat(relay): add RelayConfig env-driven config with multi-agent support"
```

---

## Task 4: Relay handlers — `/callback` with signature + freshness verification

**Files:**
- Modify: `src/remote_control/relay/app.py`
- Test: `tests/test_relay_app.py`

The relay reuses the existing `remote_control.wecom.crypto.verify_signature` and `parse_message_xml` (DRY — do not re-implement crypto).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_relay_app.py`:

```python
from aiohttp.test_utils import TestClient, TestServer
from remote_control.relay.app import create_relay_app
from remote_control.wecom.crypto import encrypt_message, make_signature

R_AES = "kWxPEV2UEDyxWpmPB8jfIqLfNjGjRiIpG2lMGKEQCTm"
R_TOKEN = "relay_wecom_token"
R_CORP = "relay_corp"


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
    # Sign with a timestamp 10 minutes in the past — beyond the 300s window.
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_relay_app.py -k callback -v`
Expected: FAIL with `ImportError: cannot import name 'create_relay_app'`

- [ ] **Step 3: Implement the callback handler and `create_relay_app`**

Add to `src/remote_control/relay/app.py`:

```python
from aiohttp import web

from remote_control.wecom.crypto import parse_message_xml, verify_signature


def create_relay_app(config: RelayConfig, now_fn=None, store: "RelayStore | None" = None) -> web.Application:
    """Build the relay aiohttp app. now_fn injectable for tests."""
    now_fn = now_fn or (lambda: int(time.time()))
    if store is None:
        store = RelayStore(config.db_path, ttl_seconds=config.ttl_days * 86400)
        store.open()

    async def handle_callback(request: web.Request) -> web.Response:
        agent_id = request.match_info.get("agent_id", "")
        params = request.query
        msg_signature = params.get("msg_signature", "")
        timestamp = params.get("timestamp", "")
        nonce = params.get("nonce", "")
        body = await request.text()
        if not body:
            return web.Response(status=403, text="empty body")

        token, _aes = config.agent_creds(agent_id)
        if not token:
            return web.Response(status=403, text="unknown agent")

        # Timestamp freshness — replay protection at the trust boundary.
        try:
            ts_int = int(timestamp)
        except ValueError:
            return web.Response(status=403, text="bad timestamp")
        if abs(now_fn() - ts_int) > config.freshness_seconds:
            logger.warning("Rejected stale callback (agent=%s, age=%ss)", agent_id, now_fn() - ts_int)
            return web.Response(status=403, text="stale")

        # Verify WeCom signature over the encrypted payload.
        outer = parse_message_xml(body)
        encrypt = outer.get("Encrypt", "")
        if not encrypt or not verify_signature(token, timestamp, nonce, encrypt, msg_signature):
            return web.Response(status=403, text="invalid signature")

        store.put(agent_id=agent_id, body=body, query_params={
            "msg_signature": msg_signature, "timestamp": timestamp, "nonce": nonce,
        })
        return web.Response(text="success")

    async def handle_verify(request: web.Request) -> web.Response:
        # WeCom URL verification (GET) — echo decrypted echostr.
        from remote_control.wecom.crypto import decrypt_message
        agent_id = request.match_info.get("agent_id", "")
        params = request.query
        token, aes_key = config.agent_creds(agent_id)
        if not token:
            return web.Response(status=403, text="unknown agent")
        echostr = params.get("echostr", "")
        if not verify_signature(token, params.get("timestamp", ""),
                                params.get("nonce", ""), echostr,
                                params.get("msg_signature", "")):
            return web.Response(status=403, text="invalid signature")
        decrypted = decrypt_message(aes_key, echostr)
        return web.Response(text=decrypted.content)

    app = web.Application()
    app["relay_store"] = store
    app["relay_config"] = config
    app["now_fn"] = now_fn
    app.router.add_get("/callback/{agent_id}", handle_verify)
    app.router.add_post("/callback/{agent_id}", handle_callback)
    app.router.add_get("/callback", handle_verify)
    app.router.add_post("/callback", handle_callback)
    return app
```

Note: routes without `{agent_id}` need `match_info.get("agent_id", "")` to return `""` — aiohttp handles this since the param is simply absent. The legacy-single-agent path falls back via `agent_creds("")`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_relay_app.py -k callback -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/remote_control/relay/app.py tests/test_relay_app.py
git commit -m "feat(relay): add /callback handler with signature + freshness verification"
```

---

## Task 5: `/messages/fetch` with Bearer auth + `/health`

**Files:**
- Modify: `src/remote_control/relay/app.py`
- Test: `tests/test_relay_app.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_relay_app.py`:

```python
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
    # Store a valid message via the callback path first.
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_relay_app.py -k "fetch or health" -v`
Expected: FAIL — routes not registered (404), so assertions on 401/200 fail.

- [ ] **Step 3: Implement fetch + health handlers**

In `create_relay_app`, add a rejection counter and the handlers, then register routes. Add near the top of `create_relay_app` after `now_fn` setup:

```python
    stats = {"rejected": 0, "last_callback_ts": 0}
```

In `handle_callback`, increment `stats["rejected"]` on each 403 return path and set `stats["last_callback_ts"] = now_fn()` on success. Then add:

```python
    async def handle_fetch(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {config.fetch_token}"
        if auth != expected:
            return web.Response(status=401, text="unauthorized")
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        cursor = int(payload.get("cursor") or 0)
        limit = min(int(payload.get("limit", 100)), 100)
        messages, next_cursor = store.fetch(cursor=cursor, limit=limit)
        return web.json_response({"messages": messages, "next_cursor": next_cursor})

    async def handle_health(request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "queue_depth": store.queue_depth(),
            "last_callback_ts": stats["last_callback_ts"],
            "rejected_count": stats["rejected"],
        })
```

Register in the app:

```python
    app.router.add_post("/messages/fetch", handle_fetch)
    app.router.add_get("/health", handle_health)
```

Update `handle_callback`'s 403 returns to increment `stats["rejected"]` (e.g. a small local helper `def _reject(text): stats["rejected"] += 1; return web.Response(status=403, text=text)` and use it for every 403 path).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_relay_app.py -v`
Expected: PASS (all relay tests)

- [ ] **Step 5: Commit**

```bash
git add src/remote_control/relay/app.py tests/test_relay_app.py
git commit -m "feat(relay): add authenticated /messages/fetch and /health endpoints"
```

---

## Task 6: Relay entry point with periodic TTL purge

**Files:**
- Create: `src/remote_control/relay/__main__.py`
- Test: `tests/test_relay_app.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_relay_app.py`:

```python
@pytest.mark.asyncio
async def test_purge_task_registered(relay_env):
    """create_relay_app with run_purge=True registers a cleanup background task on startup."""
    app = create_relay_app(relay_env, now_fn=lambda: 1_700_000_000, run_purge=True)
    assert len(app.on_startup) >= 1
    assert len(app.on_cleanup) >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_relay_app.py -k purge -v`
Expected: FAIL — `create_relay_app() got an unexpected keyword argument 'run_purge'`

- [ ] **Step 3: Add purge background task support**

In `create_relay_app`, add `run_purge: bool = False` to the signature. Before `return app`:

```python
    if run_purge:
        import asyncio

        async def _purge_loop(_app):
            async def loop():
                while True:
                    try:
                        store.purge_expired()
                    except Exception:
                        logger.exception("purge failed")
                    await asyncio.sleep(3600)
            _app["_purge_task"] = asyncio.create_task(loop())

        async def _stop_purge(_app):
            t = _app.get("_purge_task")
            if t:
                t.cancel()

        app.on_startup.append(_purge_loop)
        app.on_cleanup.append(_stop_purge)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_relay_app.py -k purge -v`
Expected: PASS

- [ ] **Step 5: Create the entry point**

Create `src/remote_control/relay/__main__.py`:

```python
"""Run the self-hosted WeCom relay: python -m remote_control.relay"""

import logging
import os

from aiohttp import web

from remote_control.relay.app import RelayConfig, create_relay_app


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = RelayConfig.from_env()
    app = create_relay_app(config, run_purge=True)
    host = os.environ.get("RELAY_HOST", "0.0.0.0")
    port = int(os.environ.get("RELAY_PORT", "8443"))
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run full relay test suite**

Run: `python -m pytest tests/test_relay_app.py -v`
Expected: PASS (all)

- [ ] **Step 7: Commit**

```bash
git add src/remote_control/relay/app.py src/remote_control/relay/__main__.py tests/test_relay_app.py
git commit -m "feat(relay): add entry point and periodic TTL purge"
```

---

## Task 7: Local poller sends Bearer token

**Files:**
- Modify: `src/remote_control/wecom/message_source.py:161-168` (`_fetch_messages`)
- Modify: `src/remote_control/server.py:45-49` (pass `relay_token`)
- Test: `tests/test_message_source.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_message_source.py`:

```python
@pytest.mark.asyncio
async def test_relay_source_sends_bearer_token(wecom_config, on_message, mock_store):
    """_fetch_messages includes Authorization: Bearer when a token is configured."""
    import httpx
    from unittest.mock import patch

    cfg = wecom_config.model_copy(update={"relay_token": "secret-bearer"})
    source = RelayPollingSource(cfg, "http://relay.example.com", on_message, store=mock_store)

    captured = {}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"messages": [], "next_cursor": ""}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            captured["headers"] = headers
            return FakeResp()

    with patch.object(httpx, "AsyncClient", FakeClient):
        await source._fetch_messages("http://relay.example.com/messages/fetch", {"cursor": "", "limit": 100})

    assert captured["headers"]["Authorization"] == "Bearer secret-bearer"


@pytest.mark.asyncio
async def test_relay_source_no_auth_header_without_token(wecom_config, on_message, mock_store):
    """No Authorization header when relay_token is empty (backwards compatible)."""
    import httpx
    from unittest.mock import patch

    source = RelayPollingSource(wecom_config, "http://relay.example.com", on_message, store=mock_store)
    captured = {}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"messages": [], "next_cursor": ""}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            captured["headers"] = headers
            return FakeResp()

    with patch.object(httpx, "AsyncClient", FakeClient):
        await source._fetch_messages("http://relay.example.com/messages/fetch", {"cursor": "", "limit": 100})

    assert not captured["headers"]  # empty dict
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_message_source.py -k bearer -v`
Expected: FAIL — `_fetch_messages` does not send headers (captured["headers"] is None or missing Authorization).

- [ ] **Step 3: Update `_fetch_messages` and the constructor**

In `src/remote_control/wecom/message_source.py`, `RelayPollingSource.__init__`, after `self._config = config` add:

```python
        self._relay_token = getattr(config, "relay_token", "")
```

Replace `_fetch_messages` (lines 161-168):

```python
    async def _fetch_messages(self, url: str, payload: dict) -> dict:
        """HTTP POST to the relay. Separated for testability."""
        import httpx

        headers = {}
        if self._relay_token:
            headers["Authorization"] = f"Bearer {self._relay_token}"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        return resp.json()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_message_source.py -v`
Expected: PASS (all message source tests, including the existing ones)

- [ ] **Step 5: Commit**

```bash
git add src/remote_control/wecom/message_source.py tests/test_message_source.py
git commit -m "feat(relay): local poller authenticates to relay with Bearer token"
```

---

## Task 8: `audit-sg.sh` — pre-deploy SG gate

**Files:**
- Create: `scripts/audit-sg.sh`
- Test: `tests/test_audit_sg.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_audit_sg.py`:

```python
"""Tests for scripts/audit-sg.sh using a fake aws CLI on PATH."""

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "audit-sg.sh"


def _fake_aws(tmp_path, ip_permissions_json: str) -> dict:
    """Create a fake `aws` executable that prints the given SG ingress JSON."""
    fake = tmp_path / "aws"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f"cat <<'EOF'\n{ip_permissions_json}\nEOF\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    return env


def test_audit_passes_when_ports_restricted(tmp_path):
    # Relay port 8443 and SOCKS 1080 restricted to a specific CIDR.
    perms = '[{"FromPort":8443,"ToPort":8443,"IpRanges":[{"CidrIp":"1.2.3.4/32"}]},' \
            '{"FromPort":1080,"ToPort":1080,"IpRanges":[{"CidrIp":"1.2.3.4/32"}]}]'
    env = _fake_aws(tmp_path, perms)
    r = subprocess.run(["bash", str(SCRIPT), "--sg-id", "sg-1", "--relay-port", "8443"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_audit_fails_when_relay_port_open(tmp_path):
    perms = '[{"FromPort":8443,"ToPort":8443,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'
    env = _fake_aws(tmp_path, perms)
    r = subprocess.run(["bash", str(SCRIPT), "--sg-id", "sg-1", "--relay-port", "8443"],
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0
    assert "0.0.0.0/0" in (r.stdout + r.stderr)


def test_audit_fails_when_socks_port_open(tmp_path):
    perms = '[{"FromPort":1080,"ToPort":1080,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'
    env = _fake_aws(tmp_path, perms)
    r = subprocess.run(["bash", str(SCRIPT), "--sg-id", "sg-1", "--relay-port", "8443"],
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0
    assert "1080" in (r.stdout + r.stderr)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_audit_sg.py -v`
Expected: FAIL — script does not exist (non-zero from bash "No such file").

- [ ] **Step 3: Implement `scripts/audit-sg.sh`**

Create `scripts/audit-sg.sh`:

```bash
#!/usr/bin/env bash
#
# Pre-deploy security gate: fail if the relay port or SOCKS port 1080 is
# exposed to 0.0.0.0/0 in the given security group.
#
# Usage:
#   ./scripts/audit-sg.sh --sg-id sg-xxxx [--relay-port 8443] [--region ap-southeast-1]
#
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-ap-southeast-1}"
SG_ID=""
RELAY_PORT="8443"
SOCKS_PORT="1080"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sg-id)      SG_ID="$2"; shift 2 ;;
        --relay-port) RELAY_PORT="$2"; shift 2 ;;
        --region)     REGION="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 2 ;;
    esac
done

[ -z "$SG_ID" ] && { echo "ERROR: --sg-id required"; exit 2; }

PERMS=$(aws --region "$REGION" ec2 describe-security-groups \
    --group-ids "$SG_ID" \
    --query "SecurityGroups[0].IpPermissions" --output json)

FAIL=0
check_port() {
    local port="$1" label="$2"
    local open
    open=$(echo "$PERMS" | python3 -c "
import sys, json
perms = json.load(sys.stdin)
port = int('$port')
bad = False
for p in perms:
    fr, to = p.get('FromPort'), p.get('ToPort')
    if fr is None or to is None:
        continue
    if fr <= port <= to:
        for r in p.get('IpRanges', []):
            if r.get('CidrIp') == '0.0.0.0/0':
                bad = True
print('OPEN' if bad else 'OK')
")
    if [ "$open" = "OPEN" ]; then
        echo "  FAIL: $label (port $port) is open to 0.0.0.0/0"
        FAIL=1
    else
        echo "  OK:   $label (port $port) is not open to 0.0.0.0/0"
    fi
}

echo "=== Security Group Audit ($SG_ID) ==="
check_port "$RELAY_PORT" "relay callback port"
check_port "$SOCKS_PORT" "SOCKS proxy port"

if [ "$FAIL" -ne 0 ]; then
    echo ""
    echo "AUDIT FAILED — refusing to proceed. Restrict the offending port to WeCom IP ranges."
    exit 1
fi
echo "Audit passed."
```

Make executable:

```bash
chmod +x scripts/audit-sg.sh
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_audit_sg.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/audit-sg.sh tests/test_audit_sg.py
git commit -m "feat(scripts): add audit-sg.sh pre-deploy gate for open ports"
```

---

## Task 9: `setup-self-relay.sh` — provision relay SG (WeCom IPs) + deploy

**Files:**
- Create: `scripts/setup-self-relay.sh`
- Create: `scripts/templates/lobster-relay.service`

This script is operational glue (AWS SG + scp + systemd). It is not unit-tested in CI (requires AWS + a live host); validate with `bash -n` and a `--dry-run` mode that prints the planned actions. The SG rule creation reuses the WeCom `getcallbackip` API.

- [ ] **Step 1: Create the systemd unit template**

Create `scripts/templates/lobster-relay.service`:

```ini
[Unit]
Description=lobster-cc self-hosted WeCom relay
After=network.target

[Service]
Type=simple
User=lobster-relay
Group=lobster-relay
Environment=RELAY_FETCH_TOKEN=__FETCH_TOKEN__
Environment=WECOM_TOKEN=__WECOM_TOKEN__
Environment=WECOM_AES_KEY=__WECOM_AES_KEY__
Environment=AGENT_CONFIGS=__AGENT_CONFIGS__
Environment=RELAY_DB_PATH=/var/lib/lobster-relay/relay.db
Environment=RELAY_PORT=__RELAY_PORT__
Environment=TTL_DAYS=2
Environment=FRESHNESS_SECONDS=300
ExecStart=/usr/bin/python3 -m remote_control.relay
WorkingDirectory=/opt/lobster-relay
Restart=always
RestartSec=10
OnFailure=lobster-relay-alert@%n.service

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Create `scripts/setup-self-relay.sh`**

Create `scripts/setup-self-relay.sh`:

```bash
#!/usr/bin/env bash
#
# Provision and deploy the self-hosted WeCom relay onto an existing EC2 box.
# Replaces the AWS API Gateway + Lambda + DynamoDB relay.
#
# What it does:
#   1. Reads WeCom callback IP ranges (getcallbackip) — passed via --wecom-ips
#   2. Opens the relay port in the EC2 security group RESTRICTED to those IPs
#   3. Runs scripts/audit-sg.sh as a gate (fails if port would be 0.0.0.0/0)
#   4. Copies relay code + installs systemd unit on the EC2 host
#
# Usage:
#   ./scripts/setup-self-relay.sh \
#       --host ec2-user@<elastic-ip> --sg-id sg-xxxx \
#       --relay-port 8443 --fetch-token <secret> \
#       --wecom-ips "1.2.3.0/24,5.6.7.8/32" \
#       --ssh-key ~/.ssh/rc-proxy-key.pem [--region ap-southeast-1] [--dry-run]
#
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-ap-southeast-1}"
HOST=""; SG_ID=""; RELAY_PORT="8443"; FETCH_TOKEN=""; WECOM_IPS=""
SSH_KEY="$HOME/.ssh/rc-proxy-key.pem"; DRY_RUN=false
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --sg-id) SG_ID="$2"; shift 2 ;;
        --relay-port) RELAY_PORT="$2"; shift 2 ;;
        --fetch-token) FETCH_TOKEN="$2"; shift 2 ;;
        --wecom-ips) WECOM_IPS="$2"; shift 2 ;;
        --ssh-key) SSH_KEY="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        *) echo "Unknown arg: $1"; exit 2 ;;
    esac
done

for req in HOST SG_ID FETCH_TOKEN WECOM_IPS; do
    [ -z "${!req}" ] && { echo "ERROR: --${req,,} required"; exit 2; }
done

AWS="aws --region $REGION --output text"
run() { if [ "$DRY_RUN" = true ]; then echo "  + $*"; else eval "$@"; fi; }

echo "=== Self-Hosted Relay Setup ==="
echo "  Host: $HOST   SG: $SG_ID   Port: $RELAY_PORT   Region: $REGION"
echo ""

echo "[1/4] Opening relay port to WeCom IP ranges (not 0.0.0.0/0)..."
IFS=',' read -ra CIDRS <<< "$WECOM_IPS"
for cidr in "${CIDRS[@]}"; do
    cidr="$(echo "$cidr" | xargs)"
    [ "$cidr" = "0.0.0.0/0" ] && { echo "  REFUSED: will not open to 0.0.0.0/0"; exit 1; }
    echo "  Authorizing $cidr -> tcp/$RELAY_PORT"
    run "$AWS ec2 authorize-security-group-ingress --group-id $SG_ID \
        --protocol tcp --port $RELAY_PORT --cidr $cidr 2>/dev/null || true"
done

echo "[2/4] Running SG audit gate..."
if [ "$DRY_RUN" = false ]; then
    "$SCRIPT_DIR/audit-sg.sh" --sg-id "$SG_ID" --relay-port "$RELAY_PORT" --region "$REGION"
fi

echo "[3/4] Deploying relay code + systemd unit to $HOST..."
SVC=$(sed -e "s|__FETCH_TOKEN__|$FETCH_TOKEN|" \
          -e "s|__RELAY_PORT__|$RELAY_PORT|" \
          "$SCRIPT_DIR/templates/lobster-relay.service")
if [ "$DRY_RUN" = true ]; then
    echo "  + rsync relay code to $HOST:/opt/lobster-relay"
    echo "  + install systemd unit lobster-relay.service"
else
    ssh -i "$SSH_KEY" "$HOST" "sudo useradd -r -s /usr/sbin/nologin lobster-relay 2>/dev/null || true; \
        sudo mkdir -p /opt/lobster-relay /var/lib/lobster-relay; \
        sudo chown lobster-relay:lobster-relay /var/lib/lobster-relay; \
        sudo chmod 700 /var/lib/lobster-relay"
    rsync -az -e "ssh -i $SSH_KEY" \
        "$PROJECT_DIR/src/remote_control" "$HOST:/tmp/lobster-relay-src/"
    ssh -i "$SSH_KEY" "$HOST" "sudo cp -r /tmp/lobster-relay-src/remote_control /opt/lobster-relay/"
    echo "$SVC" | ssh -i "$SSH_KEY" "$HOST" "sudo tee /etc/systemd/system/lobster-relay.service > /dev/null"
    ssh -i "$SSH_KEY" "$HOST" "sudo systemctl daemon-reload && sudo systemctl enable --now lobster-relay"
fi

echo "[4/4] Done."
echo ""
echo "  WeCom callback URL:  http://<elastic-ip>:$RELAY_PORT/callback/<agent_id>"
echo "  Local config.yaml:"
echo "    wecom:"
echo "      mode: \"relay\""
echo "      relay_url: \"http://<elastic-ip>:$RELAY_PORT\""
echo "      relay_token: \"$FETCH_TOKEN\""
```

Make executable:

```bash
chmod +x scripts/setup-self-relay.sh
```

- [ ] **Step 3: Syntax-check the scripts**

Run: `bash -n scripts/setup-self-relay.sh && bash -n scripts/audit-sg.sh && echo OK`
Expected: `OK`

- [ ] **Step 4: Dry-run smoke test**

Run: `bash scripts/setup-self-relay.sh --host ec2-user@1.2.3.4 --sg-id sg-test --fetch-token tok --wecom-ips "1.2.3.0/24" --dry-run`
Expected: prints planned `+` actions, no AWS calls, exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/setup-self-relay.sh scripts/templates/lobster-relay.service
git commit -m "feat(scripts): add setup-self-relay.sh with WeCom-IP-restricted SG + systemd"
```

---

## Task 10: `relay-monitor.sh` — health-check cron with WeCom alerts

**Files:**
- Create: `scripts/relay-monitor.sh`

Operational script. Validate with `bash -n` and a self-test mode.

- [ ] **Step 1: Create `scripts/relay-monitor.sh`**

Create `scripts/relay-monitor.sh`:

```bash
#!/usr/bin/env bash
#
# Health-check the self-hosted relay and alert (stdout/exit code) on problems.
# Intended to run via cron every minute. Wire alerting to a WeCom-sending hook.
#
# Usage:
#   ./scripts/relay-monitor.sh --url http://127.0.0.1:8443 \
#       [--max-queue 100] [--max-rejected-rate 10]
#
# Exit codes: 0 healthy, 1 unreachable, 2 threshold breached.
#
set -euo pipefail

URL="http://127.0.0.1:8443"
MAX_QUEUE=100
MAX_REJECTED=600   # per 60s poll * 10/min threshold equivalent; tune as needed

while [[ $# -gt 0 ]]; do
    case "$1" in
        --url) URL="$2"; shift 2 ;;
        --max-queue) MAX_QUEUE="$2"; shift 2 ;;
        --max-rejected-rate) MAX_REJECTED="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 2 ;;
    esac
done

RESP=$(curl -sf --max-time 5 "$URL/health" 2>/dev/null) || {
    echo "ALERT: relay unreachable at $URL/health"
    exit 1
}

QD=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('queue_depth',0))")
RJ=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('rejected_count',0))")

if [ "$QD" -gt "$MAX_QUEUE" ]; then
    echo "ALERT: relay queue depth $QD exceeds $MAX_QUEUE (local poller may be down)"
    exit 2
fi
if [ "$RJ" -gt "$MAX_REJECTED" ]; then
    echo "ALERT: relay rejected_count $RJ exceeds $MAX_REJECTED (possible attack / config drift)"
    exit 2
fi
echo "OK: queue_depth=$QD rejected_count=$RJ"
```

Make executable:

```bash
chmod +x scripts/relay-monitor.sh
```

- [ ] **Step 2: Syntax check**

Run: `bash -n scripts/relay-monitor.sh && echo OK`
Expected: `OK`

- [ ] **Step 3: Unreachable-path smoke test**

Run: `bash scripts/relay-monitor.sh --url http://127.0.0.1:1 ; echo "exit=$?"`
Expected: prints "ALERT: relay unreachable", `exit=1`

- [ ] **Step 4: Commit**

```bash
git add scripts/relay-monitor.sh
git commit -m "feat(scripts): add relay-monitor.sh health-check for cron alerting"
```

---

## Task 11: Documentation — security, ops, ADR, README/DESIGN/CLAUDE

**Files:**
- Create: `docs/self-hosted-relay.md`
- Create: `docs/security.md`
- Create: `docs/architecture-decisions/0001-self-hosted-relay.md`
- Modify: `README.md`, `DESIGN.md`, `CLAUDE.md`, `relay/README.md`, `scripts/setup-relay.sh` (deprecation header)

- [ ] **Step 1: Write `docs/architecture-decisions/0001-self-hosted-relay.md`**

```markdown
# ADR 0001: Self-Hosted Relay (replace AWS API Gateway + Lambda + DynamoDB)

## Status
Accepted — 2026-06-15

## Context
AppSec finding `APIGAuthenticationCheck` flagged two unauthenticated API Gateway
POST endpoints (`/messages/fetch` drains the queue; `/callback` stores any body
without verifying the WeCom signature). We were required to remove API Gateway
and DynamoDB.

WeCom callback delivery (official doc, path/90930): 5s response deadline, 3
retries on connection-failure/timeout only, short undocumented retry window,
dropped events permanently lost, no fetch-later API. Official guidance: do not
hard-depend on callbacks.

## Decision
Adopt **Option C**: a small self-hosted aiohttp relay on the existing always-on
EC2 box, buffering raw callbacks in short-TTL SQLite, served to the local poller
over an authenticated `/messages/fetch`. The `/callback` endpoint verifies the
WeCom signature + a 5-minute timestamp freshness window. The SG opens the relay
port only to WeCom IP ranges.

## Rejected: Option A (reverse SSH tunnel, no buffer)
A reverse tunnel from desktop to EC2, processing callbacks inline with no store,
would also remove all managed AWS services. Rejected because of the retry policy
above: if the desktop/tunnel is down for even seconds during WeCom's short retry
window, the message is permanently lost. The cloud buffer is the whole point.

## Consequences
- Freshness is enforced at the relay ingress ONLY, never at local dispatch —
  enforcing it locally would discard messages legitimately buffered during a
  local outage, reintroducing the loss problem we are avoiding.
- We now operate a relay process, but on infra we control, with a far smaller
  blast radius than a 7-day DynamoDB table.
- TLS is deferred (payloads are AES+HMAC+timestamp protected; WeCom permits HTTP).
```

- [ ] **Step 2: Write `docs/security.md`**

Document: the two-layer auth model (IP allowlist first, signature/Bearer second), the plain-HTTP decision with rationale, the timestamp-freshness placement, and the manual Bearer-token rotation procedure (regenerate 32-byte secret, update EC2 systemd env + restart relay, update local `config.yaml` `relay_token` + restart server).

- [ ] **Step 3: Write `docs/self-hosted-relay.md`**

Document: architecture diagram, `setup-self-relay.sh` usage, getting WeCom IPs from `getcallbackip`, systemd management commands, `relay-monitor.sh` cron setup, health endpoint fields, cutover procedure (stop local server before repointing WeCom URL to avoid split-brain), teardown of old AWS relay.

- [ ] **Step 4: Update `README.md`** — replace the relay-mode architecture diagram/description (lines ~168-183 and the relay mode bullet) to describe the self-hosted relay. Point setup at `setup-self-relay.sh`.

- [ ] **Step 5: Update `DESIGN.md`** — in the relay section, describe the self-hosted relay; note the AWS API Gateway/Lambda/DynamoDB stack was removed (AppSec finding), reference the ADR.

- [ ] **Step 6: Update `CLAUDE.md`** — update the "Message source modes" relay bullet to describe the self-hosted EC2 relay instead of AWS Lambda relay.

- [ ] **Step 7: Rewrite `relay/README.md`** — remove DynamoDB/API Gateway/Lambda specifics; describe the self-hosted relay and point to `docs/self-hosted-relay.md`. Keep a short "Legacy AWS relay (removed)" note with the teardown command for anyone with old infra.

- [ ] **Step 8: Add deprecation header to `scripts/setup-relay.sh`** — at the top (after the shebang/comment block), echo a deprecation warning when run WITHOUT `--teardown`, pointing to `setup-self-relay.sh`. Keep `--teardown` fully functional.

```bash
if [ "$TEARDOWN" != true ]; then
    echo "WARNING: setup-relay.sh (AWS Lambda relay) is DEPRECATED and flagged by AppSec."
    echo "         Use scripts/setup-self-relay.sh (self-hosted relay) instead."
    echo "         This script remains only for --teardown of legacy resources."
    echo ""
fi
```

- [ ] **Step 9: Commit**

```bash
git add docs/ README.md DESIGN.md CLAUDE.md relay/README.md scripts/setup-relay.sh
git commit -m "docs: document self-hosted relay, security model, ADR; deprecate AWS relay"
```

---

## Task 12: Full test run + lint

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS (existing + new). No regressions in `test_message_source.py`, `test_config.py`, `test_server.py`.

- [ ] **Step 2: Lint**

Run: `ruff check src/ tests/ scripts/ 2>/dev/null || ruff check src/ tests/`
Expected: no errors. Fix any introduced by new code.

- [ ] **Step 3: Commit any lint fixes**

```bash
git add -A
git commit -m "chore: lint fixes for self-hosted relay"
```

---

## Task 13: AWS teardown (DEFERRED — requires explicit user approval)

**This task is NOT executed during coding. It is the deployment/cutover runbook, run only with the user's explicit go-ahead, because it is a breaking change to live infrastructure.**

Cutover order (zero message loss):
1. Provision/deploy self-hosted relay: `scripts/setup-self-relay.sh ...` (creates SG rule restricted to WeCom IPs, deploys systemd unit).
2. **Stop the local server** (prevents split-brain where new messages go to the new relay while the poller still reads the old one).
3. Repoint WeCom admin-console callback URL → `http://<elastic-ip>:<port>/callback/<agent_id>`. WeCom issues a GET verify; the relay must be running.
4. Update local `config.yaml`: `relay_url` → new relay, add `relay_token`. Restart local server.
5. End-to-end test: send a WeCom message, confirm a reply.
6. **Wait out the old DynamoDB 7-day TTL** so any in-flight messages drain. Keep old infra ~1 week as rollback (revert config + WeCom URL).
7. Tear down legacy AWS resources: `scripts/setup-relay.sh --teardown` (deletes API Gateway + Lambda + IAM role + DynamoDB).
8. Verify the AppSec endpoints are gone: `curl -sS -o /dev/null -w "%{http_code}" -X POST https://zg68gf3ep7.execute-api.ap-southeast-1.amazonaws.com/messages/fetch` → expect connection failure / 404 (API Gateway deleted). Do NOT click "Request Verification Of Fix".

Rollback: revert WeCom callback URL + local `config.yaml` to the old relay; old DynamoDB buffer (7-day TTL) still has messages.

---

## Self-Review Checklist (run after implementing)

1. **Spec coverage:** finding resolved (APIGW+DynamoDB removed), `/callback` signature+freshness ✓, `/messages/fetch` Bearer ✓, SG WeCom-IPs + audit gate ✓, SQLite buffer ✓, monitoring/health ✓, non-root user ✓, docs+ADR ✓, teardown runbook ✓.
2. **Freshness placement:** relay ingress only — verified no freshness check added to local dispatch path.
3. **Type/name consistency:** `RelayStore`, `RelayConfig`, `create_relay_app(config, now_fn, store, run_purge)`, `fetch_token`, `relay_token` used consistently across tasks.
4. **DRY:** relay reuses `wecom.crypto.verify_signature` / `parse_message_xml` / `decrypt_message` — no re-implemented crypto.

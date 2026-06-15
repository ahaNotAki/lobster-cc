"""Self-hosted WeCom relay app.

Replaces the AWS API Gateway + Lambda + DynamoDB relay. Receives raw WeCom
callbacks, verifies the WeCom signature + timestamp freshness, buffers the raw
encrypted message in a short-TTL SQLite store, and serves it to the local
server's poller over an authenticated /messages/fetch endpoint.

Freshness is enforced HERE (the trust boundary, where WeCom delivers within
seconds), never on the local dispatch path — enforcing it locally would discard
messages legitimately buffered during a local outage.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass

from aiohttp import web

from remote_control.wecom.crypto import (
    decrypt_message,
    parse_message_xml,
    verify_signature,
)

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    -- AUTOINCREMENT is load-bearing: it guarantees seq is never reused after
    -- purge_expired() deletes rows. The local poller stores its position as a
    -- seq cursor; reusing a seq (plain INTEGER PRIMARY KEY / ROWID would) could
    -- make the poller skip or re-read messages. Do not "simplify" this away.
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL,
    query_params TEXT NOT NULL DEFAULT '{}',
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
"""


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
        agent_configs: dict = {}
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
        """Return (token, aes_key) for an agent, falling back to legacy single-agent env."""
        if agent_id and agent_id in self.agent_configs:
            c = self.agent_configs[agent_id]
            return c.get("token", ""), c.get("aes_key", "")
        return self.legacy_token, self.legacy_aes_key


class RelayStore:
    """SQLite-backed buffer for raw WeCom callbacks. WAL mode, short TTL.

    Concurrency contract: every method is synchronous and runs on the single
    aiohttp event-loop thread (handlers + the purge task). This is what makes
    commit-per-write safe without locking. Do not `await` mid-transaction or
    move these calls to a thread executor without adding locking and
    check_same_thread=False.
    """

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


def create_relay_app(
    config: RelayConfig,
    now_fn=None,
    store: "RelayStore | None" = None,
    run_purge: bool = False,
) -> web.Application:
    """Build the relay aiohttp app. now_fn injectable for tests."""
    now_fn = now_fn or (lambda: int(time.time()))
    owns_store = store is None
    if store is None:
        store = RelayStore(config.db_path, ttl_seconds=config.ttl_days * 86400)
        store.open()

    stats = {"rejected": 0, "last_callback_ts": 0}

    def _reject(text: str) -> web.Response:
        stats["rejected"] += 1
        return web.Response(status=403, text=text)

    async def handle_callback(request: web.Request) -> web.Response:
        agent_id = request.match_info.get("agent_id", "")
        params = request.query
        msg_signature = params.get("msg_signature", "")
        timestamp = params.get("timestamp", "")
        nonce = params.get("nonce", "")
        body = await request.text()
        if not body:
            return _reject("empty body")

        token, _aes = config.agent_creds(agent_id)
        if not token:
            return _reject("unknown agent")

        # Timestamp freshness — replay protection at the trust boundary.
        try:
            ts_int = int(timestamp)
        except ValueError:
            return _reject("bad timestamp")
        if abs(now_fn() - ts_int) > config.freshness_seconds:
            logger.warning("Rejected stale callback (agent=%s, age=%ss)", agent_id, now_fn() - ts_int)
            return _reject("stale")

        # Verify WeCom signature over the encrypted payload.
        outer = parse_message_xml(body)
        encrypt = outer.get("Encrypt", "")
        if not encrypt or not verify_signature(token, timestamp, nonce, encrypt, msg_signature):
            return _reject("invalid signature")

        store.put(agent_id=agent_id, body=body, query_params={
            "msg_signature": msg_signature, "timestamp": timestamp, "nonce": nonce,
        })
        stats["last_callback_ts"] = now_fn()
        return web.Response(text="success")

    async def handle_verify(request: web.Request) -> web.Response:
        # WeCom URL verification (GET) — echo decrypted echostr.
        agent_id = request.match_info.get("agent_id", "")
        params = request.query
        token, aes_key = config.agent_creds(agent_id)
        if not token or not aes_key:
            return web.Response(status=403, text="unknown agent")
        echostr = params.get("echostr", "")
        if not verify_signature(token, params.get("timestamp", ""),
                                params.get("nonce", ""), echostr,
                                params.get("msg_signature", "")):
            return web.Response(status=403, text="invalid signature")
        try:
            decrypted = decrypt_message(aes_key, echostr)
        except Exception:
            logger.warning("Failed to decrypt echostr during verify (agent=%s)", agent_id)
            return web.Response(status=403, text="invalid echostr")
        return web.Response(text=decrypted.content)

    async def handle_fetch(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {config.fetch_token}"
        # Constant-time compare — token is a shared secret.
        if not hmac.compare_digest(auth, expected):
            return web.Response(status=401, text="unauthorized")
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        try:
            cursor = int(payload.get("cursor") or 0)
            limit = max(1, min(int(payload.get("limit", 100)), 100))
        except (TypeError, ValueError):
            return web.Response(status=400, text="bad cursor or limit")
        messages, next_cursor = store.fetch(cursor=cursor, limit=limit)
        return web.json_response({"messages": messages, "next_cursor": next_cursor})

    async def handle_health(request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "queue_depth": store.queue_depth(),
            "last_callback_ts": stats["last_callback_ts"],
            "rejected_count": stats["rejected"],
        })

    app = web.Application()
    app["relay_store"] = store
    app["relay_config"] = config
    app["now_fn"] = now_fn
    app.router.add_get("/callback/{agent_id}", handle_verify)
    app.router.add_post("/callback/{agent_id}", handle_callback)
    app.router.add_get("/callback", handle_verify)
    app.router.add_post("/callback", handle_callback)
    app.router.add_post("/messages/fetch", handle_fetch)
    app.router.add_get("/health", handle_health)

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

    if owns_store:
        async def _close_store(_app):
            store.close()
        app.on_cleanup.append(_close_store)

    return app

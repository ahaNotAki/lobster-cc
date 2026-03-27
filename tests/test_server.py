"""Tests for the server app creation and health endpoint."""

import json

import pytest
from unittest.mock import MagicMock
from aiohttp.test_utils import TestClient, TestServer

from remote_control.server import create_app, _create_message_source, _write_mcp_json
from remote_control.wecom.message_source import CallbackSource, RelayPollingSource


def _mock_store():
    s = MagicMock()
    s.get_kv = MagicMock(return_value="")
    s.set_kv = MagicMock()
    return s


@pytest.mark.asyncio
async def test_health_endpoint(app_config):
    app = create_app(app_config)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"

    await client.close()


@pytest.mark.asyncio
async def test_callback_routes_registered(app_config):
    app = create_app(app_config)
    route_paths = [r.resource.canonical for r in app.router.routes()]
    assert "/wecom/callback/1000002" in route_paths
    assert "/health" in route_paths


@pytest.mark.asyncio
async def test_relay_routes_registered(relay_config):
    app = create_app(relay_config)
    route_paths = [r.resource.canonical for r in app.router.routes()]
    assert "/relay/status/1000002" in route_paths
    assert "/health" in route_paths
    # Callback routes should NOT be registered in relay mode
    assert "/wecom/callback/1000002" not in route_paths


def test_create_message_source_callback(app_config):
    source = _create_message_source(app_config.wecom[0], lambda msg: None, _mock_store())
    assert isinstance(source, CallbackSource)


def test_create_message_source_relay(relay_config):
    source = _create_message_source(relay_config.wecom[0], lambda msg: None, _mock_store())
    assert isinstance(source, RelayPollingSource)


def test_create_message_source_invalid(app_config):
    app_config.wecom[0].mode = "invalid"
    with pytest.raises(ValueError, match="Unknown wecom.mode"):
        _create_message_source(app_config.wecom[0], lambda msg: None, _mock_store())


def test_create_message_source_relay_missing_url(app_config):
    app_config.wecom[0].mode = "relay"
    app_config.wecom[0].relay_url = ""
    with pytest.raises(ValueError, match="relay_url is required"):
        _create_message_source(app_config.wecom[0], lambda msg: None, _mock_store())


@pytest.mark.asyncio
async def test_app_stores_references(app_config):
    app = create_app(app_config)
    assert "agents" in app
    assert "store" in app
    # Backwards-compatible references
    assert "executor" in app
    assert "wecom_api" in app
    assert "message_source" in app


@pytest.mark.asyncio
async def test_multi_agent_app(tmp_path):
    """Test app creation with multiple agents."""
    from remote_control.config import (
        AgentConfig, AppConfig, WeComConfig, StorageConfig, NotificationsConfig,
    )
    config = AppConfig(
        wecom=[
            WeComConfig(
                name="agent-a", corp_id="c", agent_id=1000002,
                secret="s1", token="t1", encoding_aes_key="k1", mode="callback",
            ),
            WeComConfig(
                name="agent-b", corp_id="c", agent_id=1000003,
                secret="s2", token="t2", encoding_aes_key="k2", mode="callback",
            ),
        ],
        agent=AgentConfig(default_working_dir=str(tmp_path)),
        storage=StorageConfig(db_path=str(tmp_path / "test.db")),
        notifications=NotificationsConfig(progress_interval_seconds=1),
    )
    app = create_app(config)
    assert len(app["agents"]) == 2
    assert app["agents"][0]["label"] == "agent-a"
    assert app["agents"][1]["label"] == "agent-b"


# --- .mcp.json generation ---


def test_write_mcp_json(app_config, tmp_path):
    _write_mcp_json(app_config, app_config.wecom[0])
    mcp_path = tmp_path / ".mcp.json"
    assert mcp_path.exists()

    data = json.loads(mcp_path.read_text())
    wecom = data["mcpServers"]["wecom"]
    assert wecom["env"]["WECOM_CORP_ID"] == "test_corp"
    assert wecom["env"]["WECOM_AGENT_ID"] == "1000002"
    assert wecom["env"]["WECOM_SECRET"] == "test_secret"
    assert "-m" in wecom["args"]
    assert "remote_control.mcp.wecom_server" in wecom["args"]


def test_write_mcp_json_merges_existing(app_config, tmp_path):
    """Existing .mcp.json entries should be preserved."""
    mcp_path = tmp_path / ".mcp.json"
    mcp_path.write_text(json.dumps({
        "mcpServers": {"other-tool": {"command": "node", "args": ["server.js"]}}
    }))

    _write_mcp_json(app_config, app_config.wecom[0])

    data = json.loads(mcp_path.read_text())
    assert "other-tool" in data["mcpServers"]
    assert "wecom" in data["mcpServers"]


# --- Cron task lifecycle API ---


async def _make_cron_client(app_config):
    """Create a test client for cron API tests, suppressing shutdown errors."""
    app = create_app(app_config)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return app, client


async def _close_cron_client(client):
    """Close test client, suppressing WeComAPI shutdown errors (test uses fake creds)."""
    try:
        await client.close()
    except RuntimeError:
        pass


@pytest.mark.asyncio
async def test_cron_start_creates_running_task(app_config, tmp_path):
    """POST /api/cron/start should create a running task in the DB."""
    app, client = await _make_cron_client(app_config)

    resp = await client.post("/api/cron/start", json={
        "name": "test-cron",
        "working_dir": str(tmp_path),
    })
    assert resp.status == 200
    data = await resp.json()
    assert "task_id" in data
    assert data["agent_id"] == "1000002"

    # Verify task exists in DB with running status
    store = app["store"]
    task = store.get_task(data["task_id"])
    assert task is not None
    assert task.status.value == "running"
    assert "[Scheduled] test-cron" in task.message

    await _close_cron_client(client)


@pytest.mark.asyncio
async def test_cron_finish_completes_task(app_config, tmp_path):
    """POST /api/cron/finish should mark the task as completed."""
    app, client = await _make_cron_client(app_config)

    # Start a cron task
    resp = await client.post("/api/cron/start", json={
        "name": "test-cron",
        "working_dir": str(tmp_path),
    })
    task_id = (await resp.json())["task_id"]

    # Finish it successfully
    resp = await client.post("/api/cron/finish", json={
        "task_id": task_id,
        "exit_code": 0,
    })
    assert resp.status == 200

    # Verify task is completed
    store = app["store"]
    task = store.get_task(task_id)
    assert task.status.value == "completed"

    await _close_cron_client(client)


@pytest.mark.asyncio
async def test_cron_finish_marks_failed(app_config, tmp_path):
    """Non-zero exit code should mark task as failed."""
    app, client = await _make_cron_client(app_config)

    resp = await client.post("/api/cron/start", json={
        "name": "failing-cron",
        "working_dir": str(tmp_path),
    })
    task_id = (await resp.json())["task_id"]

    resp = await client.post("/api/cron/finish", json={
        "task_id": task_id,
        "exit_code": 1,
    })
    assert resp.status == 200

    store = app["store"]
    task = store.get_task(task_id)
    assert task.status.value == "failed"
    assert "exit code 1" in task.error

    await _close_cron_client(client)


@pytest.mark.asyncio
async def test_cron_start_missing_fields(app_config, tmp_path):
    """Missing name or working_dir should return 400."""
    _, client = await _make_cron_client(app_config)

    resp = await client.post("/api/cron/start", json={"name": "x"})
    assert resp.status == 400

    resp = await client.post("/api/cron/start", json={"working_dir": "/tmp"})
    assert resp.status == 400

    await _close_cron_client(client)


@pytest.mark.asyncio
async def test_cron_start_unknown_working_dir_uses_fallback(app_config, tmp_path):
    """Unknown working_dir should fall back to first agent."""
    _, client = await _make_cron_client(app_config)

    resp = await client.post("/api/cron/start", json={
        "name": "mystery-cron",
        "working_dir": "/nonexistent/path",
    })
    assert resp.status == 200
    data = await resp.json()
    # Falls back to first agent
    assert data["agent_id"] == "1000002"

    await _close_cron_client(client)

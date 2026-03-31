"""Tests for dashboard routes — auth helpers and HTTP endpoints."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from remote_control.dashboard import routes
from remote_control.dashboard.routes import (
    _COOKIE_MAX_AGE,
    _COOKIE_NAME,
    _LOCKOUT_SECONDS,
    _MAX_FAILED_ATTEMPTS,
    _get_client_ip,
    _is_locked_out,
    _make_token,
    _record_failure,
    _verify_token,
    register_dashboard_routes,
)

# ---------------------------------------------------------------------------
# Constants used across tests
# ---------------------------------------------------------------------------
PASSWORD = "hunter2"
SECRET = "super-secret-key"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_app(store=None, runner=None, streaming_ref=None, working_dir="."):
    """Create a minimal aiohttp app with dashboard routes registered."""
    app = web.Application()
    register_dashboard_routes(
        app,
        password=PASSWORD,
        secret=SECRET,
        store=store or MagicMock(),
        runner=runner or MagicMock(),
        streaming_ref=streaming_ref or {},
        working_dir=working_dir,
    )
    return app


def _make_request_with_headers(headers: dict, peername=None):
    """Build a mock aiohttp.web.Request with given headers and peername."""
    req = MagicMock(spec=web.Request)
    req.headers = headers
    transport = MagicMock()
    transport.get_extra_info.return_value = peername
    req.transport = transport
    return req


# ---------------------------------------------------------------------------
# 1. _make_token
# ---------------------------------------------------------------------------

class TestMakeToken:
    def test_format(self):
        token = _make_token(PASSWORD, SECRET)
        parts = token.split(":")
        assert len(parts) == 2, "Token must be 'expires:sig'"
        expires_str, sig = parts
        assert expires_str.isdigit()
        assert len(sig) == 32  # hex digest truncated to 32 chars

    def test_determinism_same_second(self):
        """Tokens created in the same second are identical."""
        with patch.object(routes.time, "time", return_value=1000000.0):
            t1 = _make_token(PASSWORD, SECRET)
            t2 = _make_token(PASSWORD, SECRET)
        assert t1 == t2

    def test_expiration_offset(self):
        fake_now = 1_700_000_000.0
        with patch.object(routes.time, "time", return_value=fake_now):
            token = _make_token(PASSWORD, SECRET)
        expires_str = token.split(":")[0]
        assert int(expires_str) == int(fake_now) + _COOKIE_MAX_AGE


# ---------------------------------------------------------------------------
# 2. _verify_token
# ---------------------------------------------------------------------------

class TestVerifyToken:
    def test_valid_token(self):
        with patch.object(routes.time, "time", return_value=1_700_000_000.0):
            token = _make_token(PASSWORD, SECRET)
        # Verify while still before expiry
        with patch.object(routes.time, "time", return_value=1_700_000_000.0 + 100):
            assert _verify_token(token, PASSWORD, SECRET) is True

    def test_expired_token(self):
        with patch.object(routes.time, "time", return_value=1_700_000_000.0):
            token = _make_token(PASSWORD, SECRET)
        # Verify after expiry
        with patch.object(routes.time, "time", return_value=1_700_000_000.0 + _COOKIE_MAX_AGE + 1):
            assert _verify_token(token, PASSWORD, SECRET) is False

    def test_invalid_signature(self):
        with patch.object(routes.time, "time", return_value=1_700_000_000.0):
            token = _make_token(PASSWORD, SECRET)
        expires_str = token.split(":")[0]
        bad_token = f"{expires_str}:{'a' * 32}"
        with patch.object(routes.time, "time", return_value=1_700_000_000.0 + 100):
            assert _verify_token(bad_token, PASSWORD, SECRET) is False

    def test_malformed_no_colon(self):
        assert _verify_token("nocolonhere", PASSWORD, SECRET) is False

    def test_malformed_non_numeric_expires(self):
        assert _verify_token("notanumber:abcdef1234567890abcdef1234567890", PASSWORD, SECRET) is False

    def test_wrong_secret(self):
        with patch.object(routes.time, "time", return_value=1_700_000_000.0):
            token = _make_token(PASSWORD, SECRET)
        with patch.object(routes.time, "time", return_value=1_700_000_000.0 + 100):
            assert _verify_token(token, PASSWORD, "wrong-secret") is False


# ---------------------------------------------------------------------------
# 3. _is_locked_out
# ---------------------------------------------------------------------------

class TestIsLockedOut:
    def setup_method(self):
        routes._failed_attempts.clear()

    def test_not_in_dict(self):
        assert _is_locked_out("1.2.3.4") is False

    def test_under_threshold(self):
        routes._failed_attempts["1.2.3.4"] = (_MAX_FAILED_ATTEMPTS - 1, time.time())
        assert _is_locked_out("1.2.3.4") is False

    def test_at_threshold(self):
        routes._failed_attempts["1.2.3.4"] = (_MAX_FAILED_ATTEMPTS, time.time())
        assert _is_locked_out("1.2.3.4") is True

    def test_above_threshold(self):
        routes._failed_attempts["1.2.3.4"] = (_MAX_FAILED_ATTEMPTS + 5, time.time())
        assert _is_locked_out("1.2.3.4") is True

    def test_window_expired(self):
        old_time = time.time() - _LOCKOUT_SECONDS - 1
        routes._failed_attempts["1.2.3.4"] = (_MAX_FAILED_ATTEMPTS, old_time)
        assert _is_locked_out("1.2.3.4") is False
        # Entry should be removed
        assert "1.2.3.4" not in routes._failed_attempts


# ---------------------------------------------------------------------------
# 4. _record_failure
# ---------------------------------------------------------------------------

class TestRecordFailure:
    def setup_method(self):
        routes._failed_attempts.clear()

    def test_first_failure(self):
        _record_failure("10.0.0.1")
        assert "10.0.0.1" in routes._failed_attempts
        count, _ = routes._failed_attempts["10.0.0.1"]
        assert count == 1

    def test_increment(self):
        now = time.time()
        routes._failed_attempts["10.0.0.1"] = (3, now)
        _record_failure("10.0.0.1")
        count, first_time = routes._failed_attempts["10.0.0.1"]
        assert count == 4
        assert first_time == now  # first_time should be preserved

    def test_reset_after_window(self):
        old_time = time.time() - _LOCKOUT_SECONDS - 1
        routes._failed_attempts["10.0.0.1"] = (10, old_time)
        _record_failure("10.0.0.1")
        count, first_time = routes._failed_attempts["10.0.0.1"]
        assert count == 1
        assert first_time > old_time  # reset to a new window


# ---------------------------------------------------------------------------
# 5. _get_client_ip
# ---------------------------------------------------------------------------

class TestGetClientIp:
    def test_x_forwarded_for_single(self):
        req = _make_request_with_headers({"X-Forwarded-For": "203.0.113.50"})
        assert _get_client_ip(req) == "203.0.113.50"

    def test_x_forwarded_for_multiple(self):
        req = _make_request_with_headers({"X-Forwarded-For": " 203.0.113.50 , 70.41.3.18 , 150.172.238.178 "})
        assert _get_client_ip(req) == "203.0.113.50"

    def test_no_header_uses_peername(self):
        req = _make_request_with_headers({}, peername=("192.168.1.1", 54321))
        assert _get_client_ip(req) == "192.168.1.1"

    def test_no_header_no_peername(self):
        req = _make_request_with_headers({}, peername=None)
        assert _get_client_ip(req) == "unknown"


# ---------------------------------------------------------------------------
# 6. Route integration tests
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_store():
    store = MagicMock()
    store.get_running_task.return_value = None
    store.get_latest_task_any_user.return_value = None
    store.list_tasks_all_users.return_value = []
    return store


@pytest.fixture
def mock_runner():
    runner = MagicMock()
    runner.is_running = False
    runner._process = None
    runner.model_info = {}
    return runner


@pytest.fixture
async def client(mock_store, mock_runner, tmp_path):
    app = _build_app(store=mock_store, runner=mock_runner, working_dir=str(tmp_path))
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    # Clear global state between tests
    routes._failed_attempts.clear()
    yield cli
    await cli.close()
    routes._failed_attempts.clear()


def _auth_cookie(fake_time=1_700_000_000.0):
    """Generate a valid auth cookie value."""
    with patch.object(routes.time, "time", return_value=fake_time):
        return _make_token(PASSWORD, SECRET)


class TestLoginPage:
    @pytest.mark.asyncio
    async def test_get_login_returns_200(self, client: TestClient):
        resp = await client.get("/dashboard/login")
        assert resp.status == 200
        text = await resp.text()
        assert "password" in text.lower()


class TestLoginSubmit:
    @pytest.mark.asyncio
    async def test_correct_password_redirects_with_cookie(self, client: TestClient):
        resp = await client.post(
            "/dashboard/login",
            data={"password": PASSWORD},
            allow_redirects=False,
        )
        assert resp.status == 302
        assert resp.headers["Location"] == "/dashboard"
        # Cookie should be set
        cookie_header = resp.headers.get("Set-Cookie", "")
        assert _COOKIE_NAME in cookie_header

    @pytest.mark.asyncio
    async def test_wrong_password_records_failure(self, client: TestClient):
        resp = await client.post(
            "/dashboard/login",
            data={"password": "wrongpass"},
            allow_redirects=False,
        )
        assert resp.status == 302
        assert "error=" in resp.headers["Location"]
        # There should be a failure recorded for the client IP
        assert len(routes._failed_attempts) >= 1


class TestDashboardPage:
    @pytest.mark.asyncio
    async def test_no_auth_redirects(self, client: TestClient):
        resp = await client.get("/dashboard", allow_redirects=False)
        assert resp.status == 302
        assert "/dashboard/login" in resp.headers["Location"]

    @pytest.mark.asyncio
    async def test_with_valid_auth_returns_200(self, client: TestClient, tmp_path):
        # We need the actual static HTML file to exist; use the real one
        fake_time = 1_700_000_000.0
        token = _auth_cookie(fake_time)
        with patch.object(routes.time, "time", return_value=fake_time + 100):
            client.session.cookie_jar.update_cookies(
                {_COOKIE_NAME: token},
                response_url=client.make_url("/"),
            )
            resp = await client.get("/dashboard", allow_redirects=False)
        assert resp.status == 200
        text = await resp.text()
        assert "html" in text.lower()


class TestApiStatus:
    @pytest.mark.asyncio
    async def test_no_auth_returns_401(self, client: TestClient):
        resp = await client.get("/api/status")
        assert resp.status == 401
        data = await resp.json()
        assert data["error"] == "unauthorized"

    @pytest.mark.asyncio
    async def test_with_auth_returns_200_json(self, client: TestClient, mock_store, mock_runner):
        fake_time = 1_700_000_000.0
        token = _auth_cookie(fake_time)
        with patch.object(routes.time, "time", return_value=fake_time + 100):
            client.session.cookie_jar.update_cookies(
                {_COOKIE_NAME: token},
                response_url=client.make_url("/"),
            )
            resp = await client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert "agent" in data
        assert "recent_tasks" in data
        assert data["agent"]["state"] == "idle"


class TestLockout:
    @pytest.mark.asyncio
    async def test_lockout_after_max_failures(self, client: TestClient):
        # Submit wrong password MAX_FAILED_ATTEMPTS times
        for _ in range(_MAX_FAILED_ATTEMPTS):
            await client.post(
                "/dashboard/login",
                data={"password": "wrong"},
                allow_redirects=False,
            )

        # Next attempt should be locked out
        resp = await client.post(
            "/dashboard/login",
            data={"password": "wrong"},
            allow_redirects=False,
        )
        assert resp.status == 302
        assert "Locked" in resp.headers["Location"] or "locked" in resp.headers["Location"].lower()

        # Even correct password should be locked out
        resp = await client.post(
            "/dashboard/login",
            data={"password": PASSWORD},
            allow_redirects=False,
        )
        assert resp.status == 302
        assert "Locked" in resp.headers["Location"] or "locked" in resp.headers["Location"].lower()


# ---------------------------------------------------------------------------
# 7. Task detail API
# ---------------------------------------------------------------------------

class TestApiTaskDetail:
    @pytest.mark.asyncio
    async def test_task_detail_api(self, client: TestClient, mock_store):
        """GET /api/task/{task_id} returns full task data with auth."""
        from remote_control.core.models import Task, TaskStatus

        task = Task(
            id="abc123",
            user_id="u1",
            session_id="s1",
            message="do something important",
            status=TaskStatus.COMPLETED,
            output="task output here",
            error="",
            created_at="2026-01-01T00:00:00+00:00",
            started_at="2026-01-01T00:00:05+00:00",
            finished_at="2026-01-01T00:00:35+00:00",
        )
        mock_store.get_task.return_value = task

        fake_time = 1_700_000_000.0
        token = _auth_cookie(fake_time)
        with patch.object(routes.time, "time", return_value=fake_time + 100):
            client.session.cookie_jar.update_cookies(
                {_COOKIE_NAME: token},
                response_url=client.make_url("/"),
            )
            resp = await client.get("/api/task/abc123")

        assert resp.status == 200
        data = await resp.json()
        assert data["id"] == "abc123"
        assert data["status"] == "completed"
        assert data["message"] == "do something important"
        assert data["output"] == "task output here"
        assert data["error"] == ""
        assert data["created_at"] == "2026-01-01T00:00:00+00:00"
        assert data["started_at"] == "2026-01-01T00:00:05+00:00"
        assert data["finished_at"] == "2026-01-01T00:00:35+00:00"
        assert data["duration"] == 30

    @pytest.mark.asyncio
    async def test_task_detail_api_unauthorized(self, client: TestClient):
        """GET /api/task/{task_id} returns 401 without auth."""
        resp = await client.get("/api/task/abc123")
        assert resp.status == 401
        data = await resp.json()
        assert data["error"] == "unauthorized"

    @pytest.mark.asyncio
    async def test_task_detail_api_not_found(self, client: TestClient, mock_store):
        """GET /api/task/{task_id} returns 404 for unknown task."""
        mock_store.get_task.return_value = None

        fake_time = 1_700_000_000.0
        token = _auth_cookie(fake_time)
        with patch.object(routes.time, "time", return_value=fake_time + 100):
            client.session.cookie_jar.update_cookies(
                {_COOKIE_NAME: token},
                response_url=client.make_url("/"),
            )
            resp = await client.get("/api/task/nonexistent")

        assert resp.status == 404
        data = await resp.json()
        assert data["error"] == "task not found"


# ---------------------------------------------------------------------------
# 8. Tab data API
# ---------------------------------------------------------------------------


@pytest.fixture
async def tab_client(mock_store, mock_runner, tmp_path):
    """Client with agents configured for tab API testing."""
    import json

    # Create tab config and data files in a working dir
    wd = tmp_path / "lobster1"
    wd.mkdir()
    tabs = [
        {"id": "stocks", "label": "Stocks", "type": "data", "source": "stocks.json"},
    ]
    (wd / ".dashboard-tabs.json").write_text(json.dumps(tabs))
    (wd / "stocks.json").write_text(json.dumps([{"ticker": "AAPL", "price": 150}]))

    # Build app with agents list that has a working_dir
    app = web.Application()

    # Mock executor
    executor = MagicMock()
    executor.runner = mock_runner
    executor.dashboard_streaming = {}
    executor.store = mock_store

    # Mock wecom_config with agent_id attribute
    wecom_config = MagicMock()
    wecom_config.agent_id = "1000002"

    agents = [{"label": "TestBot", "executor": executor,
               "working_dir": str(wd), "wecom_config": wecom_config}]

    register_dashboard_routes(
        app, password=PASSWORD, secret=SECRET, store=mock_store,
        agents=agents, working_dir=str(tmp_path),
    )

    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    routes._failed_attempts.clear()
    yield cli
    await cli.close()
    routes._failed_attempts.clear()


class TestTabApi:
    @pytest.mark.asyncio
    async def test_tab_api_endpoint(self, tab_client: TestClient):
        """GET /api/tab/{agent_id}/{tab_id} returns tab data."""
        fake_time = 1_700_000_000.0
        token = _auth_cookie(fake_time)
        with patch.object(routes.time, "time", return_value=fake_time + 100):
            tab_client.session.cookie_jar.update_cookies(
                {_COOKIE_NAME: token},
                response_url=tab_client.make_url("/"),
            )
            resp = await tab_client.get("/api/tab/1000002/stocks")
        assert resp.status == 200
        data = await resp.json()
        assert data["template"] == "table"
        assert data["data"][0]["ticker"] == "AAPL"

    @pytest.mark.asyncio
    async def test_tab_api_unauthorized(self, tab_client: TestClient):
        """Returns 401 without auth."""
        resp = await tab_client.get("/api/tab/1000002/stocks")
        assert resp.status == 401
        data = await resp.json()
        assert data["error"] == "unauthorized"

    @pytest.mark.asyncio
    async def test_tab_api_not_found(self, tab_client: TestClient):
        """Unknown tab_id returns 404."""
        fake_time = 1_700_000_000.0
        token = _auth_cookie(fake_time)
        with patch.object(routes.time, "time", return_value=fake_time + 100):
            tab_client.session.cookie_jar.update_cookies(
                {_COOKIE_NAME: token},
                response_url=tab_client.make_url("/"),
            )
            resp = await tab_client.get("/api/tab/1000002/nonexistent")
        assert resp.status == 404
        data = await resp.json()
        assert data["error"] == "tab not found"

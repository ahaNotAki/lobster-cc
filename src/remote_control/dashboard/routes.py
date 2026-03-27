"""Dashboard routes — read-only web UI with password auth."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from pathlib import Path

from aiohttp import web

from remote_control.dashboard.status import get_agent_status

logger = logging.getLogger(__name__)

# Auth settings
_COOKIE_NAME = "rc_dash_token"
_COOKIE_MAX_AGE = 86400  # 24 hours
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_SECONDS = 900  # 15 minutes

# Rate-limit tracking: ip -> (fail_count, first_fail_time)
_failed_attempts: dict[str, tuple[int, float]] = {}


def _make_token(password: str, secret: str) -> str:
    """Create an HMAC-signed auth token."""
    expires = str(int(time.time()) + _COOKIE_MAX_AGE)
    sig = hmac.new(secret.encode(), f"{password}:{expires}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{expires}:{sig}"


def _verify_token(token: str, password: str, secret: str) -> bool:
    """Verify an auth token is valid and not expired."""
    try:
        expires_str, sig = token.split(":", 1)
        if int(expires_str) < time.time():
            return False
        expected = hmac.new(secret.encode(), f"{password}:{expires_str}".encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(sig, expected)
    except (ValueError, AttributeError):
        return False


def _is_locked_out(ip: str) -> bool:
    """Check if an IP is locked out from too many failed attempts."""
    if ip not in _failed_attempts:
        return False
    count, first_time = _failed_attempts[ip]
    if time.time() - first_time > _LOCKOUT_SECONDS:
        del _failed_attempts[ip]
        return False
    return count >= _MAX_FAILED_ATTEMPTS


def _record_failure(ip: str) -> None:
    if ip in _failed_attempts:
        count, first_time = _failed_attempts[ip]
        if time.time() - first_time > _LOCKOUT_SECONDS:
            _failed_attempts[ip] = (1, time.time())
        else:
            _failed_attempts[ip] = (count + 1, first_time)
    else:
        _failed_attempts[ip] = (1, time.time())


def _get_client_ip(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    peername = request.transport.get_extra_info("peername")
    return peername[0] if peername else "unknown"


def register_dashboard_routes(
    app: web.Application,
    password: str,
    secret: str,
    store,
    agents: list[dict] | None = None,
    working_dir: str = ".",
    # Legacy single-agent params (for backwards compat with tests)
    runner=None,
    streaming_ref: dict | None = None,
) -> None:
    """Register all dashboard routes (GET only — strictly read-only)."""

    def _check_auth(request: web.Request) -> bool:
        token = request.cookies.get(_COOKIE_NAME, "")
        return _verify_token(token, password, secret)

    async def login_page(request: web.Request) -> web.Response:
        error = request.query.get("error", "")
        ip = _get_client_ip(request)
        locked = _is_locked_out(ip)
        html = _LOGIN_HTML.replace("{{ERROR}}", "IP locked out for 15 minutes." if locked else error)
        return web.Response(text=html, content_type="text/html")

    async def login_submit(request: web.Request) -> web.Response:
        ip = _get_client_ip(request)
        if _is_locked_out(ip):
            raise web.HTTPFound("/dashboard/login?error=Too+many+attempts.+Locked+for+15+min.")

        data = await request.post()
        pwd = data.get("password", "")
        if pwd == password:
            _failed_attempts.pop(ip, None)
            token = _make_token(password, secret)
            resp = web.HTTPFound("/dashboard")
            resp.set_cookie(_COOKIE_NAME, token, max_age=_COOKIE_MAX_AGE,
                           httponly=True, path="/", samesite="Lax")
            return resp
        else:
            _record_failure(ip)
            raise web.HTTPFound("/dashboard/login?error=Wrong+password")

    async def dashboard_page(request: web.Request) -> web.Response:
        if not _check_auth(request):
            raise web.HTTPFound("/dashboard/login")
        html_path = Path(__file__).parent / "static" / "dashboard.html"
        html = html_path.read_text()
        return web.Response(text=html, content_type="text/html")

    async def api_status(request: web.Request) -> web.Response:
        if not _check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        # Resolve project_dir (the dir where remote_control is deployed)
        import os
        project_dir = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))

        agent_list = agents or []
        if not agent_list and runner is not None:
            agent_list = [{"label": "default", "executor": type("E", (), {
                "runner": runner, "dashboard_streaming": streaming_ref or {}
            })()}]

        all_agents_data = []
        for ag in agent_list:
            executor = ag.get("executor")
            ag_runner = executor.runner if executor else None
            ag_streaming = executor.dashboard_streaming if executor else {}
            # Use the executor's ScopedStore so each agent only sees its own tasks
            ag_store = getattr(executor, "store", store) if executor else store
            # Per-agent working dir for config loading
            ag_working_dir = ag.get("working_dir", working_dir)
            data = get_agent_status(ag_store, ag_runner, ag_streaming, ag_working_dir, project_dir)
            # Use lobster.name from config (real-time), fallback to startup label
            lobster_name = data.get("lobster", {}).get("name", "")
            data["agent"]["name"] = lobster_name or ag.get("label", "agent")
            all_agents_data.append(data)

        # Always return multi-agent structure
        first = all_agents_data[0] if all_agents_data else get_agent_status(store, None, {}, working_dir, project_dir)
        # Build agent_id → name mapping
        agent_names = {}
        for ag_data, ag_cfg in zip(all_agents_data, agent_list):
            aid = str(ag_cfg.get("wecom_config", type("X", (), {"agent_id": ""})()).agent_id) if hasattr(ag_cfg.get("wecom_config"), "agent_id") else ""
            if not aid:
                aid = ag_cfg.get("label", "")
            agent_names[aid] = ag_data["agent"]["name"]

        first["agents"] = [a["agent"] for a in all_agents_data]
        first["all_processes"] = [a["process"] for a in all_agents_data]
        first["all_models"] = [a.get("model", {}) for a in all_agents_data]
        first["all_lobsters"] = [a.get("lobster", {}) for a in all_agents_data]
        first["all_schedules"] = [a.get("schedule_configs", []) for a in all_agents_data]
        first["agent_names"] = agent_names
        return web.json_response(first)

    # Only GET routes + one POST for login form
    app.router.add_get("/dashboard", dashboard_page)
    app.router.add_get("/dashboard/login", login_page)
    app.router.add_post("/dashboard/login", login_submit)
    app.router.add_get("/api/status", api_status)


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>🦞 Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,#0c1445,#1a237e);font-family:-apple-system,sans-serif;color:#fff}
.card{background:rgba(255,255,255,0.08);border-radius:16px;padding:40px;width:340px;
  backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.1)}
h1{text-align:center;font-size:28px;margin-bottom:24px}
input{width:100%;padding:12px 16px;border-radius:8px;border:1px solid rgba(255,255,255,0.2);
  background:rgba(255,255,255,0.06);color:#fff;font-size:16px;margin-bottom:16px;outline:none}
input:focus{border-color:#64b5f6}
button{width:100%;padding:12px;border:none;border-radius:8px;background:#1565c0;color:#fff;
  font-size:16px;cursor:pointer;transition:background .2s}
button:hover{background:#1976d2}
.error{color:#ef5350;text-align:center;margin-bottom:12px;font-size:14px}
</style></head><body>
<div class="card"><h1>🦞 Dashboard</h1>
<div class="error">{{ERROR}}</div>
<form method="POST" action="/dashboard/login">
<input type="password" name="password" placeholder="Password" autofocus>
<button type="submit">Login</button></form></div></body></html>"""

"""aiohttp server setup and route registration."""

import json
import logging
import sys
from pathlib import Path

from aiohttp import web

from remote_control.config import AppConfig, WeComConfig
from remote_control.core.executor import Executor
from remote_control.core.notifier import Notifier
from remote_control.core.router import CommandRouter
from remote_control.core.runner import AgentRunner
from remote_control.core.store import ScopedStore, Store
from remote_control.core.watchdog import ProcessWatchdog
from remote_control.wecom.api import WeComAPI
from remote_control.wecom.gateway import IncomingMessage
from remote_control.wecom.message_source import CallbackSource, MessageSource, RelayPollingSource

logger = logging.getLogger(__name__)

# Map content-type to file extension for downloaded media
_MEDIA_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "audio/amr": ".amr",
    "video/mp4": ".mp4",
    "application/octet-stream": "",
}


def _create_message_source(
    wecom_config: WeComConfig, on_message, store: Store,
) -> MessageSource:
    """Create the appropriate message source based on config."""
    mode = wecom_config.mode
    if mode == "relay":
        if not wecom_config.relay_url:
            raise ValueError(
                f"wecom.relay_url is required when mode is 'relay' (agent_id={wecom_config.agent_id})."
            )
        return RelayPollingSource(
            wecom_config, wecom_config.relay_url, on_message,
            store=store,
            poll_interval=wecom_config.relay_poll_interval_seconds,
        )
    elif mode == "callback":
        return CallbackSource(wecom_config, on_message)
    else:
        raise ValueError(f"Unknown wecom.mode: {mode!r}. Must be 'callback' or 'relay'.")


async def _download_and_save_media(
    api: WeComAPI, msg: IncomingMessage, working_dir: str,
) -> str:
    """Download media from WeCom and save to working directory. Returns the saved file path."""
    content, content_type = await api.download_media(msg.media_id)

    # Determine filename
    if msg.file_name:
        filename = msg.file_name
    else:
        ext = _MEDIA_EXTENSIONS.get(content_type, "")
        if not ext and "/" in content_type:
            ext = "." + content_type.split("/")[-1]
        filename = f"{msg.msg_type}_{msg.msg_id[:12]}{ext}"

    # Save to a _media subdirectory in the working dir
    media_dir = Path(working_dir) / "_media"
    media_dir.mkdir(exist_ok=True)
    file_path = media_dir / filename
    file_path.write_bytes(content)
    logger.info("Saved %s media to %s (%d bytes)", msg.msg_type, file_path, len(content))
    return str(file_path)


def _write_mcp_json(config: AppConfig, wecom_config: WeComConfig) -> None:
    """Write .mcp.json in the working directory so Claude Code picks up the wecom tools."""
    working_dir = Path(config.agent.default_working_dir)
    mcp_path = working_dir / ".mcp.json"

    # Find the python executable (same one running this server)
    python_bin = sys.executable

    new_entry = {
        "command": python_bin,
        "args": ["-m", "remote_control.mcp.wecom_server"],
        "env": {
            "WECOM_CORP_ID": wecom_config.corp_id,
            "WECOM_AGENT_ID": str(wecom_config.agent_id),
            "WECOM_SECRET": wecom_config.secret,
            "WECOM_PROXY": wecom_config.proxy or "",
        },
    }

    # Merge with existing .mcp.json if present
    existing = {}
    if mcp_path.exists():
        try:
            existing = json.loads(mcp_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    servers = existing.get("mcpServers", {})
    servers["wecom"] = new_entry
    existing["mcpServers"] = servers

    mcp_path.write_text(json.dumps(existing, indent=2) + "\n")
    logger.info("Wrote MCP config to %s", mcp_path)


def create_app(config: AppConfig) -> web.Application:
    """Create and configure the aiohttp application."""
    app = web.Application()

    # Shared store
    store = Store(config.storage.db_path)
    store.open()

    # Shared watchdog — monitors all agents' claude processes
    watchdog = ProcessWatchdog(
        store=store,
        notifier=None,  # Set after first notifier is created
        timeout_seconds=config.agent.watchdog_timeout_seconds,
        interval_seconds=config.agent.watchdog_interval_seconds,
    )

    # Per-agent components
    agents: list[dict] = []

    for wecom_config in config.wecom:
        agent_label = wecom_config.name or str(wecom_config.agent_id)

        # Per-agent working dir override
        agent_working_dir = wecom_config.working_dir or config.agent.default_working_dir
        agent_config = config.model_copy()
        agent_config.agent = config.agent.model_copy(update={"default_working_dir": agent_working_dir})

        wecom_api = WeComAPI(wecom_config)
        # Per-agent notification config (override global if set)
        agent_notif = config.notifications.model_copy()
        if wecom_config.streaming_interval > 0:
            agent_notif.streaming_interval_seconds = wecom_config.streaming_interval
        if wecom_config.progress_interval > 0:
            agent_notif.progress_interval_seconds = wecom_config.progress_interval
        notifier = Notifier(wecom_api, agent_notif)
        if watchdog._notifier is None:
            watchdog._notifier = notifier
        scoped_store = ScopedStore(store, str(wecom_config.agent_id))
        runner = AgentRunner(agent_config.agent, watchdog=watchdog)
        executor = Executor(agent_config, scoped_store, notifier, runner)
        router = CommandRouter(executor)

        # Create message handler with media download support
        def _make_handler(r, api, wd):
            async def on_message(msg: IncomingMessage) -> None:
                content = msg.content
                # Handle media messages: download and prepend file info
                if msg.msg_type != "text" and msg.media_id:
                    try:
                        working_dir = wd
                        path = await _download_and_save_media(api, msg, working_dir)
                        if msg.msg_type == "image":
                            prefix = f"[User sent an image: {path}]"
                        elif msg.msg_type == "voice":
                            prefix = f"[User sent a voice message: {path}]"
                        elif msg.msg_type == "file":
                            prefix = f"[User sent a file: {path}]"
                        elif msg.msg_type == "video":
                            prefix = f"[User sent a video: {path}]"
                        else:
                            prefix = f"[User sent media: {path}]"
                        content = f"{prefix}\n{content}" if content else prefix
                    except Exception:
                        logger.exception("Failed to download media %s", msg.media_id[:20])
                        content = content or f"[User sent a {msg.msg_type} but download failed]"
                if content:
                    await r.route(msg.user_id, content)
            return on_message

        source = _create_message_source(
            wecom_config, _make_handler(router, wecom_api, agent_working_dir), store,
        )
        source.register_routes(app)

        agents.append({
            "label": agent_label,
            "wecom_config": wecom_config,
            "wecom_api": wecom_api,
            "executor": executor,
            "source": source,
            "working_dir": agent_working_dir,
        })

        # Write .mcp.json so Claude Code (including scheduled tasks) can send WeCom messages
        _write_mcp_json(agent_config, wecom_config)

        logger.info("Agent '%s' (agent_id=%d) configured in %s mode (wd=%s)",
                     agent_label, wecom_config.agent_id, wecom_config.mode, agent_working_dir)

    # Dashboard (read-only web UI)
    if config.dashboard.enabled and config.dashboard.password:
        from remote_control.dashboard.routes import register_dashboard_routes
        register_dashboard_routes(
            app,
            password=config.dashboard.password,
            secret=config.dashboard.secret,
            store=store,
            agents=agents,
            working_dir=config.agent.default_working_dir,
        )
        logger.info("Dashboard enabled at /dashboard")

    # Cron task lifecycle API (called by run-scheduled-task.sh)
    def _find_agent_by_working_dir(wd: str) -> dict | None:
        """Map a working directory to an agent config."""
        wd_resolved = str(Path(wd).resolve())
        for ag in agents:
            ag_wd = str(Path(ag["working_dir"]).resolve())
            if ag_wd == wd_resolved:
                return ag
        return None

    async def _cron_start(request: web.Request) -> web.Response:
        """Called by run-scheduled-task.sh when a cron task starts."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        name = data.get("name", "")
        working_dir = data.get("working_dir", "")
        if not name or not working_dir:
            return web.json_response({"error": "name and working_dir required"}, status=400)

        ag = _find_agent_by_working_dir(working_dir)
        if not ag:
            # Fallback: use first agent
            ag = agents[0] if agents else None
        if not ag:
            return web.json_response({"error": "no agents configured"}, status=500)

        agent_id = str(ag["wecom_config"].agent_id)
        scoped = ScopedStore(store, agent_id)  # noqa: F841 — used in future auto-expire logic

        # Auto-expire any stale running task with the same cron name for this agent.
        # This handles cases where the previous run crashed without calling /finish.
        from remote_control.core.models import Task, TaskStatus
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        msg_pattern = f"[Scheduled] {name}"
        stale = store.conn.execute(
            "SELECT id FROM tasks WHERE agent_id = ? AND status = ? AND message = ?",
            (agent_id, TaskStatus.RUNNING.value, msg_pattern),
        ).fetchall()
        for row in stale:
            store.update_task_status(
                row["id"], TaskStatus.FAILED, error="Stale (new run started before previous finished)"
            )
            logger.warning("Auto-expired stale cron task: %s", row["id"])

        # Create a task directly in running state (bypasses executor queue)
        task = Task(user_id="cron", session_id="", message=msg_pattern)
        store.conn.execute(
            "INSERT INTO tasks (id, user_id, agent_id, session_id, message, status, created_at, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task.id, task.user_id, agent_id, task.session_id,
             task.message, TaskStatus.RUNNING.value, now, now),
        )
        store.conn.commit()
        logger.info("Cron task started: %s (task_id=%s, agent_id=%s)", name, task.id, agent_id)
        return web.json_response({"task_id": task.id, "agent_id": agent_id})

    async def _cron_finish(request: web.Request) -> web.Response:
        """Called by run-scheduled-task.sh when a cron task finishes."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        task_id = data.get("task_id", "")
        exit_code = data.get("exit_code", 0)
        if not task_id:
            return web.json_response({"error": "task_id required"}, status=400)

        from remote_control.core.models import TaskStatus
        status = TaskStatus.COMPLETED if exit_code == 0 else TaskStatus.FAILED
        error = f"exit code {exit_code}" if exit_code != 0 else None
        store.update_task_status(task_id, status, error=error)
        logger.info("Cron task finished: task_id=%s, exit_code=%d", task_id, exit_code)
        return web.json_response({"ok": True})

    app.router.add_post("/api/cron/start", _cron_start)
    app.router.add_post("/api/cron/finish", _cron_finish)

    # Routes
    app.router.add_get("/health", _health_handler)

    # Lifecycle hooks
    async def on_startup(_app: web.Application) -> None:
        await watchdog.start()
        for ag in agents:
            await ag["executor"].start()
            await ag["source"].start()
        logger.info(
            "Server started on %s:%s with %d agent(s)",
            config.server.host, config.server.port, len(agents),
        )

    async def on_shutdown(_app: web.Application) -> None:
        logger.info("Shutting down...")
        await watchdog.stop()
        for ag in agents:
            await ag["source"].stop()
            await ag["executor"].stop()
            await ag["wecom_api"].close()
        store.close()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # Store references for testing
    app["agents"] = agents
    app["store"] = store
    app["watchdog"] = watchdog
    # Backwards-compatible single-agent references
    if agents:
        app["executor"] = agents[0]["executor"]
        app["wecom_api"] = agents[0]["wecom_api"]
        app["message_source"] = agents[0]["source"]

    return app


async def _health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})

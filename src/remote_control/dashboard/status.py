"""Dashboard status — assembles agent state for the API endpoint."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

from remote_control.core.models import TaskStatus
from remote_control.core.store import Store

logger = logging.getLogger(__name__)

# Default workstations — used if the JSON config file doesn't exist.
_DEFAULT_LOBSTER: dict = {"name": "🦞", "emoji": "🦞"}

_DEFAULT_WORKSTATIONS: list[dict] = [
    {"id": "coding", "label": "CODE", "icon": "💻",
     "keywords": ["代码", "code", "fix", "bug", "implement", "refactor", "编程",
                   "write", "edit", "重构", "test", "deploy", "部署"]},
    {"id": "stock", "label": "STOCK", "icon": "📈",
     "keywords": ["股票", "stock", "分析", "行情", "watchlist", "晨间", "A股",
                   "买入", "持仓", "交易"]},
    {"id": "news", "label": "NEWS", "icon": "📰",
     "keywords": ["新闻", "news", "简报", "热搜", "早报", "headlines"]},
    {"id": "xhs", "label": "XHS", "icon": "📱",
     "keywords": ["小红书", "xhs", "红书", "发帖", "评论", "笔记"]},
    {"id": "browse", "label": "BROWSE", "icon": "🔍",
     "keywords": ["浏览", "browse", "search", "搜索", "查询", "lookup", "查"]},
    {"id": "general", "label": "WORK", "icon": "⚙️",
     "keywords": []},
]

WORKSTATION_FILE = ".dashboard-workstations.json"

# Cron schedule to human-readable mapping
_CRON_DESCRIPTIONS = {
    "0 1 * * *": "每天 09:00",
    "30 0 * * 1-5": "工作日 08:30",
    "45 0 * * 1-5": "工作日 08:45",
    "17 * * * *": "每小时 :17",
    "*/30 * * * *": "每30分钟",
    "*/30 1-7 * * 1-5": "工作日 09:00-15:00 每30分",
    "30 4 * * *": "每天 12:30",
    "0 12 * * *": "每天 20:00",
    "0 2 * * 4": "周四 10:00",
    "0 10 * * 5": "周五 18:00",
    "30 8 * * 5": "周五 16:30",
    "3 10 * * *": "每天 18:03",
    "0 14 * * *": "每天 22:00",
}

# Cache: path_str -> (mtime, lobster_config, workstations)
_config_cache: dict[str, tuple[float, dict, list[dict]]] = {}


def load_dashboard_config(working_dir: str, project_dir: str = "") -> tuple[dict, list[dict]]:
    """Load lobster + workstation config from JSON, with per-path caching by mtime.

    Searches working_dir first (per-agent), then project_dir (global fallback).
    """
    path = None
    for candidate in [working_dir, project_dir]:
        if candidate:
            p = Path(candidate) / WORKSTATION_FILE
            if p.exists():
                path = p
                break
    if path is None:
        return _DEFAULT_LOBSTER.copy(), _DEFAULT_WORKSTATIONS
    try:
        path_key = str(path)
        mtime = path.stat().st_mtime
        cached = _config_cache.get(path_key)
        if cached and cached[0] == mtime and cached[2]:
            return cached[1], cached[2]
        data = json.loads(path.read_text())
        # Support both formats: list (old) and object (new)
        if isinstance(data, list) and data:
            _config_cache[path_key] = (mtime, _DEFAULT_LOBSTER.copy(), data)
            return _DEFAULT_LOBSTER.copy(), data
        elif isinstance(data, dict):
            lobster = data.get("lobster", _DEFAULT_LOBSTER.copy())
            ws = data.get("workstations", _DEFAULT_WORKSTATIONS)
            if ws:
                _config_cache[path_key] = (mtime, lobster, ws)
                return lobster, ws
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to load %s, using defaults", path, exc_info=True)
    return _DEFAULT_LOBSTER.copy(), _DEFAULT_WORKSTATIONS


def classify_task_state(message: str, workstations: list[dict]) -> str:
    """Classify a task message into a workstation ID."""
    lower = message.lower()
    for ws in workstations:
        keywords = ws.get("keywords", [])
        if keywords and any(kw in lower for kw in keywords):
            return ws["id"]
    return "general"


def _state_label(state: str, workstations: list[dict]) -> str:
    """Get human-readable label for a state."""
    _SPECIAL = {"idle": "空闲", "error": "出错了", "done": "搞定了"}
    if state in _SPECIAL:
        return _SPECIAL[state]
    for ws in workstations:
        if ws["id"] == state:
            return ws.get("label", state)
    return state


def get_agent_status(store: Store, runner, streaming_ref: dict | None = None,
                     working_dir: str = ".", project_dir: str = "") -> dict:
    """Build the full status payload for the dashboard API."""
    lobster_config, workstations = load_dashboard_config(working_dir, project_dir)
    running_task = store.get_running_task()

    if running_task:
        state = classify_task_state(running_task.message, workstations)
        elapsed = 0.0
        if running_task.started_at:
            try:
                started = datetime.fromisoformat(running_task.started_at)
                elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            except (ValueError, TypeError):
                pass

        task_msg = _clean_message(running_task.message)

        agent_info = {
            "state": state,
            "state_label": _state_label(state, workstations),
            "task_id": running_task.id,
            "task_message": task_msg,
            "elapsed_seconds": round(elapsed),
            "streaming_output": (streaming_ref or {}).get("buffer", "")[-2000:],
            "thinking": (streaming_ref or {}).get("thinking", "")[-3000:],
        }
    else:
        agent_info = {
            "state": "idle",
            "state_label": "空闲",
            "task_id": None,
            "task_message": "",
            "elapsed_seconds": 0,
            "streaming_output": "",
            "thinking": "",
        }

    # Check last completed task for "done" flash
    latest = store.get_latest_task_any_user()
    if (
        latest
        and latest.status == TaskStatus.COMPLETED
        and not running_task
        and latest.finished_at
    ):
        try:
            finished = datetime.fromisoformat(latest.finished_at)
            since = (datetime.now(timezone.utc) - finished).total_seconds()
            if since < 10:
                agent_info["state"] = "done"
                agent_info["state_label"] = "搞定了"
                agent_info["task_message"] = _clean_message(latest.message)
        except (ValueError, TypeError):
            pass

    # Process info
    process_info = {"pid": None, "rss_mb": None}
    if runner and runner.is_running and runner._process and runner._process.pid:
        pid = runner._process.pid
        process_info["pid"] = pid
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        process_info["rss_mb"] = int(line.split()[1]) // 1024
                        break
        except (FileNotFoundError, PermissionError, ValueError):
            pass

    # Recent tasks (list_tasks_all_users returns dicts with agent_id)
    recent = []
    for td in store.list_tasks_all_users(limit=15):
        duration = None
        if td.get("started_at") and td.get("finished_at"):
            try:
                s = datetime.fromisoformat(td["started_at"])
                e = datetime.fromisoformat(td["finished_at"])
                duration = round((e - s).total_seconds())
            except (ValueError, TypeError):
                pass
        recent.append({
            "id": td["id"],
            "status": td["status"],
            "message": _clean_message(td["message"])[:200],
            "created_at": td["created_at"],
            "duration": duration,
            "agent_id": td.get("agent_id", ""),
        })

    # Model info from runner
    model_info = {}
    if runner:
        mi = getattr(runner, "model_info", {})
        model_info = {
            "model": mi.get("model", ""),
            "claude_code_version": mi.get("claude_code_version", ""),
            "context_window": mi.get("context_window", 0),
            "max_output_tokens": mi.get("max_output_tokens", 0),
            "input_tokens": mi.get("input_tokens", 0),
            "output_tokens": mi.get("output_tokens", 0),
            "cache_read_tokens": mi.get("cache_read_tokens", 0),
            "cache_creation_tokens": mi.get("cache_creation_tokens", 0),
            "current_context_tokens": mi.get("current_context_tokens", 0),
            "total_cost_usd": mi.get("total_cost_usd", 0),
            "num_turns": mi.get("num_turns", 0),
        }

    # System crontab + structured schedule configs
    system_crons = _get_system_crontab()
    schedule_configs = load_schedule_configs(working_dir)

    # Custom dashboard tabs
    from remote_control.dashboard.tabs import load_tab_configs
    tabs = load_tab_configs(working_dir)

    return {
        "agent": agent_info,
        "process": process_info,
        "model": model_info,
        "recent_tasks": recent,
        "system_crons": system_crons,
        "schedule_configs": schedule_configs,
        "tabs": tabs,
        "lobster": lobster_config,
        "workstations": [{"id": ws["id"], "label": ws.get("label", ws["id"]),
                          "icon": ws.get("icon", "⚙️")} for ws in workstations],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _parse_cron_hours(cron_expr: str, tz_offset: int = 8) -> tuple[float | None, float | None]:
    """Parse a cron expression and return (start_hour, end_hour) in local time.

    Args:
        cron_expr: Standard 5-field cron expression (e.g., "30 0 * * 1-5")
        tz_offset: Hours to add to UTC (default 8 for Beijing time)

    Returns:
        (start_hour, end_hour) as floats (e.g., 8.5 for 08:30).
        end_hour is None for point-in-time tasks, set for range tasks like "*/30 1-7 ...".
    """
    parts = cron_expr.strip().split()
    if len(parts) < 2:
        return None, None
    minute_field, hour_field = parts[0], parts[1]

    try:
        # Parse minute — use first concrete value
        if "/" in minute_field:
            minute = 0.0
        elif "," in minute_field:
            minute = float(minute_field.split(",")[0])
        elif minute_field == "*":
            minute = 0.0
        else:
            minute = float(minute_field)

        # Parse hour field
        if "-" in hour_field and "/" not in hour_field:
            # Range like "1-7" → start and end
            h_start, h_end = hour_field.split("-", 1)
            start = (float(h_start) + minute / 60 + tz_offset) % 24
            end = (float(h_end) + tz_offset) % 24
            return start, end
        elif "/" in hour_field:
            # Step like "*/2" or "1-7/2" — treat as range
            base = hour_field.split("/")[0]
            if "-" in base:
                h_start, h_end = base.split("-", 1)
            elif base == "*":
                h_start, h_end = "0", "23"
            else:
                h_start, h_end = base, base
            start = (float(h_start) + minute / 60 + tz_offset) % 24
            end = (float(h_end) + tz_offset) % 24
            return start, end
        elif hour_field == "*":
            # Every hour — show as full-day point at the minute offset
            start = minute / 60
            return start, None
        elif "," in hour_field:
            h = float(hour_field.split(",")[0])
            start = (h + minute / 60 + tz_offset) % 24
            return start, None
        else:
            start = (float(hour_field) + minute / 60 + tz_offset) % 24
            return start, None
    except (ValueError, IndexError):
        return None, None


def load_schedule_configs(working_dir: str) -> list[dict]:
    """Load structured schedule definitions from .schedules/*.yaml in working dir."""
    sched_dir = Path(working_dir) / ".schedules"
    if not sched_dir.is_dir():
        return []

    schedules = []
    for f in sorted(sched_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text())
            if not isinstance(data, dict) or "name" not in data:
                continue
            cron_expr = data.get("schedule", "")
            start_h, end_h = _parse_cron_hours(cron_expr)
            # Determine frequency type from cron fields
            freq = "daily"
            if cron_expr:
                parts = cron_expr.split()
                if len(parts) >= 5:
                    dow = parts[4]
                    if dow not in ("*", "1-5", "0-6"):
                        freq = "weekly"  # specific day(s) like "5" (Friday)
                    elif "1-5" in dow:
                        freq = "weekday"
                if "/" in parts[0] or "/" in parts[1]:
                    freq = "periodic"
            schedules.append({
                "name": data.get("name", f.stem),
                "schedule": cron_expr,
                "schedule_human": data.get("schedule_human", data.get("schedule", "")),
                "enabled": data.get("enabled", True),
                "timeout": data.get("timeout", 600),
                "file": f.name,
                "prompt": data.get("prompt", ""),
                "working_dir": str(sched_dir.parent),
                "start_hour": start_h,
                "end_hour": end_h,
                "freq": freq,
            })
        except Exception:
            logger.warning("Failed to parse schedule %s", f, exc_info=True)

    return schedules


def _get_system_crontab() -> list[dict]:
    """Read system crontab entries with human-readable schedule (best-effort)."""
    import subprocess
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        entries = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("SHELL=") or line.startswith("PATH="):
                continue
            # Extract task name: prefer trailing comment (# task-name), else script filename
            name = ""
            if " # " in line:
                name = line.rsplit(" # ", 1)[-1].strip()
            if not name:
                # Try to extract from script path (last .sh or .py file)
                import re
                m = re.search(r'([/\w-]+\.(?:sh|py))', line)
                if m:
                    name = Path(m.group(1)).stem
            # Extract cron schedule (first 5 fields) for human display
            parts = line.split()
            if line.startswith("@"):
                schedule_raw = parts[0]
                schedule_human = {"@reboot": "开机启动", "@daily": "每天", "@hourly": "每小时"}.get(schedule_raw, schedule_raw)
            else:
                schedule_raw = " ".join(parts[:5]) if len(parts) >= 5 else ""
                schedule_human = _CRON_DESCRIPTIONS.get(schedule_raw, schedule_raw)
            entries.append({
                "name": name or "(unnamed)",
                "schedule": schedule_human,
                "schedule_raw": schedule_raw,
            })
        return entries
    except Exception:
        return []


def _clean_message(message: str) -> str:
    """Strip system-injected prefixes from a task message."""
    from remote_control.core.utils import clean_message
    return clean_message(message)

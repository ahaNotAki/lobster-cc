"""Task Recall MCP Server — browse and retrieve past task history.

Standalone stdio-based MCP server registered in .mcp.json.
Claude uses these tools to recall past task results across sessions.

Configuration via environment variables (set in .mcp.json):
    AGENT_WORKING_DIR — agent's working directory (.task-archive/ lives here)
    AGENT_ID          — agent's numeric ID (for scoped DB queries)
    DB_PATH           — path to the SQLite database
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from remote_control.core.store import ScopedStore, Store

_store: Store | None = None
_scoped: ScopedStore | None = None


def _get_store() -> ScopedStore:
    global _store, _scoped
    if _scoped is None:
        db_path = os.environ.get("DB_PATH", "")
        agent_id = os.environ.get("AGENT_ID", "")
        if not db_path or not agent_id:
            raise RuntimeError("DB_PATH and AGENT_ID must be set.")
        _store = Store(db_path)
        _store.open()
        _scoped = ScopedStore(_store, agent_id)
    return _scoped


def _get_working_dir() -> str:
    return os.environ.get("AGENT_WORKING_DIR", ".")


_TIME_RANGES = {
    "today": lambda: timedelta(hours=datetime.now(timezone.utc).hour + 1),
    "yesterday": lambda: timedelta(days=2),
    "last_3_days": lambda: timedelta(days=3),
    "last_week": lambda: timedelta(weeks=1),
    "last_month": lambda: timedelta(days=30),
    "all": lambda: timedelta(days=3650),
}


def _parse_time_range(time_range: str) -> tuple[str, str]:
    """Convert a named time range to (start_iso, end_iso) strings."""
    now = datetime.now(timezone.utc)
    delta_fn = _TIME_RANGES.get(time_range, _TIME_RANGES["last_week"])
    start = now - delta_fn()
    return start.isoformat(), now.isoformat()


def _do_recall_tasks(time_range: str = "last_week", limit: int = 30) -> str:
    """Browse past task history. Returns a summary list."""
    store = _get_store()
    start, end = _parse_time_range(time_range)
    tasks = store.recall_tasks(start, end, limit)
    if not tasks:
        return f"No completed tasks found in range: {time_range}."
    lines = []
    for t in tasks:
        icon = "\u2705" if t["status"] == "completed" else "\u274c"
        summary = t["summary"] or t["message"]
        lines.append(f"[{t['date'][:16]}] {icon} {summary}  (id:{t['task_id'][:8]})")
    return "\n".join(lines)


def _do_get_task_detail(task_id: str) -> str:
    """Get the full output of a past task by ID (prefix match supported)."""
    wd = _get_working_dir()
    archive_dir = Path(wd) / ".task-archive"

    # Exact match first
    exact = archive_dir / f"{task_id}.md"
    if exact.exists():
        return exact.read_text(encoding="utf-8")

    # Prefix match (user may pass short ID from recall_tasks)
    if archive_dir.exists():
        for f in archive_dir.iterdir():
            if f.name.startswith(task_id) and f.suffix == ".md":
                return f.read_text(encoding="utf-8")

    # Fallback: check DB output field
    store = _get_store()
    task = store.get_task(task_id)
    if task and task.output:
        return f"# Task: {task.message[:200]}\n\n---\n\n{task.output}"

    return f"Task archive not found for ID: {task_id}"


# --- MCP Server ---

mcp = FastMCP(
    "task-recall",
    instructions=(
        "Browse and retrieve past task history. Use recall_tasks to find tasks by time range, "
        "then get_task_detail to read full output. Works across session resets."
    ),
)


@mcp.tool()
def recall_tasks(time_range: str = "last_week", limit: int = 30) -> str:
    """Browse past task history. Returns a list of task summaries with IDs.

    Args:
        time_range: One of "today", "yesterday", "last_3_days", "last_week", "last_month", "all".
        limit: Maximum number of tasks to return (default 30).
    """
    return _do_recall_tasks(time_range, limit)


@mcp.tool()
def get_task_detail(task_id: str) -> str:
    """Get the full output of a specific past task.

    Args:
        task_id: Task ID (or prefix) from recall_tasks results.
    """
    return _do_get_task_detail(task_id)


if __name__ == "__main__":
    mcp.run()

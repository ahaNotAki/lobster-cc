# Task Archive & Recall System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken SQLite keyword-injection memory system with a task archive + on-demand MCP recall tool, so Claude never loses task history — even across `/new` session resets.

**Architecture:** Task outputs are saved to `.task-archive/{task_id}.md` files after each task completes. Claude generates a one-line `📋` summary which is stored in the `tasks.summary` column. A new MCP server (`recall_server.py`) exposes `recall_tasks` (browse by date) and `get_task_detail` (read full output) tools. The old memory system (SQLite `memories` table, `_inject_memory`, keyword extraction) is removed entirely.

**Tech Stack:** Python 3.12, SQLite, FastMCP (mcp.server.fastmcp), aiohttp, pytest

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/remote_control/core/executor.py` | Modify | Add summary extraction + archive saving, remove `_inject_memory` and `_save_raw_memory` |
| `src/remote_control/core/store.py` | Modify | Remove all memory methods from `ScopedStore`, add `recall_tasks` query |
| `src/remote_control/core/memory.py` | Delete | No longer needed (keyword extraction, context building) |
| `src/remote_control/core/models.py` | Modify | Remove `Memory` dataclass |
| `src/remote_control/core/router.py` | Modify | Remove `/memory` command |
| `src/remote_control/mcp/recall_server.py` | Create | New MCP server with `recall_tasks` and `get_task_detail` tools |
| `src/remote_control/server.py` | Modify | Register recall MCP server in `.mcp.json` |
| `src/remote_control/config.py` | Modify | Remove `MemoryConfig` |
| `tests/test_recall_server.py` | Create | Tests for recall MCP tools |
| `tests/test_memory.py` | Delete | Tests for removed memory module |
| `tests/test_store.py` | Modify | Remove memory-related tests |
| `tests/test_router.py` | Modify | Remove `/memory` tests |
| `tests/test_executor.py` | Modify | Update for removed memory injection |
| `tests/conftest.py` | Modify | Remove memory config if referenced |
| `.gitignore` | Modify | Add `.task-archive/` |
| `deploy.sh` | Modify | Add `--exclude '.task-archive/'` to rsync |

---

### Task 1: Add summary extraction helper + archive saving to executor

**Files:**
- Modify: `src/remote_control/core/executor.py`
- Test: `tests/test_executor.py`

- [ ] **Step 1: Write the failing test for `_extract_summary`**

```python
# tests/test_executor.py — add at the bottom

def test_extract_summary_with_emoji():
    from remote_control.core.executor import _extract_summary
    output = "Some analysis result here.\n\n📋 A股6只持仓分析：招商轮船+6.47%最强"
    assert _extract_summary(output) == "A股6只持仓分析：招商轮船+6.47%最强"


def test_extract_summary_missing():
    from remote_control.core.executor import _extract_summary
    output = "Some output without summary line."
    assert _extract_summary(output) == ""


def test_extract_summary_multiline_picks_last():
    from remote_control.core.executor import _extract_summary
    output = "line 1\n📋 wrong one\nmore text\n📋 correct final summary"
    assert _extract_summary(output) == "correct final summary"


def test_extract_summary_strips_whitespace():
    from remote_control.core.executor import _extract_summary
    output = "output\n📋   spaced summary   \n"
    assert _extract_summary(output) == "spaced summary"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_executor.py::test_extract_summary_with_emoji -v`
Expected: FAIL with `cannot import name '_extract_summary'`

- [ ] **Step 3: Implement `_extract_summary` and archive saving**

In `src/remote_control/core/executor.py`, add the function at module level (after imports):

```python
def _extract_summary(output: str) -> str:
    """Extract 📋 summary line from Claude's output. Returns empty string if not found."""
    for line in reversed(output.strip().split("\n")):
        stripped = line.strip()
        if stripped.startswith("📋"):
            return stripped[1:].strip()
    return ""
```

Then modify `_execute_task` — replace the block at lines 263-268 and line 288 with:

```python
            output = result.output

            # Extract Claude-generated summary, fallback to first line
            task_summary = _extract_summary(output)
            if not task_summary:
                first_line = output.strip().split("\n")[0] if output.strip() else ""
                task_summary = first_line[:150]

            summary = output[-self.config.agent.max_output_length:]
            self.store.update_task_status(
                task_id, TaskStatus.COMPLETED, output=output, summary=task_summary
            )
            task.summary = task_summary
            task.output = output
```

And after the `logger.info("Task %s completed ...")` block (after line 281), replace the `_save_raw_memory` call with archive saving:

```python
            if result.exit_code != 0 and result.error:
                logger.warning("Task %s non-zero exit: %s", task_id[:12], result.error[:200])
                task.error = result.error
                await self.notifier.task_failed(task)
            else:
                # Archive full output to file
                self._archive_task(task_id, task.message, task_summary, output)
```

Add the `_archive_task` method to the `Executor` class:

```python
    def _archive_task(self, task_id: str, message: str, summary: str, output: str) -> None:
        """Save full task output to .task-archive/ for recall. Best-effort."""
        try:
            archive_dir = Path(self.config.agent.default_working_dir) / ".task-archive"
            archive_dir.mkdir(exist_ok=True)
            (archive_dir / f"{task_id}.md").write_text(
                f"# Task: {message[:200]}\n"
                f"Summary: {summary}\n\n"
                f"---\n\n{output}",
                encoding="utf-8",
            )
        except Exception:
            logger.warning("Failed to archive task %s", task_id, exc_info=True)
```

- [ ] **Step 4: Update the system hint to request 📋 summary**

In `_default_system_hint` (line 183-198), add before the closing quote of the return string:

```python
            f"Task summary: End your response with a line starting with 📋 that summarizes "
            f"what was done and the key result in one sentence (under 80 chars, same language as the user). "
```

Also add the same hint to the `.system-prompt.md` template in `_inject_wecom_hint` by appending after the hint string is built (after line 181):

```python
        hint += (
            "\nTask summary: End your response with a line starting with 📋 that summarizes "
            "what was done and the key result in one sentence (under 80 chars, same language as the user)."
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_executor.py -v -k "extract_summary"`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add src/remote_control/core/executor.py tests/test_executor.py
git commit -m "feat: add task summary extraction + archive saving"
```

---

### Task 2: Remove old memory system (memory.py, Memory model, MemoryConfig)

**Files:**
- Delete: `src/remote_control/core/memory.py`
- Modify: `src/remote_control/core/models.py` — remove `Memory` dataclass
- Modify: `src/remote_control/config.py` — remove `MemoryConfig`, remove `memory` field from `AppConfig`
- Modify: `src/remote_control/core/executor.py` — remove `_inject_memory`, `_save_raw_memory`, memory imports
- Delete: `tests/test_memory.py`

- [ ] **Step 1: Remove `_inject_memory` and `_save_raw_memory` from executor**

In `src/remote_control/core/executor.py`:

Remove the import line:
```python
from remote_control.core.memory import extract_keywords, build_context_block
```

Remove the entire `_inject_memory` method (lines 84-124).

Remove the entire `_save_raw_memory` method (lines 126-140).

In `_execute_task`, change line 230 from:
```python
            augmented_message = self._inject_memory(user_id, task.message)
            augmented_message = self._inject_wecom_hint(user_id, augmented_message)
```
to:
```python
            augmented_message = self._inject_wecom_hint(user_id, task.message)
```

- [ ] **Step 2: Remove `Memory` dataclass from models.py**

In `src/remote_control/core/models.py`, delete the entire `Memory` class (lines 44-53).

- [ ] **Step 3: Remove `MemoryConfig` from config.py**

In `src/remote_control/config.py`:

Delete the `MemoryConfig` class (lines 47-53).

Remove `memory` field from `AppConfig`:
```python
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
```

- [ ] **Step 4: Remove memory methods from store.py**

In `src/remote_control/core/store.py`:

From `Store` class, remove:
- `_row_to_memory` static method (lines 198-204)

From `ScopedStore` class, remove all memory methods (lines 363-441):
- `create_memory`
- `get_recent_memories`
- `get_consolidated_memories`
- `get_keyword_matched_memories`
- `clear_memories`
- `get_memory_stats`

Remove `Memory` from the import line at the top:
```python
from remote_control.core.models import Memory, Session, Task, TaskStatus
```
becomes:
```python
from remote_control.core.models import Session, Task, TaskStatus
```

Keep the `memories` table in `_SCHEMA` for now (we'll clean up the DB data on the remote host separately — no schema migration needed since we just stop using it).

- [ ] **Step 5: Delete memory.py and test_memory.py**

```bash
rm src/remote_control/core/memory.py tests/test_memory.py
```

- [ ] **Step 6: Remove memory tests from test_store.py**

In `tests/test_store.py`, delete all tests from line 216 onwards (everything under `# --- Memories ---`):
- `test_create_memory`
- `test_get_recent_memories`
- `test_get_consolidated_memories`
- `test_get_keyword_matched_memories`
- `test_get_keyword_matched_memories_excludes_recent`
- `test_get_keyword_matched_memories_no_matches`
- `test_clear_memories`
- `test_get_keyword_matched_multi_keyword_ranking`
- `test_get_memory_stats`

- [ ] **Step 7: Remove `/memory` command from router**

In `src/remote_control/core/router.py`:

Remove `/memory` from `HELP_TEXT` (line 22):
```
/memory - Show memory stats
/memory show - Show consolidated knowledge
/memory clear - Clear all memory
```

Remove `"/memory": self._handle_memory,` from the handlers dict (line 54).

Delete the entire `_handle_memory` method (lines 180-205).

- [ ] **Step 8: Remove `/memory` tests from test_router.py**

In `tests/test_router.py`, delete the `# --- /memory ---` section (lines 289-328):
- `test_memory_stats`
- `test_memory_show`
- `test_memory_show_empty`
- `test_memory_clear`

- [ ] **Step 9: Remove `memory` references from executor tests**

In `tests/test_executor.py`, remove any mocks or assertions related to `_inject_memory`, `_save_raw_memory`, or `config.memory`. If tests mock `config.memory.enabled`, remove those mocks.

- [ ] **Step 10: Run all tests to verify nothing is broken**

Run: `python -m pytest tests/test_store.py tests/test_router.py tests/test_executor.py -v`
Expected: All remaining tests PASS, no import errors

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "refactor: remove old SQLite memory system (never worked for Chinese)"
```

---

### Task 3: Add `recall_tasks` query to ScopedStore

**Files:**
- Modify: `src/remote_control/core/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py — add at the bottom

from datetime import datetime, timezone, timedelta


def test_recall_tasks_by_date_range(store):
    """recall_tasks returns tasks within the specified date range."""
    t1 = store.create_task("user1", "s1", "old task")
    t2 = store.create_task("user1", "s1", "recent task")
    store.update_task_status(t1.id, TaskStatus.COMPLETED, summary="Old summary")
    store.update_task_status(t2.id, TaskStatus.COMPLETED, summary="Recent summary")

    # Use wide date range to get all
    results = store.recall_tasks(
        time_start="2020-01-01T00:00:00",
        time_end="2030-01-01T00:00:00",
        limit=10,
    )
    assert len(results) == 2
    assert results[0]["summary"] == "Recent summary"  # newest first
    assert results[1]["summary"] == "Old summary"


def test_recall_tasks_empty(store):
    results = store.recall_tasks(
        time_start="2020-01-01T00:00:00",
        time_end="2030-01-01T00:00:00",
    )
    assert results == []


def test_recall_tasks_respects_limit(store):
    for i in range(10):
        t = store.create_task("user1", "s1", f"task {i}")
        store.update_task_status(t.id, TaskStatus.COMPLETED, summary=f"Sum {i}")

    results = store.recall_tasks(
        time_start="2020-01-01T00:00:00",
        time_end="2030-01-01T00:00:00",
        limit=3,
    )
    assert len(results) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_store.py::test_recall_tasks_by_date_range -v`
Expected: FAIL with `AttributeError: 'ScopedStore' object has no attribute 'recall_tasks'`

- [ ] **Step 3: Implement `recall_tasks` in ScopedStore**

Add to `ScopedStore` class in `src/remote_control/core/store.py`:

```python
    def recall_tasks(self, time_start: str, time_end: str, limit: int = 30) -> list[dict]:
        """Return completed/failed tasks in a date range for recall browsing."""
        rows = self.conn.execute(
            "SELECT id, message, summary, status, created_at FROM tasks "
            "WHERE agent_id = ? AND created_at >= ? AND created_at <= ? "
            "AND status IN (?, ?) "
            "ORDER BY created_at DESC LIMIT ?",
            (self._agent_id, time_start, time_end,
             TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, limit),
        ).fetchall()
        return [
            {
                "task_id": row["id"],
                "message": row["message"][:80],
                "summary": row["summary"] or row["message"][:80],
                "status": row["status"],
                "date": row["created_at"],
            }
            for row in rows
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_store.py -v -k "recall"`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/remote_control/core/store.py tests/test_store.py
git commit -m "feat: add recall_tasks query to ScopedStore"
```

---

### Task 4: Create recall MCP server

**Files:**
- Create: `src/remote_control/mcp/recall_server.py`
- Create: `tests/test_recall_server.py`

- [ ] **Step 1: Write the tests**

```python
# tests/test_recall_server.py
"""Tests for the task recall MCP server."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from remote_control.core.models import TaskStatus
from remote_control.core.store import ScopedStore, Store


@pytest.fixture
def recall_env(tmp_path):
    """Set up environment and store for recall server tests."""
    db_path = str(tmp_path / "test.db")
    working_dir = str(tmp_path / "workdir")
    os.makedirs(working_dir, exist_ok=True)

    store = Store(db_path)
    store.open()
    scoped = ScopedStore(store, "1000002")

    env = {
        "AGENT_WORKING_DIR": working_dir,
        "AGENT_ID": "1000002",
        "DB_PATH": db_path,
    }
    yield env, store, scoped, working_dir
    store.close()


def test_recall_tasks_formats_output(recall_env):
    env, store, scoped, wd = recall_env
    t = scoped.create_task("user1", "s1", "analyze stocks")
    store.update_task_status(t.id, TaskStatus.COMPLETED, summary="A股分析完成")

    with patch.dict(os.environ, env):
        from remote_control.mcp.recall_server import _do_recall_tasks
        result = _do_recall_tasks("all", 10)

    assert "A股分析完成" in result
    assert t.id[:8] in result


def test_get_task_detail_from_archive(recall_env):
    env, store, scoped, wd = recall_env
    task_id = "test123"
    archive_dir = Path(wd) / ".task-archive"
    archive_dir.mkdir()
    (archive_dir / f"{task_id}.md").write_text("# Full output here\nDetails...")

    with patch.dict(os.environ, env):
        from remote_control.mcp.recall_server import _do_get_task_detail
        result = _do_get_task_detail(task_id)

    assert "Full output here" in result
    assert "Details..." in result


def test_get_task_detail_not_found(recall_env):
    env, store, scoped, wd = recall_env
    with patch.dict(os.environ, env):
        from remote_control.mcp.recall_server import _do_get_task_detail
        result = _do_get_task_detail("nonexistent")

    assert "not found" in result.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_recall_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'remote_control.mcp.recall_server'`

- [ ] **Step 3: Implement the recall server**

Create `src/remote_control/mcp/recall_server.py`:

```python
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

# ---------------------------------------------------------------------------
# Lazy-init store (created on first tool call)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Core logic (testable without MCP transport)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_recall_server.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/remote_control/mcp/recall_server.py tests/test_recall_server.py
git commit -m "feat: add task recall MCP server with recall_tasks + get_task_detail"
```

---

### Task 5: Register recall server in .mcp.json + deploy config

**Files:**
- Modify: `src/remote_control/server.py`
- Modify: `.gitignore`
- Modify: `deploy.sh`

- [ ] **Step 1: Add recall server to `_write_mcp_json`**

In `src/remote_control/server.py`, in `_write_mcp_json` (line 80), add after the `profile_entry` dict (after line 106):

```python
    recall_entry = {
        "command": python_bin,
        "args": ["-m", "remote_control.mcp.recall_server"],
        "env": {
            "AGENT_WORKING_DIR": str(working_dir),
            "AGENT_ID": str(wecom_config.agent_id),
            "DB_PATH": str(Path(config.storage.db_path).resolve()),
        },
    }
```

And register it alongside the other servers (after line 118):

```python
    servers["task-recall"] = recall_entry
```

- [ ] **Step 2: Add `.task-archive/` to `.gitignore`**

Append to `.gitignore`:
```
.task-archive/
```

- [ ] **Step 3: Add `--exclude '.task-archive/'` to deploy.sh rsync**

In `deploy.sh`, add to the rsync excludes (after the `.agent-profile-history/` exclude):

```
    --exclude '.task-archive/' \
```

- [ ] **Step 4: Run the full test suite**

Run: `python -m pytest tests/ -v --ignore=tests/test_dashboard_routes.py --ignore=tests/test_gateway.py -x`
Expected: All tests PASS (ignoring pre-existing async fixture issues)

- [ ] **Step 5: Commit**

```bash
git add src/remote_control/server.py .gitignore deploy.sh
git commit -m "feat: register recall MCP server in .mcp.json + deploy excludes"
```

---

### Task 6: Update documentation

**Files:**
- Modify: `CLAUDE.md`
- Modify: `DESIGN.md`
- Modify: `NEXT_STEPS.md`

- [ ] **Step 1: Update CLAUDE.md architecture section**

In `CLAUDE.md`, update the `core/memory.py` entry to remove it, and add recall_server:

Replace:
```
- `core/memory.py` — Memory utilities (`extract_keywords`, `build_context_block`, `clean_message`) for task history recall via keyword matching. Supports CJK characters.
```
With:
```
- `mcp/recall_server.py` — Standalone MCP server exposing `recall_tasks` (browse by date range) and `get_task_detail` (read full output) tools for cross-session task history recall. Archives stored in `.task-archive/`.
```

Update the **Persistent memory** section to reflect the new architecture:

Replace the existing paragraph starting with "**Persistent memory**:" with:
```
**Persistent memory**: Long-term knowledge lives in Claude Code's native auto-memory (`~/.claude/projects/.../memory/MEMORY.md`), auto-read at every session start. Task history is archived to `.task-archive/{task_id}.md` files after each completed task, with Claude-generated 📋 summaries stored in the tasks table. The `task-recall` MCP server exposes `recall_tasks` and `get_task_detail` tools so Claude can search and retrieve past task results on demand — even after `/new` session resets. No blind context injection; Claude queries history only when needed.
```

- [ ] **Step 2: Mark items complete in NEXT_STEPS.md**

Mark "Memory feedback loop" as done (replaced by recall system). Add a note that the old SQLite memory system was removed.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md DESIGN.md NEXT_STEPS.md
git commit -m "docs: update architecture for task archive + recall system"
```

---

## Self-Review Checklist

1. **Spec coverage:**
   - [x] Save full task output (not truncated) — Task 1 (archive to `.task-archive/`)
   - [x] Claude generates summary — Task 1 (`📋` extraction + system hint)
   - [x] MCP tools for recall — Task 4 (`recall_tasks` + `get_task_detail`)
   - [x] Remove old memory system — Task 2 (memory.py, Memory model, config, router)
   - [x] Register MCP in .mcp.json — Task 5
   - [x] Update docs — Task 6
   - [x] .gitignore + deploy excludes — Task 5

2. **Placeholder scan:** No TODOs, TBDs, or "implement later" found.

3. **Type consistency:**
   - `_extract_summary(output: str) -> str` — used in Task 1, tested in Task 1
   - `recall_tasks(time_start, time_end, limit)` — defined in Task 3, used in Task 4
   - `_do_recall_tasks` / `_do_get_task_detail` — defined and tested in Task 4

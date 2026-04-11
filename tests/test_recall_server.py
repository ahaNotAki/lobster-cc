"""Tests for the task recall MCP server."""

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
        # Reset module-level singletons
        import remote_control.mcp.recall_server as rs
        rs._store = None
        rs._scoped = None
        result = rs._do_recall_tasks("all", 10)

    assert "A股分析完成" in result
    assert t.id[:8] in result


def test_get_task_detail_from_archive(recall_env):
    env, store, scoped, wd = recall_env
    task_id = "test123"
    archive_dir = Path(wd) / ".task-archive"
    archive_dir.mkdir()
    (archive_dir / f"{task_id}.md").write_text("# Full output here\nDetails...")

    with patch.dict(os.environ, env):
        import remote_control.mcp.recall_server as rs
        rs._store = None
        rs._scoped = None
        result = rs._do_get_task_detail(task_id)

    assert "Full output here" in result
    assert "Details..." in result


def test_get_task_detail_prefix_match(recall_env):
    env, store, scoped, wd = recall_env
    task_id = "abcdef123456"
    archive_dir = Path(wd) / ".task-archive"
    archive_dir.mkdir()
    (archive_dir / f"{task_id}.md").write_text("# Prefix matched content")

    with patch.dict(os.environ, env):
        import remote_control.mcp.recall_server as rs
        rs._store = None
        rs._scoped = None
        result = rs._do_get_task_detail("abcdef12")  # prefix

    assert "Prefix matched content" in result


def test_get_task_detail_not_found(recall_env):
    env, store, scoped, wd = recall_env
    with patch.dict(os.environ, env):
        import remote_control.mcp.recall_server as rs
        rs._store = None
        rs._scoped = None
        result = rs._do_get_task_detail("nonexistent")

    assert "not found" in result.lower()


def test_recall_tasks_empty(recall_env):
    env, store, scoped, wd = recall_env
    with patch.dict(os.environ, env):
        import remote_control.mcp.recall_server as rs
        rs._store = None
        rs._scoped = None
        result = rs._do_recall_tasks("last_week", 10)

    assert "No completed tasks" in result

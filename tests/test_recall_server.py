"""Tests for the task recall MCP server."""

import os
from unittest.mock import patch

import pytest

from remote_control.core.models import TaskStatus
from remote_control.core.store import ScopedStore, Store


@pytest.fixture
def recall_env(tmp_path):
    """Set up environment and store for recall server tests."""
    db_path = str(tmp_path / "test.db")

    store = Store(db_path)
    store.open()
    scoped = ScopedStore(store, "1000002")

    env = {
        "AGENT_ID": "1000002",
        "DB_PATH": db_path,
    }
    yield env, store, scoped
    store.close()


def test_recall_tasks_formats_output(recall_env):
    env, store, scoped = recall_env
    t = scoped.create_task("user1", "s1", "analyze stocks")
    store.update_task_status(t.id, TaskStatus.COMPLETED, summary="A股分析完成")

    with patch.dict(os.environ, env):
        import remote_control.mcp.recall_server as rs
        rs._store = None
        rs._scoped = None
        result = rs._do_recall_tasks("all", 10)

    assert "A股分析完成" in result
    assert t.id[:8] in result


def test_get_task_detail_from_db(recall_env):
    """get_task_detail reads full output from DB."""
    env, store, scoped = recall_env
    t = scoped.create_task("user1", "s1", "analyze stocks")
    store.update_task_status(t.id, TaskStatus.COMPLETED, output="Full output here\nDetails...", summary="A股分析")

    with patch.dict(os.environ, env):
        import remote_control.mcp.recall_server as rs
        rs._store = None
        rs._scoped = None
        result = rs._do_get_task_detail(t.id)

    assert "Full output here" in result
    assert "Details..." in result
    assert "A股分析" in result


def test_get_task_detail_not_found(recall_env):
    env, store, scoped = recall_env
    with patch.dict(os.environ, env):
        import remote_control.mcp.recall_server as rs
        rs._store = None
        rs._scoped = None
        result = rs._do_get_task_detail("nonexistent")

    assert "not found" in result.lower()


def test_get_task_detail_invalid_id(recall_env):
    env, store, scoped = recall_env
    with patch.dict(os.environ, env):
        import remote_control.mcp.recall_server as rs
        rs._store = None
        rs._scoped = None
        result = rs._do_get_task_detail("../etc/passwd")

    assert "invalid" in result.lower()


def test_recall_tasks_empty(recall_env):
    env, store, scoped = recall_env
    with patch.dict(os.environ, env):
        import remote_control.mcp.recall_server as rs
        rs._store = None
        rs._scoped = None
        result = rs._do_recall_tasks("last_week", 10)

    assert "No completed tasks" in result

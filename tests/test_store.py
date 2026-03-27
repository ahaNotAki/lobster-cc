"""Tests for the SQLite store."""

import pytest

from remote_control.core.models import TaskStatus
from remote_control.core.store import ScopedStore, Store


def test_create_and_get_task(store):
    task = store.create_task("user1", "session1", "do something")
    assert task.id
    assert task.status == TaskStatus.QUEUED
    assert task.user_id == "user1"
    assert task.session_id == "session1"
    assert task.created_at

    retrieved = store.get_task(task.id)
    assert retrieved is not None
    assert retrieved.id == task.id
    assert retrieved.message == "do something"


def test_get_task_not_found(store):
    assert store.get_task("nonexistent") is None


def test_get_latest_task(store):
    store.create_task("user1", "s1", "first")
    t2 = store.create_task("user1", "s1", "second")

    latest = store.get_latest_task("user1")
    assert latest is not None
    assert latest.id == t2.id


def test_get_latest_task_no_tasks(store):
    assert store.get_latest_task("nobody") is None


def test_get_latest_task_per_user(store):
    store.create_task("user1", "s1", "user1 task")
    t2 = store.create_task("user2", "s2", "user2 task")

    latest = store.get_latest_task("user2")
    assert latest.id == t2.id
    assert latest.message == "user2 task"


def test_update_task_status_to_running(store):
    task = store.create_task("user1", "s1", "work")
    store.update_task_status(task.id, TaskStatus.RUNNING)

    updated = store.get_task(task.id)
    assert updated.status == TaskStatus.RUNNING
    assert updated.started_at
    assert not updated.finished_at


def test_update_task_status_to_completed(store):
    task = store.create_task("user1", "s1", "work")
    store.update_task_status(task.id, TaskStatus.RUNNING)
    store.update_task_status(task.id, TaskStatus.COMPLETED, output="done", summary="summary")

    completed = store.get_task(task.id)
    assert completed.status == TaskStatus.COMPLETED
    assert completed.output == "done"
    assert completed.summary == "summary"
    assert completed.finished_at


def test_update_task_status_to_failed(store):
    task = store.create_task("user1", "s1", "work")
    store.update_task_status(task.id, TaskStatus.RUNNING)
    store.update_task_status(task.id, TaskStatus.FAILED, error="crash")

    failed = store.get_task(task.id)
    assert failed.status == TaskStatus.FAILED
    assert failed.error == "crash"
    assert failed.finished_at


def test_update_task_status_to_cancelled(store):
    task = store.create_task("user1", "s1", "work")
    store.update_task_status(task.id, TaskStatus.CANCELLED)

    cancelled = store.get_task(task.id)
    assert cancelled.status == TaskStatus.CANCELLED
    assert cancelled.finished_at


def test_get_running_task(store):
    assert store.get_running_task() is None

    task = store.create_task("user1", "s1", "work")
    store.update_task_status(task.id, TaskStatus.RUNNING)

    running = store.get_running_task()
    assert running is not None
    assert running.id == task.id


def test_get_running_task_none_when_completed(store):
    task = store.create_task("user1", "s1", "work")
    store.update_task_status(task.id, TaskStatus.RUNNING)
    store.update_task_status(task.id, TaskStatus.COMPLETED)

    assert store.get_running_task() is None


def test_get_next_queued_task(store):
    t1 = store.create_task("user1", "s1", "first")
    store.create_task("user1", "s1", "second")

    queued = store.get_next_queued_task()
    assert queued.id == t1.id


def test_get_next_queued_task_skips_running(store):
    t1 = store.create_task("user1", "s1", "first")
    t2 = store.create_task("user1", "s1", "second")
    store.update_task_status(t1.id, TaskStatus.RUNNING)

    queued = store.get_next_queued_task()
    assert queued.id == t2.id


def test_get_next_queued_task_none(store):
    assert store.get_next_queued_task() is None


def test_list_tasks(store):
    for i in range(5):
        store.create_task("user1", "s1", f"task {i}")

    tasks = store.list_tasks("user1", limit=3)
    assert len(tasks) == 3
    assert tasks[0].message == "task 4"  # most recent first


def test_list_tasks_empty(store):
    tasks = store.list_tasks("nobody")
    assert tasks == []


def test_list_tasks_per_user(store):
    store.create_task("user1", "s1", "u1 task")
    store.create_task("user2", "s2", "u2 task")

    u1_tasks = store.list_tasks("user1")
    assert len(u1_tasks) == 1
    assert u1_tasks[0].message == "u1 task"


def test_session_create_and_get(store):
    session = store.get_or_create_session("user1", "/default")
    assert session.user_id == "user1"
    assert session.working_dir == "/default"
    assert session.session_id

    # Get again returns same session
    same = store.get_or_create_session("user1", "/other")
    assert same.session_id == session.session_id
    assert same.working_dir == "/default"  # unchanged


def test_session_reset(store):
    old = store.get_or_create_session("user1", "/default")
    new = store.reset_session("user1", "/new_dir")
    assert new.session_id != old.session_id
    assert new.working_dir == "/new_dir"

    # Verify persisted
    fetched = store.get_or_create_session("user1", "/ignored")
    assert fetched.session_id == new.session_id


def test_session_update_working_dir(store):
    store.get_or_create_session("user1", "/default")
    store.update_session_working_dir("user1", "/new_path")

    session = store.get_or_create_session("user1", "/default")
    assert session.working_dir == "/new_path"


def test_session_update_used(store):
    session = store.get_or_create_session("user1", "/default")
    old_used = session.last_used_at
    store.update_session_used("user1")
    updated = store.get_or_create_session("user1", "/default")
    assert updated.last_used_at >= old_used


def test_store_not_opened():
    s = Store("/tmp/not_opened.db")
    with pytest.raises(RuntimeError, match="not opened"):
        s.conn


def test_store_open_close_reopen(tmp_path):
    db = tmp_path / "reopen.db"
    s = Store(db)
    s.open()
    ss = ScopedStore(s, "test")
    ss.create_task("u", "s", "msg")
    s.close()

    s2 = Store(db)
    s2.open()
    ss2 = ScopedStore(s2, "test")
    task = ss2.get_latest_task("u")
    assert task is not None
    assert task.message == "msg"
    s2.close()


# --- Memories ---


def test_create_memory(store):
    mem = store.create_memory("user1", "raw", "Task: fix bug\nResult: done", "fix,bug", source_task="t1")
    assert mem.id
    assert mem.user_id == "user1"
    assert mem.type == "raw"
    assert mem.source_task == "t1"
    assert mem.content == "Task: fix bug\nResult: done"
    assert mem.tags == "fix,bug"


def test_get_recent_memories(store):
    store.create_memory("user1", "raw", "first", "a")
    store.create_memory("user1", "raw", "second", "b")
    store.create_memory("user1", "raw", "third", "c")
    store.create_memory("user2", "raw", "other user", "d")

    recent = store.get_recent_memories("user1", limit=2)
    assert len(recent) == 2
    assert recent[0].content == "third"
    assert recent[1].content == "second"


def test_get_consolidated_memories(store):
    store.create_memory("user1", "raw", "raw entry", "a")
    store.create_memory("user1", "consolidated", "fact one", "", category="facts")
    store.create_memory("user1", "consolidated", "decision one", "", category="decisions")

    consolidated = store.get_consolidated_memories("user1")
    assert len(consolidated) == 2
    assert all(m.type == "consolidated" for m in consolidated)


def test_get_keyword_matched_memories(store):
    store.create_memory("user1", "raw", "fixed auth bug", "auth,bug,fix")
    store.create_memory("user1", "raw", "added oauth flow", "oauth,auth,flow")
    store.create_memory("user1", "raw", "updated readme", "readme,docs")

    matches = store.get_keyword_matched_memories("user1", ["auth"], limit=5, exclude_recent=0)
    assert len(matches) == 2
    assert all("auth" in m.tags for m in matches)


def test_get_keyword_matched_memories_excludes_recent(store):
    m1 = store.create_memory("user1", "raw", "old auth", "auth")
    store.create_memory("user1", "raw", "new auth", "auth")

    matches = store.get_keyword_matched_memories(
        "user1", ["auth"], limit=5, exclude_recent=1
    )
    assert len(matches) == 1
    assert matches[0].id == m1.id


def test_get_keyword_matched_memories_no_matches(store):
    store.create_memory("user1", "raw", "fixed auth bug", "auth,bug")
    matches = store.get_keyword_matched_memories("user1", ["deploy"], limit=5, exclude_recent=0)
    assert matches == []


def test_clear_memories(store):
    store.create_memory("user1", "raw", "entry", "a")
    store.create_memory("user1", "consolidated", "fact", "", category="facts")
    store.create_memory("user2", "raw", "other", "b")

    count = store.clear_memories("user1")
    assert count == 2

    assert store.get_recent_memories("user1", limit=10) == []
    assert len(store.get_recent_memories("user2", limit=10)) == 1


def test_get_keyword_matched_multi_keyword_ranking(store):
    """Multiple keywords should rank by number of matching tags."""
    store.create_memory("user1", "raw", "auth only", "auth")
    store.create_memory("user1", "raw", "auth and bug", "auth,bug")
    store.create_memory("user1", "raw", "unrelated", "docs")

    matches = store.get_keyword_matched_memories("user1", ["auth", "bug"], limit=5, exclude_recent=0)
    assert len(matches) == 2
    # The one with both tags should rank first
    assert matches[0].content == "auth and bug"
    assert matches[1].content == "auth only"




def test_get_memory_stats(store):
    store.create_memory("user1", "raw", "r1", "a")
    store.create_memory("user1", "raw", "r2", "b")
    store.create_memory("user1", "consolidated", "c1", "", category="facts")

    stats = store.get_memory_stats("user1")
    assert stats["raw_count"] == 2
    assert stats["consolidated_count"] == 1


# --- list_tasks_all_users / get_latest_task_any_user ---


def test_list_tasks_all_users(store):
    """Creates tasks for multiple users, verifies all returned sorted by date (newest first)."""
    t1 = store.create_task("user1", "s1", "task A")
    t2 = store.create_task("user2", "s2", "task B")
    t3 = store.create_task("user1", "s1", "task C")

    tasks = store.list_tasks_all_users(limit=10)
    assert len(tasks) == 3
    # Returns dicts, newest first
    assert tasks[0]["id"] == t3.id
    assert tasks[1]["id"] == t2.id
    assert tasks[2]["id"] == t1.id


def test_list_tasks_all_users_empty(store):
    """Empty DB returns []."""
    tasks = store.list_tasks_all_users()
    assert tasks == []


def test_get_latest_task_any_user(store):
    """Returns the newest task across all users."""
    store.create_task("user1", "s1", "older")
    t2 = store.create_task("user2", "s2", "newer")

    latest = store.get_latest_task_any_user()
    assert latest is not None
    assert latest.id == t2.id
    assert latest.message == "newer"


def test_get_latest_task_any_user_empty(store):
    """Empty DB returns None."""
    assert store.get_latest_task_any_user() is None

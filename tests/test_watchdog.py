"""Tests for the ProcessWatchdog."""

import os
import subprocess

import pytest
from unittest.mock import AsyncMock, MagicMock

from remote_control.core.models import TaskStatus
from remote_control.core.store import ScopedStore, Store
from remote_control.core.watchdog import ProcessWatchdog


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.open()
    yield s
    s.close()


@pytest.fixture
def scoped(store):
    return ScopedStore(store, "test")


@pytest.fixture
def mock_notifier():
    n = MagicMock()
    n.task_failed = AsyncMock()
    return n


@pytest.fixture
def watchdog(store, mock_notifier):
    return ProcessWatchdog(
        store=store,
        notifier=mock_notifier,
        timeout_seconds=1,  # 1 second for fast tests
        interval_seconds=0.5,
    )


def test_register_unregister(watchdog):
    watchdog.register(12345, "task-1")
    assert 12345 in watchdog._tracked

    watchdog.unregister(12345)
    assert 12345 not in watchdog._tracked


def test_unregister_nonexistent(watchdog):
    """Unregistering a PID that isn't tracked should not raise."""
    watchdog.unregister(99999)


@pytest.mark.asyncio
async def test_start_stop(watchdog):
    await watchdog.start()
    assert watchdog._task is not None
    await watchdog.stop()


@pytest.mark.asyncio
async def test_watchdog_kills_timed_out_process(store, scoped, mock_notifier):
    """Watchdog should kill a process that exceeds the timeout."""
    # Start a real subprocess (sleep) to have a valid PID
    proc = subprocess.Popen(["sleep", "60"])
    pid = proc.pid

    try:
        watchdog = ProcessWatchdog(
            store=store, notifier=mock_notifier,
            timeout_seconds=0,  # immediate timeout
            interval_seconds=60,  # won't auto-trigger, we call _check manually
        )

        # Create a task in the store so watchdog can update it
        task = scoped.create_task("user1", "session1", "long task")
        store.update_task_status(task.id, TaskStatus.RUNNING)

        watchdog.register(pid, task.id)
        await watchdog._check()

        # Process should be killed
        proc.wait(timeout=5)
        assert proc.returncode is not None  # process terminated

        # Task should be marked failed
        updated = store.get_task(task.id)
        assert updated.status == TaskStatus.FAILED
        assert "watchdog" in updated.error.lower()

        # Notifier should be called
        mock_notifier.task_failed.assert_called_once()

        # PID should be untracked
        assert pid not in watchdog._tracked
    finally:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass


@pytest.mark.asyncio
async def test_watchdog_removes_dead_process(watchdog, store, scoped):
    """Watchdog should clean up tracking for already-dead processes."""
    # Use a PID that definitely doesn't exist
    fake_pid = 99999999
    task = scoped.create_task("user1", "s1", "test")
    watchdog.register(fake_pid, task.id)

    await watchdog._check()

    assert fake_pid not in watchdog._tracked


@pytest.mark.asyncio
async def test_watchdog_ignores_within_timeout(store, scoped, mock_notifier):
    """Watchdog should not kill processes within the timeout."""
    proc = subprocess.Popen(["sleep", "60"])
    pid = proc.pid

    try:
        watchdog = ProcessWatchdog(
            store=store, notifier=mock_notifier,
            timeout_seconds=3600,  # 1 hour — won't expire
            interval_seconds=60,
        )

        task = scoped.create_task("user1", "s1", "ok task")
        store.update_task_status(task.id, TaskStatus.RUNNING)
        watchdog.register(pid, task.id)

        await watchdog._check()

        # Process should still be alive and tracked
        assert pid in watchdog._tracked
        assert ProcessWatchdog._is_alive(pid)

        updated = store.get_task(task.id)
        assert updated.status == TaskStatus.RUNNING
    finally:
        proc.kill()
        proc.wait(timeout=2)


def test_is_alive():
    """Test _is_alive with current process (always alive) and bogus PID."""
    assert ProcessWatchdog._is_alive(os.getpid()) is True
    assert ProcessWatchdog._is_alive(99999999) is False

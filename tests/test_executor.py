"""Tests for the executor (task queue orchestration)."""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from remote_control.config import AgentConfig, AppConfig, NotificationsConfig, WeComConfig
from remote_control.core.executor import Executor
from remote_control.core.models import TaskStatus
from remote_control.core.runner import RunResult
from remote_control.core.store import ScopedStore, Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.open()
    yield ScopedStore(s, "test_agent")
    s.close()


class _FakeStreamHandler:
    """Minimal StreamHandler stand-in for tests."""
    async def on_output(self, text):
        pass

    async def flush(self):
        pass


@pytest.fixture
def mock_notifier():
    n = MagicMock()
    n.send_reply = AsyncMock()
    n.task_started = AsyncMock()
    n.task_completed = AsyncMock()
    n.task_failed = AsyncMock()
    n.task_cancelled = AsyncMock()
    n.task_progress = AsyncMock()
    n.create_stream_handler = MagicMock(return_value=_FakeStreamHandler())
    return n


@pytest.fixture
def mock_runner():
    r = MagicMock()
    r.cancel = AsyncMock()
    r.is_running = False
    return r


@pytest.fixture
def app_config(tmp_path):
    return AppConfig(
        wecom=[WeComConfig(
            corp_id="c", agent_id=1, secret="s", token="t", encoding_aes_key="k"
        )],
        agent=AgentConfig(default_working_dir=str(tmp_path), task_timeout_seconds=5),
        notifications=NotificationsConfig(progress_interval_seconds=1),
    )


@pytest.fixture
def executor(app_config, store, mock_notifier, mock_runner):
    return Executor(app_config, store, mock_notifier, mock_runner)


# --- enqueue_task ---


@pytest.mark.asyncio
async def test_enqueue_task(executor, store, mock_notifier):
    await executor.enqueue_task("user1", "do something")

    # Task should be in DB
    task = store.get_latest_task("user1")
    assert task is not None
    assert task.message == "do something"
    assert task.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_enqueue_creates_session(executor, store):
    await executor.enqueue_task("new_user", "hello")
    session = store.get_or_create_session("new_user", "/default")
    assert session.user_id == "new_user"


# --- cancel_running_task ---


@pytest.mark.asyncio
async def test_cancel_running_task(executor, store, mock_runner, mock_notifier):
    task = store.create_task("user1", "s1", "work")
    store.update_task_status(task.id, TaskStatus.RUNNING)

    await executor.cancel_running_task()

    mock_runner.cancel.assert_called_once()
    updated = store.get_task(task.id)
    assert updated.status == TaskStatus.CANCELLED
    mock_notifier.task_cancelled.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_no_running_task(executor, mock_runner, mock_notifier):
    await executor.cancel_running_task()
    mock_runner.cancel.assert_not_called()
    mock_notifier.task_cancelled.assert_not_called()


# --- _execute_task ---


@pytest.mark.asyncio
async def test_execute_task_success(executor, store, mock_runner, mock_notifier, app_config):
    """Test successful task execution with mocked runner."""
    session = store.get_or_create_session("user1", str(app_config.agent.default_working_dir))

    mock_runner.run = AsyncMock(return_value=RunResult(
        exit_code=0, output="All done! The bug is fixed."
    ))

    task = store.create_task("user1", session.session_id, "fix bug")
    await executor._execute_task(task)

    updated = store.get_task(task.id)
    assert updated.status == TaskStatus.COMPLETED
    assert "All done!" in updated.summary


@pytest.mark.asyncio
async def test_execute_task_runner_error(executor, store, mock_runner, mock_notifier, app_config):
    """Test task failure when runner raises an exception."""
    session = store.get_or_create_session("user1", str(app_config.agent.default_working_dir))

    mock_runner.run = AsyncMock(side_effect=RuntimeError("process crashed"))

    task = store.create_task("user1", session.session_id, "fix bug")
    await executor._execute_task(task)

    updated = store.get_task(task.id)
    assert updated.status == TaskStatus.FAILED
    assert "process crashed" in updated.error
    mock_notifier.task_failed.assert_called_once()


@pytest.mark.asyncio
async def test_execute_task_timeout(executor, store, mock_runner, mock_notifier, app_config):
    """Test task timeout handling."""
    app_config.agent.task_timeout_seconds = 0.1  # very short timeout
    session = store.get_or_create_session("user1", str(app_config.agent.default_working_dir))

    async def slow_run(message, session_id, is_resume, working_dir, on_output=None, on_thinking=None, task_id="", model_override=None):
        await asyncio.sleep(10)  # will be cancelled by timeout
        return RunResult(exit_code=0, output="never reached")

    mock_runner.run = slow_run

    task = store.create_task("user1", session.session_id, "slow task")
    await executor._execute_task(task)

    updated = store.get_task(task.id)
    assert updated.status == TaskStatus.FAILED
    assert "timed out" in updated.error.lower()
    mock_runner.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_execute_task_nonzero_exit(executor, store, mock_runner, mock_notifier, app_config):
    """Test task with non-zero exit code and stderr."""
    session = store.get_or_create_session("user1", str(app_config.agent.default_working_dir))

    mock_runner.run = AsyncMock(return_value=RunResult(
        exit_code=1, output="partial output", error="something went wrong"
    ))

    task = store.create_task("user1", session.session_id, "bad task")
    await executor._execute_task(task)

    # Non-zero exit with error → task_failed is called
    mock_notifier.task_failed.assert_called_once()


# --- start / stop ---


@pytest.mark.asyncio
async def test_start_stop(executor):
    await executor.start()
    assert executor._task is not None
    await executor.stop()


@pytest.mark.asyncio
async def test_stop_without_start(executor):
    """stop() should be safe even if start() was never called."""
    await executor.stop()


# --- memory integration ---


@pytest.mark.asyncio
async def test_execute_task_saves_summary_to_db(executor, store, mock_runner, mock_notifier, app_config):
    """Successful task should save 📋 summary to DB tasks.summary field."""
    session = store.get_or_create_session("user1", str(app_config.agent.default_working_dir))
    mock_runner.run = AsyncMock(return_value=RunResult(exit_code=0, output="Fixed the auth bug\n\n📋 Fixed auth bug"))

    task = store.create_task("user1", session.session_id, "fix auth bug")
    await executor._execute_task(task)

    saved = store.get_task(task.id)
    assert saved.summary == "Fixed auth bug"
    assert "Fixed the auth bug" in saved.output


# --- wecom hint injection ---


def test_inject_wecom_hint(executor):
    result = executor._inject_wecom_hint("user1", "fix the bug")
    assert "user1" in result
    assert "send_wecom_message" in result
    assert "fix the bug" in result


@pytest.mark.asyncio
async def test_execute_task_includes_wecom_hint(executor, store, mock_runner, mock_notifier, app_config):
    """Task message sent to runner should contain the wecom hint."""
    session = store.get_or_create_session("user1", str(app_config.agent.default_working_dir))
    mock_runner.run = AsyncMock(return_value=RunResult(exit_code=0, output="done"))

    task = store.create_task("user1", session.session_id, "run tests")
    await executor._execute_task(task)

    call_args = mock_runner.run.call_args
    message_arg = call_args[0][0]
    assert "send_wecom_message" in message_arg
    assert "user1" in message_arg
    assert "run tests" in message_arg


# --- summary extraction ---


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

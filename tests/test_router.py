"""Tests for the command router."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from remote_control.core.models import Session, Task, TaskStatus
from remote_control.core.router import CommandRouter, HELP_TEXT


@pytest.fixture
def mock_executor():
    executor = MagicMock()
    executor.store = MagicMock()
    executor.notifier = MagicMock()
    executor.notifier.send_reply = AsyncMock()
    executor.enqueue_task = AsyncMock()
    executor.cancel_running_task = AsyncMock()
    executor.config = MagicMock()
    executor.config.agent.default_working_dir = "/test"
    return executor


@pytest.fixture
def router(mock_executor):
    return CommandRouter(mock_executor)


# --- basic routing ---


@pytest.mark.asyncio
async def test_regular_message_enqueues_task(router, mock_executor):
    await router.route("user1", "fix the bug in main.py")
    mock_executor.enqueue_task.assert_called_once_with("user1", "fix the bug in main.py")


@pytest.mark.asyncio
async def test_empty_message_ignored(router, mock_executor):
    await router.route("user1", "")
    mock_executor.enqueue_task.assert_not_called()
    mock_executor.notifier.send_reply.assert_not_called()


@pytest.mark.asyncio
async def test_whitespace_only_ignored(router, mock_executor):
    await router.route("user1", "   \n  ")
    mock_executor.enqueue_task.assert_not_called()


@pytest.mark.asyncio
async def test_case_insensitive_commands(router, mock_executor):
    await router.route("user1", "/HELP")
    mock_executor.notifier.send_reply.assert_called_once_with("user1", HELP_TEXT)


# --- /help ---


@pytest.mark.asyncio
async def test_help_command(router, mock_executor):
    await router.route("user1", "/help")
    mock_executor.notifier.send_reply.assert_called_once_with("user1", HELP_TEXT)


# --- /status ---


@pytest.mark.asyncio
async def test_status_no_tasks(router, mock_executor):
    mock_executor.store.get_latest_task.return_value = None
    await router.route("user1", "/status")
    mock_executor.notifier.send_reply.assert_called_once_with("user1", "No tasks found.")


@pytest.mark.asyncio
async def test_status_latest_task(router, mock_executor):
    task = Task(
        id="abc123", user_id="user1", message="do stuff",
        status=TaskStatus.RUNNING, created_at="2024-01-01", started_at="2024-01-01T00:01:00",
    )
    mock_executor.store.get_latest_task.return_value = task
    await router.route("user1", "/status")
    msg = mock_executor.notifier.send_reply.call_args[0][1]
    assert "abc123" in msg
    assert "running" in msg
    assert "Started:" in msg


@pytest.mark.asyncio
async def test_status_with_specific_id(router, mock_executor):
    task = Task(id="xyz", message="specific", status=TaskStatus.COMPLETED, created_at="2024-01-01", finished_at="2024-01-01T01:00:00")
    mock_executor.store.get_task.return_value = task
    await router.route("user1", "/status xyz")
    mock_executor.store.get_task.assert_called_once_with("xyz")
    msg = mock_executor.notifier.send_reply.call_args[0][1]
    assert "xyz" in msg
    assert "completed" in msg
    assert "Finished:" in msg


@pytest.mark.asyncio
async def test_status_specific_id_not_found(router, mock_executor):
    mock_executor.store.get_task.return_value = None
    await router.route("user1", "/status nonexistent")
    mock_executor.notifier.send_reply.assert_called_once_with("user1", "No tasks found.")


# --- /cancel ---


@pytest.mark.asyncio
async def test_cancel_running(router, mock_executor):
    task = Task(id="abc", status=TaskStatus.RUNNING)
    mock_executor.store.get_running_task.return_value = task
    await router.route("user1", "/cancel")
    mock_executor.cancel_running_task.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_no_running(router, mock_executor):
    mock_executor.store.get_running_task.return_value = None
    await router.route("user1", "/cancel")
    mock_executor.notifier.send_reply.assert_called_once_with("user1", "No running task to cancel.")


@pytest.mark.asyncio
async def test_cancel_specific_running_task(router, mock_executor):
    task = Task(id="abc", status=TaskStatus.RUNNING)
    mock_executor.store.get_task.return_value = task
    await router.route("user1", "/cancel abc")
    mock_executor.cancel_running_task.assert_called_once()
    msg = mock_executor.notifier.send_reply.call_args[0][1]
    assert "abc" in msg
    assert "cancellation" in msg


@pytest.mark.asyncio
async def test_cancel_specific_queued_task(router, mock_executor):
    task = Task(id="def", status=TaskStatus.QUEUED)
    mock_executor.store.get_task.return_value = task
    await router.route("user1", "/cancel def")
    mock_executor.store.update_task_status.assert_called_once_with("def", TaskStatus.CANCELLED)
    msg = mock_executor.notifier.send_reply.call_args[0][1]
    assert "def" in msg
    assert "cancelled" in msg


@pytest.mark.asyncio
async def test_cancel_specific_completed_task(router, mock_executor):
    task = Task(id="ghi", status=TaskStatus.COMPLETED)
    mock_executor.store.get_task.return_value = task
    await router.route("user1", "/cancel ghi")
    msg = mock_executor.notifier.send_reply.call_args[0][1]
    assert "already completed" in msg


@pytest.mark.asyncio
async def test_cancel_specific_not_found(router, mock_executor):
    mock_executor.store.get_task.return_value = None
    await router.route("user1", "/cancel zzz")
    mock_executor.notifier.send_reply.assert_called_once_with("user1", "Task zzz not found.")


# --- /list ---


@pytest.mark.asyncio
async def test_list_tasks(router, mock_executor):
    tasks = [
        Task(id="abc", message="first task", status=TaskStatus.COMPLETED),
        Task(id="def", message="second task", status=TaskStatus.QUEUED),
    ]
    mock_executor.store.list_tasks.return_value = tasks
    await router.route("user1", "/list")
    msg = mock_executor.notifier.send_reply.call_args[0][1]
    assert "#abc" in msg
    assert "#def" in msg
    assert "completed" in msg
    assert "queued" in msg


@pytest.mark.asyncio
async def test_list_no_tasks(router, mock_executor):
    mock_executor.store.list_tasks.return_value = []
    await router.route("user1", "/list")
    mock_executor.notifier.send_reply.assert_called_once_with("user1", "No tasks found.")


# --- /new ---


@pytest.mark.asyncio
async def test_new_session(router, mock_executor):
    mock_executor.store.reset_session.return_value = Session(
        user_id="user1", session_id="new-uuid-1234"
    )
    await router.route("user1", "/new")
    mock_executor.store.reset_session.assert_called_once_with("user1", "/test")
    msg = mock_executor.notifier.send_reply.call_args[0][1]
    assert "new-uuid" in msg


# --- /cd ---


@pytest.mark.asyncio
async def test_cd_set_dir(router, mock_executor, tmp_path):
    d = str(tmp_path)
    await router.route("user1", f"/cd {d}")
    mock_executor.store.update_session_working_dir.assert_called_once_with("user1", d)
    msg = mock_executor.notifier.send_reply.call_args[0][1]
    assert d in msg


@pytest.mark.asyncio
async def test_cd_nonexistent_dir(router, mock_executor):
    await router.route("user1", "/cd /nonexistent/path/xyz")
    msg = mock_executor.notifier.send_reply.call_args[0][1]
    assert "not found" in msg.lower()
    mock_executor.store.update_session_working_dir.assert_not_called()


@pytest.mark.asyncio
async def test_cd_show_current(router, mock_executor):
    mock_executor.store.get_or_create_session.return_value = Session(
        user_id="user1", working_dir="/current"
    )
    await router.route("user1", "/cd")
    msg = mock_executor.notifier.send_reply.call_args[0][1]
    assert "/current" in msg


# --- /output ---


@pytest.mark.asyncio
async def test_output_with_task(router, mock_executor):
    task = Task(id="abc", output="full output content here")
    mock_executor.store.get_task.return_value = task
    await router.route("user1", "/output abc")
    mock_executor.notifier.send_reply.assert_called_once_with("user1", "full output content here")


@pytest.mark.asyncio
async def test_output_task_no_output(router, mock_executor):
    task = Task(id="abc", output="")
    mock_executor.store.get_task.return_value = task
    await router.route("user1", "/output abc")
    mock_executor.notifier.send_reply.assert_called_once_with("user1", "(no output)")


@pytest.mark.asyncio
async def test_output_no_arg(router, mock_executor):
    await router.route("user1", "/output")
    mock_executor.notifier.send_reply.assert_called_once_with("user1", "Usage: /output <task_id>")


@pytest.mark.asyncio
async def test_output_task_not_found(router, mock_executor):
    mock_executor.store.get_task.return_value = None
    await router.route("user1", "/output zzz")
    mock_executor.notifier.send_reply.assert_called_once_with("user1", "Task zzz not found.")


# --- /clear ---


@pytest.mark.asyncio
async def test_clear_tasks(router, mock_executor):
    mock_executor.store.clear_tasks.return_value = 5
    await router.route("user1", "/clear")
    mock_executor.store.clear_tasks.assert_called_once_with("user1")
    msg = mock_executor.notifier.send_reply.call_args[0][1]
    assert "5" in msg


# --- /cron routed as regular task ---


@pytest.mark.asyncio
async def test_cron_message_routed_as_task(router, mock_executor):
    """Cron-like messages are now routed as regular tasks for Claude to handle."""
    await router.route("user1", "/cron add every day at 9am run tests")
    mock_executor.enqueue_task.assert_called_once_with(
        "user1", "/cron add every day at 9am run tests"
    )


# --- /memory ---


@pytest.mark.asyncio
async def test_memory_stats(router, mock_executor):
    mock_executor.store.get_memory_stats.return_value = {
        "raw_count": 15, "consolidated_count": 5
    }
    await router.route("user1", "/memory")
    msg = mock_executor.notifier.send_reply.call_args[0][1]
    assert "15" in msg
    assert "5" in msg


@pytest.mark.asyncio
async def test_memory_show(router, mock_executor):
    from remote_control.core.models import Memory
    mock_executor.store.get_consolidated_memories.return_value = [
        Memory(type="consolidated", content="Uses JWT", category="facts"),
    ]
    await router.route("user1", "/memory show")
    msg = mock_executor.notifier.send_reply.call_args[0][1]
    assert "Uses JWT" in msg


@pytest.mark.asyncio
async def test_memory_show_empty(router, mock_executor):
    mock_executor.store.get_consolidated_memories.return_value = []
    await router.route("user1", "/memory show")
    msg = mock_executor.notifier.send_reply.call_args[0][1]
    assert "no" in msg.lower() or "empty" in msg.lower()


@pytest.mark.asyncio
async def test_memory_clear(router, mock_executor):
    mock_executor.store.clear_memories.return_value = 10
    await router.route("user1", "/memory clear")
    mock_executor.store.clear_memories.assert_called_once_with("user1")
    msg = mock_executor.notifier.send_reply.call_args[0][1]
    assert "10" in msg or "cleared" in msg.lower()

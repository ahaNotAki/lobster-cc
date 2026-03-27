"""Tests for the notifier."""


import pytest
from unittest.mock import AsyncMock, MagicMock

from remote_control.config import NotificationsConfig
from remote_control.core.models import Task
from remote_control.core.notifier import Notifier, _split_text


@pytest.fixture
def mock_api():
    api = MagicMock()
    api.send_markdown = AsyncMock(return_value={"errcode": 0})
    api.send_text = AsyncMock(return_value={"errcode": 0})
    api.upload_and_send_file = AsyncMock(return_value={"errcode": 0})
    return api


@pytest.fixture
def notifier(mock_api):
    return Notifier(mock_api, NotificationsConfig(progress_interval_seconds=1))


# --- task_started ---


@pytest.mark.asyncio
async def test_task_started(notifier, mock_api):
    task = Task(id="abc", user_id="user1", message="do something important")
    await notifier.task_started(task)
    mock_api.send_text.assert_called_once()
    msg = mock_api.send_text.call_args[0][1]
    assert "abc" in msg
    assert "started" in msg.lower()
    assert "do something important" in msg


@pytest.mark.asyncio
async def test_task_started_long_message_truncated(notifier, mock_api):
    task = Task(id="abc", user_id="user1", message="x" * 200)
    await notifier.task_started(task)
    msg = mock_api.send_text.call_args[0][1]
    assert "..." in msg
    assert len(msg) < 250


# --- task_completed ---


@pytest.mark.asyncio
async def test_task_completed_short(notifier, mock_api):
    task = Task(id="abc", user_id="user1", summary="Changes made successfully")
    await notifier.task_completed(task)
    msg = mock_api.send_text.call_args[0][1]
    assert "completed" in msg.lower()
    assert "Changes made successfully" in msg


@pytest.mark.asyncio
async def test_task_completed_uses_output_when_no_summary(notifier, mock_api):
    task = Task(id="abc", user_id="user1", summary="", output="raw output text")
    await notifier.task_completed(task)
    msg = mock_api.send_text.call_args[0][1]
    assert "raw output text" in msg


@pytest.mark.asyncio
async def test_task_completed_medium_via_send_text(notifier, mock_api):
    """Medium-length output (1800-5400 chars) should be sent as plain text."""
    task = Task(id="abc", user_id="user1", summary="x" * 3000)
    await notifier.task_completed(task)
    mock_api.send_text.assert_called_once()
    mock_api.send_markdown.assert_not_called()


@pytest.mark.asyncio
async def test_task_completed_very_long_sends_file(notifier, mock_api):
    """Very long output (>5400 chars) should be uploaded as a file."""
    task = Task(id="abc", user_id="user1", summary="x" * 6000)
    await notifier.task_completed(task)
    mock_api.send_text.assert_called_once()  # header
    mock_api.upload_and_send_file.assert_called_once()
    call_args = mock_api.upload_and_send_file.call_args
    assert "task_abc.md" in call_args.args


# --- task_failed ---


@pytest.mark.asyncio
async def test_task_failed(notifier, mock_api):
    task = Task(id="abc", user_id="user1", error="Process exited with code 1")
    await notifier.task_failed(task)
    msg = mock_api.send_text.call_args[0][1]
    assert "failed" in msg.lower() or "Task failed" in msg
    assert "Process exited with code 1" in msg


@pytest.mark.asyncio
async def test_task_failed_default_error(notifier, mock_api):
    task = Task(id="abc", user_id="user1", error="")
    await notifier.task_failed(task)
    msg = mock_api.send_text.call_args[0][1]
    assert "Unknown error" in msg


@pytest.mark.asyncio
async def test_task_failed_uses_plain_text(notifier, mock_api):
    """Failed task notifications should use plain text, not markdown."""
    task = Task(id="abc", user_id="user1", error="some error")
    await notifier.task_failed(task)
    mock_api.send_text.assert_called_once()
    mock_api.send_markdown.assert_not_called()


@pytest.mark.asyncio
async def test_task_failed_long_error_sends_file(notifier, mock_api):
    """Very long errors should be sent as text header + file."""
    task = Task(id="abc", user_id="user1", error="e" * 6000)
    await notifier.task_failed(task)
    mock_api.send_text.assert_called_once()
    mock_api.upload_and_send_file.assert_called_once()


# --- task_cancelled ---


@pytest.mark.asyncio
async def test_task_cancelled(notifier, mock_api):
    task = Task(id="abc", user_id="user1", message="fix the bug")
    await notifier.task_cancelled(task)
    msg = mock_api.send_text.call_args[0][1]
    assert "cancelled" in msg.lower()
    assert "fix the bug" in msg


# --- progress ---


@pytest.mark.asyncio
async def test_progress_throttling(notifier, mock_api):
    task = Task(id="abc", user_id="user1")
    notifier.reset_progress_timer()

    await notifier.task_progress(task, "Reading file")
    assert mock_api.send_text.call_count == 1

    # Second call within interval should be suppressed
    await notifier.task_progress(task, "Editing file")
    assert mock_api.send_text.call_count == 1


@pytest.mark.asyncio
async def test_progress_after_interval(mock_api):
    notifier = Notifier(mock_api, NotificationsConfig(progress_interval_seconds=0))
    task = Task(id="abc", user_id="user1")
    notifier.reset_progress_timer()

    await notifier.task_progress(task, "First action")
    await notifier.task_progress(task, "Second action")
    assert mock_api.send_text.call_count == 2


@pytest.mark.asyncio
async def test_progress_message_content(notifier, mock_api):
    task = Task(id="xyz", user_id="user1")
    notifier.reset_progress_timer()
    await notifier.task_progress(task, "Reading main.py")
    msg = mock_api.send_text.call_args[0][1]
    assert "xyz" in msg
    assert "Reading main.py" in msg


# --- send_reply ---


@pytest.mark.asyncio
async def test_send_reply_short(notifier, mock_api):
    await notifier.send_reply("user1", "hello")
    mock_api.send_text.assert_called_once_with("user1", "hello")


@pytest.mark.asyncio
async def test_send_reply_long_delegates_to_send_text(notifier, mock_api):
    """Long replies should be delegated to send_text which handles splitting."""
    await notifier.send_reply("user1", "x" * 4000)
    mock_api.send_text.assert_called_once_with("user1", "x" * 4000)


# --- reset_progress_timer ---


def test_reset_progress_timer(mock_api):
    notifier = Notifier(mock_api, NotificationsConfig(progress_interval_seconds=1))
    notifier._last_progress_time = 999.0
    notifier.reset_progress_timer()
    assert notifier._last_progress_time == 0


# --- _split_text ---


def test_split_text_short():
    assert _split_text("hello", 100) == ["hello"]


def test_split_text_exact():
    text = "x" * 100
    assert _split_text(text, 100) == [text]


def test_split_text_splits_at_newline():
    text = "line1\nline2\nline3"
    chunks = _split_text(text, 12)
    assert len(chunks) >= 2
    # Recombined should equal original
    assert "".join(chunks) == text


def test_split_text_hard_cut_no_newlines():
    text = "x" * 300
    chunks = _split_text(text, 100)
    assert len(chunks) == 3
    assert "".join(chunks) == text


def test_split_text_empty():
    assert _split_text("", 100) == []


# --- StreamHandler ---


@pytest.fixture
def stream_handler(mock_api):
    from remote_control.core.notifier import StreamHandler
    return StreamHandler(api=mock_api, user_id="user1", task_id="t1", interval=10.0)


@pytest.mark.asyncio
async def test_stream_handler_flush_sends_buffered(stream_handler, mock_api):
    stream_handler._buffer = ["line1\n", "line2\n"]
    await stream_handler.flush()
    mock_api.send_text.assert_called_once()
    sent = mock_api.send_text.call_args[0][1]
    assert "line1" in sent
    assert "line2" in sent


@pytest.mark.asyncio
async def test_stream_handler_flush_empty_noop(stream_handler, mock_api):
    await stream_handler.flush()
    mock_api.send_text.assert_not_called()


@pytest.mark.asyncio
async def test_stream_handler_on_output_respects_interval(stream_handler, mock_api):
    """Output within interval should be buffered, not sent."""
    import time
    stream_handler._last_send_time = time.monotonic()  # just sent
    await stream_handler.on_output("new line\n")
    mock_api.send_text.assert_not_called()
    assert len(stream_handler._buffer) == 1


@pytest.mark.asyncio
async def test_stream_handler_on_output_sends_after_interval(mock_api):
    from remote_control.core.notifier import StreamHandler
    handler = StreamHandler(api=mock_api, user_id="u1", task_id="t1", interval=0.0)
    handler._last_send_time = 0  # long ago
    await handler.on_output("hello\n")
    mock_api.send_text.assert_called_once()


@pytest.mark.asyncio
async def test_stream_handler_rate_limit(mock_api):
    from remote_control.core.notifier import StreamHandler, _MAX_SENDS_PER_MINUTE
    handler = StreamHandler(api=mock_api, user_id="u1", task_id="t1", interval=0.0)
    handler._sends_this_minute = _MAX_SENDS_PER_MINUTE
    handler._minute_start = __import__("time").monotonic()  # current minute
    await handler.on_output("should be rate limited\n")
    mock_api.send_text.assert_not_called()


@pytest.mark.asyncio
async def test_stream_handler_sends_full_long_content(stream_handler, mock_api):
    """Long content should be sent in full (send_text handles splitting)."""
    stream_handler._buffer = ["x" * 3000]
    await stream_handler.flush()
    sent = mock_api.send_text.call_args[0][1]
    assert len(sent) == 3000  # full content passed to send_text


@pytest.mark.asyncio
async def test_stream_handler_first_send_has_label(mock_api):
    from remote_control.core.notifier import StreamHandler
    handler = StreamHandler(api=mock_api, user_id="u1", task_id="t1", interval=0.0, task_label="fix bug")
    handler._last_send_time = 0
    await handler.on_output("working on it\n")
    sent = mock_api.send_text.call_args[0][1]
    assert "fix bug" in sent
    assert "📌" in sent
    # Second send should NOT have label
    mock_api.send_text.reset_mock()
    await handler.on_output("still working\n")
    sent2 = mock_api.send_text.call_args[0][1]
    assert "📌" not in sent2


# --- _task_label ---


def test_task_label_basic():
    from remote_control.core.notifier import _task_label
    assert _task_label("fix the auth bug") == "fix the auth bug"


def test_task_label_truncates():
    from remote_control.core.notifier import _task_label
    long_msg = "a" * 100
    result = _task_label(long_msg)
    assert len(result) <= 53  # 50 + "..."
    assert result.endswith("...")


def test_task_label_strips_system_prefix():
    from remote_control.core.notifier import _task_label
    msg = "[System: some hints here]\n\nfix the bug"
    assert _task_label(msg) == "fix the bug"


def test_task_label_strips_context_prefix():
    from remote_control.core.notifier import _task_label
    msg = "<context>\n## Recent\n- stuff\n</context>\nfix the bug"
    assert _task_label(msg) == "fix the bug"

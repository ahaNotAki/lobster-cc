"""Notification formatting and sending to WeCom."""

import logging
import tempfile
import time
from pathlib import Path

from remote_control.config import NotificationsConfig
from remote_control.core.models import Task
from remote_control.wecom.api import WeComAPI, WECOM_MAX_TEXT_BYTES

logger = logging.getLogger(__name__)

# Conservative limit per message (leave room for formatting/headers)
_MAX_CONTENT_CHARS = 1800
# If content exceeds this many chars, send as file instead of splitting
_FILE_THRESHOLD_CHARS = 5400  # ~3 messages worth
# WeCom rate limit: 30 msgs/min per user. Stay well under.
_MAX_SENDS_PER_MINUTE = 25


class Notifier:
    def __init__(self, api: WeComAPI, config: NotificationsConfig):
        self._api = api
        self._config = config
        self._last_progress_time: float = 0

    async def task_started(self, task: Task) -> None:
        preview = task.message[:100]
        if len(task.message) > 100:
            preview += "..."
        await self._api.send_text(
            task.user_id,
            f"▶️ Task #{task.id} started\n{preview}",
        )

    async def task_progress(self, task: Task, action: str) -> None:
        """Send progress if enough time has passed since the last update."""
        now = time.monotonic()
        if now - self._last_progress_time < self._config.progress_interval_seconds:
            return
        self._last_progress_time = now
        await self._api.send_text(
            task.user_id,
            f"⏳ Task #{task.id} working...\n{action[:200]}",
        )

    async def task_completed(self, task: Task) -> None:
        content = task.summary or task.output or "(no output)"
        cmd_label = _task_label(task.message)
        header = f"📌 {cmd_label}\n✅ Task completed"
        await self._send_long(task.user_id, header, content, task.id)

    async def task_failed(self, task: Task) -> None:
        content = task.error or "Unknown error"
        cmd_label = _task_label(task.message)
        header = f"📌 {cmd_label}\n❌ Task failed"
        await self._send_long(task.user_id, header, content, task.id)

    async def task_cancelled(self, task: Task) -> None:
        cmd_label = _task_label(task.message)
        await self._api.send_text(
            task.user_id,
            f"📌 {cmd_label}\n🚫 Task cancelled",
        )

    async def send_reply(self, user_id: str, text: str) -> None:
        """Send a text reply, splitting into multiple messages if needed."""
        await self._send_text_smart(user_id, text)

    async def send_image(self, user_id: str, file_path: str | Path) -> None:
        """Upload and send an image file to the user."""
        path = Path(file_path)
        logger.info("Sending image %s to %s", path.name, user_id)
        await self._api.upload_and_send_image(user_id, path)

    async def send_file(self, user_id: str, file_path: str | Path) -> None:
        """Upload and send a file to the user."""
        path = Path(file_path)
        logger.info("Sending file %s to %s", path.name, user_id)
        await self._api.upload_and_send_file(user_id, path, path.name)

    def reset_progress_timer(self) -> None:
        self._last_progress_time = 0

    def create_stream_handler(
        self, user_id: str, task_id: str, task_message: str = "",
        dashboard_ref: dict | None = None,
    ) -> "StreamHandler":
        """Create a streaming output handler for a running task."""
        return StreamHandler(
            api=self._api,
            user_id=user_id,
            task_id=task_id,
            interval=self._config.streaming_interval_seconds,
            task_label=_task_label(task_message) if task_message else "",
            dashboard_ref=dashboard_ref,
        )

    async def _send_long(self, user_id: str, header: str, content: str, task_id: str) -> None:
        """Send a long response as plain text: inline if short, split if medium, file if very long."""
        full = f"{header}\n\n{content}"
        if len(content) <= _FILE_THRESHOLD_CHARS:
            await self._api.send_text(user_id, full)
            return

        # Very long: send header + upload as file
        await self._api.send_text(
            user_id, f"{header}\n\nOutput is {len(content)} chars — sent as file."
        )
        await self._send_as_file(user_id, content, f"task_{task_id}.md")

    async def _send_text_smart(self, user_id: str, text: str) -> None:
        """Send text, delegating splitting to send_text (byte-level split with retry)."""
        await self._api.send_text(user_id, text)

    async def _send_as_file(self, user_id: str, content: str, filename: str) -> None:
        """Write content to a temp file and upload+send as WeCom file message."""
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".md", prefix="rc_", delete=False,
            ) as f:
                f.write(content)
                tmp_path = f.name
            await self._api.upload_and_send_file(user_id, tmp_path, filename)
        except Exception:
            logger.exception("Failed to send file, falling back to truncated text")
            truncated = content[:_MAX_CONTENT_CHARS] + f"\n...\n(Full output: {len(content)} chars)"
            await self._api.send_text(user_id, truncated)
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)


class StreamHandler:
    """Buffers streaming output and sends chunks at a throttled rate.

    WeCom rate limit: 30 msgs/min per user. We stay under _MAX_SENDS_PER_MINUTE.
    """

    def __init__(self, api: WeComAPI, user_id: str, task_id: str, interval: float,
                 task_label: str = "", dashboard_ref: dict | None = None):
        self._api = api
        self._user_id = user_id
        self._task_id = task_id
        self._interval = interval
        self._task_label = task_label
        self._dashboard_ref = dashboard_ref
        self._buffer: list[str] = []
        self._all_output: list[str] = []  # full output for dashboard
        self._last_send_time: float = 0.0
        self._sends_this_minute: int = 0
        self._minute_start: float = 0.0
        self._first_send = True

    async def on_output(self, text: str) -> None:
        """Called per line of stdout. Buffers and sends periodically."""
        self._buffer.append(text)
        self._all_output.append(text)
        # Update dashboard shared buffer
        if self._dashboard_ref is not None:
            full = "".join(self._all_output)
            self._dashboard_ref["buffer"] = full[-3000:] if len(full) > 3000 else full
        now = time.monotonic()

        # Reset per-minute counter
        if now - self._minute_start >= 60:
            self._sends_this_minute = 0
            self._minute_start = now

        # Check rate limit
        if self._sends_this_minute >= _MAX_SENDS_PER_MINUTE:
            return

        # Check interval
        if now - self._last_send_time < self._interval:
            return

        await self.flush()

    async def flush(self) -> None:
        """Send any buffered output."""
        if not self._buffer:
            return
        chunk = "".join(self._buffer).rstrip()
        self._buffer.clear()
        if not chunk:
            return

        # Prepend task label on the first streaming message
        if self._first_send and self._task_label:
            chunk = f"📌 {self._task_label}\n\n{chunk}"
            self._first_send = False

        await self._api.send_text(self._user_id, chunk)
        self._last_send_time = time.monotonic()
        self._sends_this_minute += 1


def _task_label(message: str, max_len: int = 50) -> str:
    """Extract a short label from the user's original task message."""
    from remote_control.core.memory import clean_message
    first_line = clean_message(message).split("\n")[0].strip()
    if len(first_line) > max_len:
        return first_line[:max_len] + "..."
    return first_line


def _split_text(text: str, max_chars: int) -> list[str]:
    """Split text into chunks, trying to break at newlines."""
    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break

        # Try to find a newline near the limit to break cleanly
        cut = remaining[:max_chars]
        last_nl = cut.rfind("\n")
        if last_nl > max_chars // 2:
            # Break at newline
            chunks.append(remaining[:last_nl + 1])
            remaining = remaining[last_nl + 1:]
        else:
            # No good newline, hard cut
            chunks.append(cut)
            remaining = remaining[max_chars:]

    return chunks

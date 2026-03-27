"""Executor — orchestrates task queue, agent runner, and notifications."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from remote_control.config import AppConfig
from remote_control.core.memory import extract_keywords, build_context_block
from remote_control.core.models import TaskStatus
from remote_control.core.notifier import Notifier
from remote_control.core.runner import AgentRunner, RunResult
from remote_control.core.store import Store

logger = logging.getLogger(__name__)


class Executor:
    def __init__(self, config: AppConfig, store: Store, notifier: Notifier, runner: AgentRunner):
        self.config = config
        self.store = store
        self.notifier = notifier
        self.runner = runner
        self._queue_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        # Shared streaming buffer for dashboard API
        self.dashboard_streaming: dict = {"buffer": ""}

    async def start(self) -> None:
        """Start the background task processing loop."""
        self._task = asyncio.create_task(self._process_loop())

    async def stop(self) -> None:
        """Stop the processing loop and cancel any running task."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.cancel_running_task()

    async def enqueue_task(self, user_id: str, message: str) -> None:
        """Create a task and wake the processing loop."""
        session = self.store.get_or_create_session(
            user_id, self.config.agent.default_working_dir
        )
        task = self.store.create_task(user_id, session.session_id, message)
        logger.info("Task %s enqueued for user %s", task.id, user_id)
        # Notify user if task is queued behind a running EXECUTOR task (not cron tasks)
        running = self.store.get_running_task()
        if running and running.user_id != "cron":
            preview = message[:60] + ("..." if len(message) > 60 else "")
            await self.notifier.send_reply(
                user_id, f"📥 收到，排队中（当前有任务运行）\n> {preview}"
            )
        self._queue_event.set()

    async def cancel_running_task(self) -> None:
        """Cancel the currently running Claude Code process."""
        running = self.store.get_running_task()
        if running:
            await self.runner.cancel()
            self.store.update_task_status(running.id, TaskStatus.CANCELLED)
            await self.notifier.task_cancelled(running)

    async def _process_loop(self) -> None:
        """Main loop: pick up queued tasks and run them sequentially."""
        while True:
            task = self.store.get_next_queued_task()
            if not task:
                self._queue_event.clear()
                await self._queue_event.wait()
                continue

            await self._execute_task(task)

    def _inject_memory(self, user_id: str, message: str) -> str:
        """Inject keyword-matched task history into the message.

        Long-term knowledge lives in Claude's native MEMORY.md (auto-read by Claude Code).
        This only injects relevant task history from SQLite for contextual recall.
        Best-effort — failures are logged but never break task execution.
        """
        if not self.config.memory.enabled:
            return message

        try:
            mc = self.config.memory
            keywords = [k for k in extract_keywords(message).split(",") if k]
            keyword_matches = self.store.get_keyword_matched_memories(
                user_id, keywords, limit=mc.keyword_match_limit,
                exclude_recent=mc.recent_context_limit,
            )
            recent = self.store.get_recent_memories(user_id, limit=mc.recent_context_limit)
            context = build_context_block(recent, keyword_matches, max_chars=mc.max_context_chars)
            if context:
                return f"{context}\n\n{message}"
        except Exception:
            logger.warning("Failed to inject memory context for user %s", user_id, exc_info=True)

        return message

    def _save_raw_memory(self, user_id: str, task_id: str, message: str, output: str) -> None:
        """Save a raw memory entry after successful task completion.

        Best-effort — failures are logged but never affect task status.
        """
        if not self.config.memory.enabled:
            return
        try:
            mc = self.config.memory
            truncated_output = output[-mc.raw_summary_max_chars:]
            content = f"Task: {message}\nResult: {truncated_output}"
            tags = extract_keywords(message)
            self.store.create_memory(user_id, "raw", content, tags, source_task=task_id)
        except Exception:
            logger.warning("Failed to save raw memory for task %s", task_id, exc_info=True)

    def _inject_wecom_hint(self, user_id: str, message: str) -> str:
        """Load per-agent system prompt from .system-prompt.md, fallback to default."""
        working_dir = self.config.agent.default_working_dir
        prompt_path = Path(working_dir) / ".system-prompt.md"

        hint = ""
        if prompt_path.exists():
            try:
                template = prompt_path.read_text().strip()
                if template:
                    hint = template.replace("{user_id}", user_id)
            except OSError:
                logger.warning("Failed to read %s, using default", prompt_path)

        if not hint:
            hint = self._default_system_hint(user_id)

        return f"[System: {hint}]\n\n{message}"

    @staticmethod
    def _default_system_hint(user_id: str) -> str:
        """Fallback system hint when .system-prompt.md doesn't exist."""
        return (
            f'The WeCom user_id for the current user is "{user_id}". '
            f"You have send_wecom_message, send_wecom_image, and send_wecom_file MCP tools available. "
            f"When setting up scheduled/recurring tasks, always include instructions in the "
            f'scheduled prompt to send results back to user_id="{user_id}" using send_wecom_message '
            f"after the task completes. When the user asks you to send a file, use send_wecom_file "
            f"or send_wecom_image. "
            f"Output format: Your response will be delivered via WeCom mobile app. "
            f"Keep responses concise (under 1500 chars preferred). Use short paragraphs, bullet points, "
            f"and avoid long code blocks. For detailed content, save to a file and send via send_wecom_file. "
            f"Memory: If you learn something of lasting value during this task, update the project MEMORY.md file. "
            f"Dashboard: If this task represents a new category of work, edit .dashboard-workstations.json to add a new workstation."
        )

    async def _execute_task(self, task) -> None:
        """Execute a single task through the agent runner."""
        task_id = task.id
        user_id = task.user_id
        session_id = task.session_id

        session = self.store.get_or_create_session(
            user_id, self.config.agent.default_working_dir
        )
        is_resume = session.initialized

        logger.info("Executing task %s for %s (session=%s, resume=%s, wd=%s)",
                    task_id[:12], user_id, session_id[:8], is_resume, session.working_dir)
        self.store.update_task_status(task_id, TaskStatus.RUNNING)
        self.store.update_session_used(user_id)

        # Create streaming handler for real-time output
        self.dashboard_streaming["buffer"] = ""
        self.dashboard_streaming["thinking"] = ""
        stream = self.notifier.create_stream_handler(
            user_id, task_id, task.message, dashboard_ref=self.dashboard_streaming,
        )

        # Thinking callback for dashboard
        async def _on_thinking(text: str) -> None:
            current = self.dashboard_streaming.get("thinking", "")
            updated = current + text
            self.dashboard_streaming["thinking"] = updated[-5000:]  # keep last 5k chars

        try:
            augmented_message = self._inject_memory(user_id, task.message)
            augmented_message = self._inject_wecom_hint(user_id, augmented_message)
            result: RunResult = await asyncio.wait_for(
                self.runner.run(
                    augmented_message, session_id, is_resume, session.working_dir,
                    on_output=stream.on_output, on_thinking=_on_thinking,
                    task_id=task_id,
                ),
                timeout=self.config.agent.task_timeout_seconds,
            )

            # Flush any remaining buffered output
            await stream.flush()

            output = result.output
            summary = output[-self.config.agent.max_output_length:]
            self.store.update_task_status(
                task_id, TaskStatus.COMPLETED, output=output, summary=summary
            )
            task.summary = summary
            task.output = output

            if not session.initialized:
                self.store.mark_session_initialized(user_id)

            mi = self.runner.model_info
            logger.info(
                "Task %s completed (exit=%d, output=%d chars, tokens=%d/%d, cost=$%.4f)",
                task_id[:12], result.exit_code, len(output),
                mi.get("input_tokens", 0) + mi.get("output_tokens", 0),
                mi.get("context_window", 0),
                mi.get("total_cost_usd", 0),
            )

            if result.exit_code != 0 and result.error:
                logger.warning("Task %s non-zero exit: %s", task_id[:12], result.error[:200])
                task.error = result.error
                await self.notifier.task_failed(task)
            else:
                self._save_raw_memory(user_id, task_id, task.message, output)

        except asyncio.TimeoutError:
            logger.warning("Task %s timed out after %ds", task_id[:12],
                          self.config.agent.task_timeout_seconds)
            await self.runner.cancel()
            self.store.update_task_status(
                task_id, TaskStatus.FAILED, error="Task timed out"
            )
            task.error = "Task timed out"
            await self.notifier.task_failed(task)

        except asyncio.CancelledError:
            self.store.update_task_status(task_id, TaskStatus.CANCELLED)
            task.status = TaskStatus.CANCELLED
            await self.notifier.task_cancelled(task)

        except Exception as e:
            logger.exception("Task %s failed with error", task_id)
            error_msg = str(e)
            self.store.update_task_status(task_id, TaskStatus.FAILED, error=error_msg)
            task.error = error_msg
            await self.notifier.task_failed(task)

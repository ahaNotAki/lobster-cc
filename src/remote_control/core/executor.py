"""Executor — orchestrates task queue, agent runner, and notifications."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from remote_control.config import AppConfig
from remote_control.core.models import TaskStatus
from remote_control.core.notifier import Notifier
from remote_control.core.runner import AgentRunner, RunResult
from remote_control.core.store import Store

logger = logging.getLogger(__name__)


def _extract_summary(output: str) -> str:
    """Extract 📋 summary line from Claude's output. Returns empty string if not found."""
    for line in reversed(output.strip().split("\n")):
        stripped = line.strip()
        if stripped.startswith("📋"):
            return stripped[1:].strip()
    return ""


class Executor:
    def __init__(
        self, config: AppConfig, store: Store, notifier: Notifier, runner: AgentRunner,
        profile_manager=None,
    ):
        self.config = config
        self.store = store
        self.notifier = notifier
        self.runner = runner
        self.profile_manager = profile_manager
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


    # Task archive removed — full output stored in DB (tasks.output field).
    # Recall MCP reads directly from DB via get_task_detail fallback.

    def _inject_wecom_hint(self, user_id: str, message: str) -> str:
        """Load per-agent system prompt from .system-prompt.md, fallback to default.

        Additionally injects profile-aware hints if profile_manager is available.
        """
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

        # Append profile-aware hints if available
        if self.profile_manager is not None:
            try:
                profile = self.profile_manager.get_profile()
                style = profile.output_style
                hint += (
                    f"\nYour preferred output style: {style.format}. "
                    f"Max length: {style.max_message_length} chars. "
                    f"Language: {style.language}."
                )
                hint += (
                    "\nYou have agent self-configuration tools: "
                    "get_agent_config, set_agent_config, list_agent_config, reset_agent_config. "
                    "Use set_agent_config to persist user preferences "
                    "(output style, model, notification frequency)."
                )
            except Exception:
                logger.debug("Failed to load profile for hint injection, skipping")

        hint += (
            "\nTask summary: End your response with a line starting with 📋 that summarizes "
            "what was done and the key result in one sentence (under 80 chars, same language as the user)."
        )

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
            f"Task summary: End your response with a line starting with 📋 that summarizes "
            f"what was done and the key result in one sentence (under 80 chars, same language as the user). "
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
            augmented_message = self._inject_wecom_hint(user_id, task.message)

            # Check profile for task-type model override
            model_override = None
            if self.profile_manager is not None:
                try:
                    profile = self.profile_manager.get_profile()
                    for override in profile.model_selection.task_type_overrides:
                        if len(override.pattern) > 200:
                            continue  # skip overly long patterns
                        if re.search(override.pattern, task.message[:500], re.IGNORECASE):
                            model_override = override.model
                            logger.info(
                                "Profile model override: pattern=%r matched, using model=%s",
                                override.pattern, override.model,
                            )
                            break
                except Exception:
                    logger.debug("Failed to check profile model overrides, using default")

            result: RunResult = await asyncio.wait_for(
                self.runner.run(
                    augmented_message, session_id, is_resume, session.working_dir,
                    on_output=stream.on_output, on_thinking=_on_thinking,
                    task_id=task_id, model_override=model_override,
                ),
                timeout=self.config.agent.task_timeout_seconds,
            )

            # Flush any remaining buffered output
            await stream.flush()

            output = result.output

            # Extract Claude-generated summary, fallback to first line
            task_summary = _extract_summary(output)
            if not task_summary:
                first_line = output.strip().split("\n")[0] if output.strip() else ""
                task_summary = first_line[:150]

            self.store.update_task_status(
                task_id, TaskStatus.COMPLETED, output=output, summary=task_summary
            )
            task.summary = task_summary
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

            # Persist model_info to kv so dashboard survives restarts
            if mi:
                import json
                agent_id = getattr(self.store, '_agent_id', '')
                self.store.set_kv(f"model_info:{agent_id}", json.dumps(mi))

            if result.exit_code != 0 and result.error:
                logger.warning("Task %s non-zero exit: %s", task_id[:12], result.error[:200])
                task.error = result.error
                await self.notifier.task_failed(task)
            else:
                pass  # output already saved to DB by update_task_status above

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

"""Command router — dispatches slash commands and creates tasks."""

import logging
from typing import TYPE_CHECKING

from remote_control.core.models import TaskStatus

if TYPE_CHECKING:
    from remote_control.core.executor import Executor

logger = logging.getLogger(__name__)

HELP_TEXT = """Available commands:
/status [id] - Show task status (latest or by ID)
/cancel [id] - Cancel running task (or by ID)
/list - List recent tasks
/new - Start a new session (reset context)
/cd <path> - Change working directory
/output <id> - Get full output of a task
/clear - Clear all task history
/memory - Show memory stats
/memory show - Show consolidated knowledge
/memory clear - Clear all memory
/restart - Restart Claude (reload MCP servers & plugins)
/help - Show this help

Scheduling: Just describe it naturally, e.g. "every day at 9am run the tests"
(Powered by Claude Code scheduler plugin)"""


class CommandRouter:
    def __init__(self, executor: "Executor"):
        self._executor = executor

    async def route(self, user_id: str, message: str) -> None:
        """Route a message to the appropriate handler."""
        stripped = message.strip()
        if not stripped:
            return

        parts = stripped.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else None

        handlers = {
            "/status": self._handle_status,
            "/cancel": self._handle_cancel,
            "/list": self._handle_list,
            "/new": self._handle_new,
            "/cd": self._handle_cd,
            "/output": self._handle_output,
            "/clear": self._handle_clear,
            "/memory": self._handle_memory,
            "/restart": self._handle_restart,
            "/help": self._handle_help,
        }

        handler = handlers.get(cmd)
        if handler:
            await handler(user_id, arg)
        else:
            await self._executor.enqueue_task(user_id, stripped)

    async def _handle_status(self, user_id: str, arg: str | None) -> None:
        store = self._executor.store
        notifier = self._executor.notifier

        if arg:
            task = store.get_task(arg)
        else:
            task = store.get_latest_task(user_id)

        if not task:
            await notifier.send_reply(user_id, "No tasks found.")
            return

        lines = [
            f"Task #{task.id}",
            f"Status: {task.status.value}",
            f"Message: {task.message[:100]}",
            f"Created: {task.created_at}",
        ]
        if task.started_at:
            lines.append(f"Started: {task.started_at}")
        if task.finished_at:
            lines.append(f"Finished: {task.finished_at}")
        await notifier.send_reply(user_id, "\n".join(lines))

    async def _handle_cancel(self, user_id: str, arg: str | None) -> None:
        notifier = self._executor.notifier

        if arg:
            task = self._executor.store.get_task(arg)
            if not task:
                await notifier.send_reply(user_id, f"Task {arg} not found.")
                return
            if task.status == TaskStatus.RUNNING:
                await self._executor.cancel_running_task()
                await notifier.send_reply(user_id, f"Task #{arg} cancellation requested.")
            elif task.status == TaskStatus.QUEUED:
                self._executor.store.update_task_status(arg, TaskStatus.CANCELLED)
                await notifier.send_reply(user_id, f"Task #{arg} cancelled.")
            else:
                await notifier.send_reply(user_id, f"Task #{arg} is already {task.status.value}.")
        else:
            running = self._executor.store.get_running_task()
            if running:
                await self._executor.cancel_running_task()
                await notifier.send_reply(user_id, f"Task #{running.id} cancellation requested.")
            else:
                await notifier.send_reply(user_id, "No running task to cancel.")

    async def _handle_list(self, user_id: str, arg: str | None) -> None:
        tasks = self._executor.store.list_tasks(user_id)
        if not tasks:
            await self._executor.notifier.send_reply(user_id, "No tasks found.")
            return
        lines = []
        for t in tasks:
            lines.append(f"#{t.id} [{t.status.value}] {t.message[:60]}")
        await self._executor.notifier.send_reply(user_id, "\n".join(lines))

    async def _handle_new(self, user_id: str, arg: str | None) -> None:
        session = self._executor.store.reset_session(
            user_id, self._executor.config.agent.default_working_dir
        )
        await self._executor.notifier.send_reply(
            user_id, f"New session started: {session.session_id[:8]}..."
        )

    async def _handle_cd(self, user_id: str, arg: str | None) -> None:
        if not arg:
            session = self._executor.store.get_or_create_session(
                user_id, self._executor.config.agent.default_working_dir
            )
            await self._executor.notifier.send_reply(
                user_id, f"Current directory: {session.working_dir}"
            )
            return
        import os
        if not os.path.isdir(arg):
            await self._executor.notifier.send_reply(user_id, f"Directory not found: {arg}")
            return
        self._executor.store.update_session_working_dir(user_id, arg)
        await self._executor.notifier.send_reply(user_id, f"Working directory set to: {arg}")

    async def _handle_output(self, user_id: str, arg: str | None) -> None:
        if not arg:
            await self._executor.notifier.send_reply(user_id, "Usage: /output <task_id>")
            return
        task = self._executor.store.get_task(arg)
        if not task:
            await self._executor.notifier.send_reply(user_id, f"Task {arg} not found.")
            return
        output = task.output or "(no output)"
        await self._executor.notifier.send_reply(user_id, output)

    async def _handle_clear(self, user_id: str, arg: str | None) -> None:
        count = self._executor.store.clear_tasks(user_id)
        await self._executor.notifier.send_reply(user_id, f"Cleared {count} tasks.")

    async def _handle_memory(self, user_id: str, arg: str | None) -> None:
        notifier = self._executor.notifier
        store = self._executor.store

        if not arg:
            stats = store.get_memory_stats(user_id)
            await notifier.send_reply(
                user_id,
                f"Memory stats:\n"
                f"Raw entries: {stats['raw_count']}\n"
                f"Consolidated entries: {stats['consolidated_count']}",
            )
        elif arg.strip().lower() == "show":
            memories = store.get_consolidated_memories(user_id)
            if not memories:
                await notifier.send_reply(user_id, "No consolidated memories yet.")
                return
            lines = []
            for mem in memories:
                lines.append(f"[{mem.category}] {mem.content}")
            await notifier.send_reply(user_id, "\n".join(lines))
        elif arg.strip().lower() == "clear":
            count = store.clear_memories(user_id)
            await notifier.send_reply(user_id, f"Cleared {count} memory entries.")
        else:
            await notifier.send_reply(user_id, "Usage: /memory [show|clear]")

    async def _handle_restart(self, user_id: str, arg: str | None) -> None:
        """Kill running claude process and reset session to force a fresh start."""
        notifier = self._executor.notifier
        # Cancel running task if any (kills the claude process)
        running = self._executor.store.get_running_task()
        if running:
            await self._executor.cancel_running_task()
        # Reset session so next task spawns a new claude process
        session = self._executor.store.reset_session(
            user_id, self._executor.config.agent.default_working_dir
        )
        await notifier.send_reply(
            user_id,
            f"Claude restarted. New session: {session.session_id[:8]}...\n"
            f"MCP servers and plugins will reload on next task.",
        )

    async def _handle_help(self, user_id: str, arg: str | None) -> None:
        await self._executor.notifier.send_reply(user_id, HELP_TEXT)

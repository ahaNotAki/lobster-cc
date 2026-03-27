"""Process Watchdog — safety net that kills claude processes exceeding timeout."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass, field

from remote_control.core.models import TaskStatus

logger = logging.getLogger(__name__)


@dataclass
class TrackedProcess:
    pid: int
    task_id: str
    start_time: float = field(default_factory=time.monotonic)


class ProcessWatchdog:
    """Periodically checks tracked processes and kills those exceeding timeout.

    Acts as a safety net alongside asyncio.wait_for in the executor.
    Only intervenes when the primary timeout mechanism fails.
    """

    def __init__(self, store, notifier, timeout_seconds: int = 1200, interval_seconds: int = 60):
        self._store = store
        self._notifier = notifier
        self._timeout = timeout_seconds
        self._interval = interval_seconds
        self._tracked: dict[int, TrackedProcess] = {}
        self._task: asyncio.Task | None = None

    def register(self, pid: int, task_id: str) -> None:
        """Register a process for watchdog tracking."""
        self._tracked[pid] = TrackedProcess(pid=pid, task_id=task_id)
        logger.info("Watchdog tracking pid=%d task=%s", pid, task_id)

    def unregister(self, pid: int) -> None:
        """Remove a process from watchdog tracking (normal completion)."""
        removed = self._tracked.pop(pid, None)
        if removed:
            logger.debug("Watchdog untracked pid=%d task=%s", pid, removed.task_id)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "ProcessWatchdog started (interval=%ds, timeout=%ds)",
            self._interval, self._timeout,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ProcessWatchdog stopped")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in watchdog check")

    async def _check(self) -> None:
        now = time.monotonic()
        to_kill: list[TrackedProcess] = []

        for proc in list(self._tracked.values()):
            elapsed = now - proc.start_time
            if elapsed > self._timeout:
                to_kill.append(proc)
            elif not self._is_alive(proc.pid):
                # Process already dead, clean up tracking
                self._tracked.pop(proc.pid, None)
                logger.info("Watchdog: pid=%d already dead, removed from tracking", proc.pid)

        for proc in to_kill:
            await self._kill_process(proc)

    async def _kill_process(self, proc: TrackedProcess) -> None:
        elapsed_min = (time.monotonic() - proc.start_time) / 60
        logger.warning(
            "Watchdog killing pid=%d task=%s (running %.1f min, limit %d s)",
            proc.pid, proc.task_id, elapsed_min, self._timeout,
        )

        # SIGTERM first, then SIGKILL
        try:
            if self._is_alive(proc.pid):
                os.kill(proc.pid, signal.SIGTERM)
                await asyncio.sleep(10)
            if self._is_alive(proc.pid):
                logger.warning("Watchdog: pid=%d did not exit after SIGTERM, sending SIGKILL", proc.pid)
                os.kill(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # Already dead

        self._tracked.pop(proc.pid, None)

        # Update task status and notify
        error_msg = f"Process watchdog: killed after running {elapsed_min:.0f} min (limit: {self._timeout}s)"
        try:
            task = self._store.get_task(proc.task_id)
            if task and task.status == TaskStatus.RUNNING:
                self._store.update_task_status(proc.task_id, TaskStatus.FAILED, error=error_msg)
                task.error = error_msg
                await self._notifier.task_failed(task)
        except Exception:
            logger.exception("Watchdog: failed to update task %s after kill", proc.task_id)

    @staticmethod
    def _is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

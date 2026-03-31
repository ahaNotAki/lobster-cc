"""Agent Runner — spawns and manages Claude Code CLI processes."""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Awaitable

from dataclasses import dataclass

from remote_control.config import AgentConfig

logger = logging.getLogger(__name__)

# Error substrings that indicate a session ID mismatch
_SESSION_NOT_FOUND = "no conversation found"
_SESSION_IN_USE = "already in use"

# Error substring for first response timeout
_FIRST_RESPONSE_TIMEOUT = "First response timeout"

# Type for the streaming output callback
OutputCallback = Callable[[str], Awaitable[None]]


@dataclass
class RunResult:
    exit_code: int
    output: str
    error: str = ""


class AgentRunner:
    def __init__(self, config: AgentConfig, watchdog=None):
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._running = False
        self._watchdog = watchdog
        self._current_task_id: str = ""
        # Model info populated from stream-json events (shared with dashboard)
        self.model_info: dict = {}

    @property
    def is_running(self) -> bool:
        return self._running

    def build_command(
        self, message: str, session_id: str, is_resume: bool, working_dir: str,
        model_override: str | None = None,
    ) -> list[str]:
        cmd = [
            self._config.claude_command,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--dangerously-skip-permissions",
        ]
        if is_resume:
            cmd += ["--resume", session_id]
        else:
            cmd += ["--session-id", session_id]

        model = model_override or self._config.model
        if model:
            cmd += ["--model", model]
        if self._config.allowed_tools:
            cmd += ["--allowedTools"] + self._config.allowed_tools

        # "--" separates flags from the positional prompt argument.
        # Without it, variadic flags like --allowedTools swallow the prompt.
        cmd += ["--", message]
        return cmd

    async def run(
        self,
        message: str,
        session_id: str,
        is_resume: bool,
        working_dir: str,
        on_output: OutputCallback | None = None,
        on_thinking: OutputCallback | None = None,
        task_id: str = "",
        model_override: str | None = None,
    ) -> RunResult:
        """Run Claude Code CLI and return the result.

        Uses automatic retry on session ID mismatch: if --resume fails because
        the session doesn't exist, retries with --session-id (and vice versa).

        Also retries once on first response timeout.

        Args:
            on_output: Optional callback invoked with each text output chunk.
            on_thinking: Optional callback invoked with each thinking chunk.
            model_override: Optional model to use instead of config default.
        """
        self._current_task_id = task_id
        result = await self._run_once(message, session_id, is_resume, working_dir, on_output, on_thinking, model_override=model_override)

        # Check if we hit a session mismatch error and should retry with the opposite mode
        if result.exit_code != 0 and self._is_session_error(result.error):
            flipped = not is_resume
            mode_label = "--resume" if flipped else "--session-id"
            logger.info(
                "Session mismatch for %s, retrying with %s", session_id[:8], mode_label,
            )
            result = await self._run_once(message, session_id, flipped, working_dir, on_output, on_thinking, model_override=model_override)

        # Check if we hit a first response timeout and should retry
        elif result.exit_code != 0 and _FIRST_RESPONSE_TIMEOUT in result.error:
            logger.warning("First response timeout, retrying...")
            result = await self._run_once(message, session_id, is_resume, working_dir, on_output, on_thinking, model_override=model_override)

        self._current_task_id = ""
        return result

    async def _run_once(
        self,
        message: str,
        session_id: str,
        is_resume: bool,
        working_dir: str,
        on_output: OutputCallback | None = None,
        on_thinking: OutputCallback | None = None,
        model_override: str | None = None,
    ) -> RunResult:
        """Execute a single CLI invocation, streaming stdout via callback.

        Parses stream-json output to separate thinking blocks from text output,
        and captures model info from init/result events.
        """
        cmd = self.build_command(message, session_id, is_resume, working_dir, model_override=model_override)
        mode = "--resume" if is_resume else "--session-id"
        msg_preview = message[:80].replace("\n", " ")
        logger.info("Running: %s %s %s... msg=%s (cwd=%s)",
                     cmd[0], mode, session_id[:8], msg_preview, working_dir)

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            limit=10 * 1024 * 1024,  # 10MB — stream-json can emit large single-line events
        )
        self._running = True
        spawn_time = time.monotonic()
        logger.info("Claude CLI spawned (pid=%d, wd=%s)", self._process.pid, working_dir)

        if self._watchdog and self._process.pid and self._current_task_id:
            self._watchdog.register(self._process.pid, self._current_task_id)

        try:
            raw_lines: list[str] = []  # fallback for non-JSON output
            stderr_parts: list[str] = []
            final_result_text: str = ""
            # Track cumulative lengths to extract only new deltas
            seen_text_len: int = 0
            seen_thinking_len: int = 0
            first_output_received = False
            init_received_time: float | None = None
            first_assistant_time: float | None = None

            async def _read_stdout():
                nonlocal final_result_text, seen_text_len, seen_thinking_len, first_output_received
                nonlocal init_received_time, first_assistant_time
                assert self._process and self._process.stdout

                # Wait for first line with timeout
                try:
                    line = await asyncio.wait_for(
                        self._process.stdout.readline(),
                        timeout=self._config.first_response_timeout_seconds
                    )
                except asyncio.TimeoutError:
                    timeout = self._config.first_response_timeout_seconds
                    logger.warning("First response timeout after %ds (pid=%d), killing process", timeout, self._process.pid)
                    # Kill the process
                    try:
                        self._process.kill()
                        await self._process.wait()
                    except Exception as e:
                        logger.error("Failed to kill process: %s", e)
                    return  # Exit the read loop

                first_output_received = True

                # Process the first line
                if line:
                    raw = line.decode("utf-8", errors="replace").strip()
                    if raw:
                        try:
                            event = json.loads(raw)
                            event_type = event.get("type", "")

                            if event_type == "system" and event.get("subtype") == "init":
                                init_received_time = time.monotonic()
                                init_delay = init_received_time - spawn_time
                                self.model_info["model"] = event.get("model", "")
                                self.model_info["session_id"] = event.get("session_id", "")
                                self.model_info["claude_code_version"] = event.get("claude_code_version", "")
                                self.model_info["mcp_servers"] = event.get("mcp_servers", [])
                                mcp_status = [s.get("name","?") for s in self.model_info["mcp_servers"]]
                                logger.info("Claude CLI ready (pid=%d, init_delay=%.1fs, mcp=%s)",
                                            self._process.pid, init_delay, mcp_status)
                        except json.JSONDecodeError:
                            # Non-JSON output
                            raw_lines.append(raw + "\n")
                            if on_output:
                                await on_output(raw + "\n")

                # Continue reading remaining lines (no timeout)
                while True:
                    line = await self._process.stdout.readline()
                    if not line:
                        break
                    raw = line.decode("utf-8", errors="replace").strip()
                    if not raw:
                        continue

                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        # Non-JSON output (e.g., plain text from non-claude commands)
                        raw_lines.append(raw + "\n")
                        if on_output:
                            await on_output(raw + "\n")
                        continue

                    event_type = event.get("type", "")

                    if event_type == "system" and event.get("subtype") == "init":
                        # Already handled above if first line, but handle here for safety
                        if init_received_time is None:
                            init_received_time = time.monotonic()
                            init_delay = init_received_time - spawn_time
                            self.model_info["model"] = event.get("model", "")
                            self.model_info["session_id"] = event.get("session_id", "")
                            self.model_info["claude_code_version"] = event.get("claude_code_version", "")
                            self.model_info["mcp_servers"] = event.get("mcp_servers", [])
                            mcp_status = [s.get("name","?") for s in self.model_info["mcp_servers"]]
                            logger.info("Claude CLI ready (pid=%d, init_delay=%.1fs, mcp=%s)",
                                        self._process.pid, init_delay, mcp_status)

                    elif event_type == "assistant":
                        # Log first assistant output
                        if first_assistant_time is None:
                            first_assistant_time = time.monotonic()
                            delay = first_assistant_time - spawn_time
                            logger.info("First output received (pid=%d, delay=%.1fs)", self._process.pid, delay)
                        # Track token usage from each assistant message
                        message_obj = event.get("message", {})
                        usage = message_obj.get("usage", {})
                        if usage:
                            inp = usage.get("input_tokens", 0)
                            cache_read = usage.get("cache_read_input_tokens", 0)
                            cache_create = usage.get("cache_creation_input_tokens", 0)
                            out = usage.get("output_tokens", 0)
                            self.model_info["current_input_tokens"] = inp
                            self.model_info["current_output_tokens"] = out
                            self.model_info["current_context_tokens"] = inp + cache_read + cache_create + out

                        # Each assistant event has CUMULATIVE content.
                        # We diff against what we've already forwarded.
                        # Only track the LAST text/thinking block — earlier blocks
                        # are frozen after tool use and were already fully output.
                        blocks = message_obj.get("content", [])
                        last_text = ""
                        last_thinking = ""
                        for block in blocks:
                            bt = block.get("type", "")
                            if bt == "text":
                                last_text = block.get("text", "")
                            elif bt == "thinking":
                                last_thinking = block.get("thinking", "")

                        if last_thinking:
                            if len(last_thinking) < seen_thinking_len:
                                seen_thinking_len = 0
                            delta = last_thinking[seen_thinking_len:]
                            seen_thinking_len = len(last_thinking)
                            if delta and on_thinking:
                                await on_thinking(delta)

                        if last_text:
                            if len(last_text) < seen_text_len:
                                seen_text_len = 0
                            delta = last_text[seen_text_len:]
                            seen_text_len = len(last_text)
                            if delta and on_output:
                                await on_output(delta)

                    elif event_type == "result":
                        final_result_text = event.get("result", "")
                        # Capture usage/model info
                        usage = event.get("usage", {})
                        model_usage = event.get("modelUsage", {})
                        self.model_info["total_cost_usd"] = event.get("total_cost_usd", 0)
                        self.model_info["num_turns"] = event.get("num_turns", 0)
                        self.model_info["duration_ms"] = event.get("duration_ms", 0)
                        self.model_info["input_tokens"] = usage.get("input_tokens", 0)
                        self.model_info["output_tokens"] = usage.get("output_tokens", 0)
                        self.model_info["cache_read_tokens"] = usage.get("cache_read_input_tokens", 0)
                        self.model_info["cache_creation_tokens"] = usage.get("cache_creation_input_tokens", 0)
                        for model_id, mu in model_usage.items():
                            self.model_info["context_window"] = mu.get("contextWindow", 0)
                            self.model_info["max_output_tokens"] = mu.get("maxOutputTokens", 0)
                        logger.info(
                            "CLI result: turns=%d, duration=%dms, in=%d, out=%d, cache_r=%d, cache_w=%d, cost=$%.4f",
                            self.model_info.get("num_turns", 0),
                            self.model_info.get("duration_ms", 0),
                            self.model_info.get("input_tokens", 0),
                            self.model_info.get("output_tokens", 0),
                            self.model_info.get("cache_read_tokens", 0),
                            self.model_info.get("cache_creation_tokens", 0),
                            self.model_info.get("total_cost_usd", 0),
                        )

            async def _read_stderr():
                assert self._process and self._process.stderr
                data = await self._process.stderr.read()
                if data:
                    stderr_parts.append(data.decode("utf-8", errors="replace"))

            await asyncio.gather(_read_stdout(), _read_stderr())
            await self._process.wait()

            # Check if we hit first response timeout
            if not first_output_received:
                timeout_msg = (
                    f"First response timeout ({self._config.first_response_timeout_seconds}s) — "
                    "Claude CLI may be stuck on MCP initialization"
                )
                return RunResult(exit_code=1, output="", error=timeout_msg)

            output = final_result_text.strip() or "".join(raw_lines).strip()
            error = "".join(stderr_parts).strip()
            exit_code = self._process.returncode or 0

            if output:
                logger.info("Claude output (%d chars): %s...", len(output), output[:200])
            if error:
                logger.warning("Claude stderr: %s", error[:500])

            return RunResult(exit_code=exit_code, output=output, error=error)
        finally:
            if self._watchdog and self._process and self._process.pid:
                self._watchdog.unregister(self._process.pid)
            self._running = False
            self._process = None

    @staticmethod
    def _is_session_error(error: str) -> bool:
        """Check if an error is a session ID mismatch that can be retried."""
        lower = error.lower()
        return _SESSION_NOT_FOUND in lower or _SESSION_IN_USE in lower

    async def cancel(self) -> None:
        """Cancel the running process gracefully."""
        if not self._process:
            return
        logger.info("Cancelling running process (pid=%s)", self._process.pid)
        try:
            self._process.terminate()
            await asyncio.wait_for(self._process.wait(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("Process did not exit after SIGTERM, sending SIGKILL")
            self._process.kill()
            await self._process.wait()
        finally:
            self._running = False
            self._process = None

    def get_exit_code(self) -> int | None:
        if self._process:
            return self._process.returncode
        return None

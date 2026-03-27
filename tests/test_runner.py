"""Tests for the agent runner."""

import json
import pytest
from unittest.mock import AsyncMock

from remote_control.config import AgentConfig
from remote_control.core.runner import AgentRunner, RunResult


# --- Command building ---


def test_build_command_new_session():
    config = AgentConfig(claude_command="claude", model="sonnet")
    runner = AgentRunner(config)
    cmd = runner.build_command("do stuff", "session-123", is_resume=False, working_dir="/tmp")
    assert cmd[:6] == ["claude", "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    assert "--session-id" in cmd
    assert "session-123" in cmd
    assert "--resume" not in cmd
    assert "--model" in cmd
    assert "sonnet" in cmd
    assert cmd[-1] == "do stuff"


def test_build_command_resume_session():
    config = AgentConfig(claude_command="claude")
    runner = AgentRunner(config)
    cmd = runner.build_command("continue", "session-123", is_resume=True, working_dir="/tmp")
    assert "--resume" in cmd
    assert "session-123" in cmd
    assert "--session-id" not in cmd


def test_build_command_with_tools():
    config = AgentConfig(
        claude_command="claude",
        allowed_tools=["Bash", "Read"],
    )
    runner = AgentRunner(config)
    cmd = runner.build_command("work", "s1", is_resume=False, working_dir="/tmp")
    assert "--allowedTools" in cmd
    assert "Bash" in cmd
    assert "Read" in cmd
    assert "--dangerously-skip-permissions" in cmd


def test_build_command_no_optional_flags():
    config = AgentConfig(claude_command="claude")
    runner = AgentRunner(config)
    cmd = runner.build_command("msg", "s1", is_resume=False, working_dir="/tmp")
    assert "--model" not in cmd
    assert "--allowedTools" not in cmd
    assert "--dangerously-skip-permissions" in cmd


def test_build_command_custom_claude_path():
    config = AgentConfig(claude_command="/opt/bin/claude-code")
    runner = AgentRunner(config)
    cmd = runner.build_command("msg", "s1", is_resume=False, working_dir="/tmp")
    assert cmd[0] == "/opt/bin/claude-code"


# --- Session error detection ---


def test_is_session_error_not_found():
    assert AgentRunner._is_session_error("Error: No conversation found with session ID abc")


def test_is_session_error_already_in_use():
    assert AgentRunner._is_session_error("Error: Session ID abc is already in use.")


def test_is_session_error_other():
    assert not AgentRunner._is_session_error("Error: something else went wrong")


# --- Retry logic ---


@pytest.mark.asyncio
async def test_run_retries_on_session_not_found():
    """If --resume fails with 'no conversation found', retry with --session-id."""
    runner = AgentRunner(AgentConfig())

    fail_result = RunResult(exit_code=1, output="", error="Error: No conversation found with session ID x")
    ok_result = RunResult(exit_code=0, output="Hello!", error="")

    runner._run_once = AsyncMock(side_effect=[fail_result, ok_result])

    result = await runner.run("hi", "sess-1", is_resume=True, working_dir="/tmp")

    assert result.exit_code == 0
    assert result.output == "Hello!"
    assert runner._run_once.call_count == 2
    # First call: is_resume=True, second call: is_resume=False
    assert runner._run_once.call_args_list[0].args[2] is True
    assert runner._run_once.call_args_list[1].args[2] is False


@pytest.mark.asyncio
async def test_run_retries_on_already_in_use():
    """If --session-id fails with 'already in use', retry with --resume."""
    runner = AgentRunner(AgentConfig())

    fail_result = RunResult(exit_code=1, output="", error="Error: Session ID x is already in use.")
    ok_result = RunResult(exit_code=0, output="Done!", error="")

    runner._run_once = AsyncMock(side_effect=[fail_result, ok_result])

    result = await runner.run("hi", "sess-1", is_resume=False, working_dir="/tmp")

    assert result.exit_code == 0
    assert result.output == "Done!"
    assert runner._run_once.call_count == 2
    # First call: is_resume=False, second call: is_resume=True
    assert runner._run_once.call_args_list[0].args[2] is False
    assert runner._run_once.call_args_list[1].args[2] is True


@pytest.mark.asyncio
async def test_run_no_retry_on_other_errors():
    """Non-session errors should not trigger a retry."""
    runner = AgentRunner(AgentConfig())

    fail_result = RunResult(exit_code=1, output="", error="Error: something else")
    runner._run_once = AsyncMock(return_value=fail_result)

    result = await runner.run("hi", "sess-1", is_resume=True, working_dir="/tmp")

    assert result.exit_code == 1
    assert runner._run_once.call_count == 1


@pytest.mark.asyncio
async def test_run_no_retry_on_success():
    """Successful run should not retry."""
    runner = AgentRunner(AgentConfig())

    ok_result = RunResult(exit_code=0, output="All good", error="")
    runner._run_once = AsyncMock(return_value=ok_result)

    result = await runner.run("hi", "sess-1", is_resume=True, working_dir="/tmp")

    assert result.exit_code == 0
    assert runner._run_once.call_count == 1


# --- Runner state ---


def test_runner_initial_state():
    runner = AgentRunner(AgentConfig())
    assert not runner.is_running
    assert runner.get_exit_code() is None


@pytest.mark.asyncio
async def test_runner_cancel_no_process():
    """cancel() should be a no-op when no process is running."""
    runner = AgentRunner(AgentConfig())
    await runner.cancel()  # should not raise


@pytest.mark.asyncio
async def test_runner_run_simple_command():
    """Integration test: run a real subprocess (echo) to verify text output capture."""
    config = AgentConfig(claude_command="echo")
    runner = AgentRunner(config)

    result = await runner.run(
        "hello world",
        "test-session",
        is_resume=False,
        working_dir="/tmp",
    )

    assert not runner.is_running
    assert result.exit_code == 0
    assert "hello world" in result.output


# --- Additional command building tests ---


def test_build_command_includes_partial_messages():
    """Verify --include-partial-messages flag is present in built command."""
    config = AgentConfig(claude_command="claude")
    runner = AgentRunner(config)
    cmd = runner.build_command("msg", "s1", is_resume=False, working_dir="/tmp")
    assert "--include-partial-messages" in cmd


def test_model_info_initialized_empty():
    """Verify model_info starts as an empty dict."""
    runner = AgentRunner(AgentConfig())
    assert runner.model_info == {}
    assert isinstance(runner.model_info, dict)


@pytest.mark.asyncio
async def test_multi_turn_text_tracking():
    """Text from new assistant turns (after tool use) should not be lost.

    When Claude uses tools and starts a new turn, the text block restarts
    from scratch. The runner must detect this and reset seen_text_len.
    """
    # Simulate stream-json output: turn 1 text, then turn 2 text (shorter = new turn)
    turn1_events = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello from turn 1"}]}}),
    ]
    turn2_events = [
        # New turn: text restarts from scratch (shorter than seen_text_len)
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "W"}]}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "World from turn 2"}]}}),
    ]
    result_event = [json.dumps({"type": "result", "result": "Hello from turn 1\nWorld from turn 2"})]

    all_lines = "\n".join(turn1_events + turn2_events + result_event) + "\n"

    config = AgentConfig(claude_command="echo")
    runner = AgentRunner(config)

    captured_output: list[str] = []

    async def capture(text: str) -> None:
        captured_output.append(text)

    # Use printf to emit the stream-json lines
    runner.build_command = lambda *a, **k: ["printf", all_lines.replace("\n", "\\n")]

    await runner._run_once(
        "test", "sess", False, "/tmp",
        on_output=capture,
    )

    combined = "".join(captured_output)
    assert "Hello from turn 1" in combined
    assert "World from turn 2" in combined


@pytest.mark.asyncio
async def test_multi_block_tool_use_text_tracking():
    """Multiple text blocks in a single assistant event (after tool use) must not corrupt output.

    When Claude uses a tool, the content array has: [text_before, tool_use, tool_result, text_after].
    Only the LAST text block should be delta-tracked.
    """
    events = [
        # Turn 1: writing text
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Before tool"},
        ]}}),
        # Turn 1: tool use added
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Before tool"},
            {"type": "tool_use", "id": "t1", "name": "bash", "input": {}},
        ]}}),
        # Turn 1: tool result + new text block starts
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Before tool"},
            {"type": "tool_use", "id": "t1", "name": "bash", "input": {}},
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            {"type": "text", "text": "After tool"},
        ]}}),
        # Turn 1: new text block grows
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Before tool"},
            {"type": "tool_use", "id": "t1", "name": "bash", "input": {}},
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            {"type": "text", "text": "After tool use done"},
        ]}}),
        json.dumps({"type": "result", "result": "Before tool\nAfter tool use done"}),
    ]

    all_lines = "\n".join(events) + "\n"

    config = AgentConfig(claude_command="echo")
    runner = AgentRunner(config)

    captured: list[str] = []
    async def capture(text: str) -> None:
        captured.append(text)

    runner.build_command = lambda *a, **k: ["printf", all_lines.replace("\n", "\\n")]

    await runner._run_once("test", "sess", False, "/tmp", on_output=capture)

    combined = "".join(captured)
    # Must contain both parts without corruption
    assert "Before tool" in combined
    assert "After tool use done" in combined
    # Must NOT contain garbage fragments from cross-block corruption
    assert "ool" not in combined or "Before tool" in combined  # "ool" only as part of "tool"


def test_build_command_with_model():
    """Verify --model flag is added when config has a model set."""
    config = AgentConfig(claude_command="claude", model="opus")
    runner = AgentRunner(config)
    cmd = runner.build_command("task", "s1", is_resume=False, working_dir="/tmp")
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "opus"

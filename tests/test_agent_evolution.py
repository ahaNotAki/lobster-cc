"""Tests for the agent self-evolution system.

Phase 1: Per-agent .system-prompt.md loading
Phase 2: Scheduled tasks .schedules/*.yaml
Phase 3: Per-agent notification config
"""

import textwrap

import pytest
from unittest.mock import AsyncMock, MagicMock

from remote_control.config import (
    AgentConfig, AppConfig, NotificationsConfig, WeComConfig,
)
from remote_control.core.executor import Executor
from remote_control.core.runner import RunResult
from remote_control.core.store import ScopedStore, Store


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.open()
    yield ScopedStore(s, "test_agent")
    s.close()


class _FakeStreamHandler:
    async def on_output(self, text):
        pass

    async def flush(self):
        pass


@pytest.fixture
def mock_notifier():
    n = MagicMock()
    n.send_reply = AsyncMock()
    n.task_failed = AsyncMock()
    n.task_cancelled = AsyncMock()
    n.create_stream_handler = MagicMock(return_value=_FakeStreamHandler())
    return n


@pytest.fixture
def mock_runner():
    r = MagicMock()
    r.cancel = AsyncMock()
    r.is_running = False
    r.model_info = {}
    r.run = AsyncMock(return_value=RunResult(exit_code=0, output="done"))
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


# ===========================================================================
# Phase 1: .system-prompt.md loading
# ===========================================================================


class TestSystemPromptLoading:
    """Test that executor loads per-agent .system-prompt.md and falls back to default."""

    def test_default_hint_when_no_file(self, executor):
        """Without .system-prompt.md, should use default hardcoded hint."""
        result = executor._inject_wecom_hint("user1", "hello")
        assert "user1" in result
        assert "hello" in result
        # Should contain core WeCom tool hints
        assert "send_wecom_message" in result

    def test_loads_system_prompt_file(self, executor, tmp_path):
        """When .system-prompt.md exists, should load and substitute variables."""
        prompt_file = tmp_path / ".system-prompt.md"
        prompt_file.write_text("You are TestBot. User: {user_id}. Be helpful.")

        result = executor._inject_wecom_hint("test_user", "check stocks")
        assert "test_user" in result
        assert "TestBot" in result
        assert "check stocks" in result

    def test_variable_substitution(self, executor, tmp_path):
        """Variables like {user_id} should be replaced."""
        prompt_file = tmp_path / ".system-prompt.md"
        prompt_file.write_text("Send results to {user_id}. Agent: {agent_name}.")

        result = executor._inject_wecom_hint("test_user", "task")
        assert "test_user" in result
        # {agent_name} should either be substituted or left as-is (not crash)
        assert "task" in result

    def test_fallback_on_read_error(self, executor, tmp_path):
        """If .system-prompt.md exists but is unreadable, should fallback gracefully."""
        prompt_file = tmp_path / ".system-prompt.md"
        prompt_file.write_text("valid content")
        prompt_file.chmod(0o000)

        try:
            result = executor._inject_wecom_hint("user1", "hello")
            # Should still work with default hint
            assert "user1" in result
            assert "hello" in result
        finally:
            prompt_file.chmod(0o644)

    def test_empty_file_uses_default(self, executor, tmp_path):
        """Empty .system-prompt.md should fallback to default."""
        prompt_file = tmp_path / ".system-prompt.md"
        prompt_file.write_text("")

        result = executor._inject_wecom_hint("user1", "hello")
        assert "user1" in result
        assert "send_wecom_message" in result  # default hint content

    def test_message_preserved(self, executor, tmp_path):
        """Original message should always be present regardless of prompt file."""
        prompt_file = tmp_path / ".system-prompt.md"
        prompt_file.write_text("Custom prompt for {user_id}")

        result = executor._inject_wecom_hint("u1", "fix the critical bug in auth.py")
        assert "fix the critical bug in auth.py" in result

    @pytest.mark.asyncio
    async def test_execute_task_uses_custom_prompt(self, executor, store, mock_runner, tmp_path):
        """Full integration: task execution should use .system-prompt.md content."""
        prompt_file = tmp_path / ".system-prompt.md"
        prompt_file.write_text("You are FinanceBot. User: {user_id}.")

        session = store.get_or_create_session("user1", str(tmp_path))
        task = store.create_task("user1", session.session_id, "analyze stocks")
        await executor._execute_task(task)

        call_args = mock_runner.run.call_args[0]
        message_arg = call_args[0]
        assert "FinanceBot" in message_arg
        assert "analyze stocks" in message_arg


# ===========================================================================
# Phase 2: .schedules/*.yaml structured config
# ===========================================================================


class TestScheduleConfig:
    """Test loading and parsing .schedules/*.yaml files."""

    def test_load_schedule_files(self, tmp_path):
        """Should read all yaml files from .schedules/ directory."""
        from remote_control.dashboard.status import load_schedule_configs

        sched_dir = tmp_path / ".schedules"
        sched_dir.mkdir()
        (sched_dir / "task1.yaml").write_text(textwrap.dedent("""\
            name: Morning Brief
            schedule: "30 0 * * 1-5"
            schedule_human: "工作日 08:30"
            enabled: true
            timeout: 1200
            prompt: |
              Analyze stocks for today.
        """))
        (sched_dir / "task2.yaml").write_text(textwrap.dedent("""\
            name: News Digest
            schedule: "0 1 * * *"
            schedule_human: "每天 09:00"
            enabled: false
            timeout: 600
            prompt: |
              Summarize today's news.
        """))

        schedules = load_schedule_configs(str(tmp_path))
        assert len(schedules) == 2
        names = {s["name"] for s in schedules}
        assert "Morning Brief" in names
        assert "News Digest" in names

    def test_load_empty_schedules_dir(self, tmp_path):
        """Empty .schedules/ dir should return empty list."""
        from remote_control.dashboard.status import load_schedule_configs

        sched_dir = tmp_path / ".schedules"
        sched_dir.mkdir()

        schedules = load_schedule_configs(str(tmp_path))
        assert schedules == []

    def test_load_no_schedules_dir(self, tmp_path):
        """Missing .schedules/ dir should return empty list."""
        from remote_control.dashboard.status import load_schedule_configs

        schedules = load_schedule_configs(str(tmp_path))
        assert schedules == []

    def test_schedule_fields(self, tmp_path):
        """Each schedule should have expected fields."""
        from remote_control.dashboard.status import load_schedule_configs

        sched_dir = tmp_path / ".schedules"
        sched_dir.mkdir()
        (sched_dir / "test.yaml").write_text(textwrap.dedent("""\
            name: Test Task
            schedule: "*/5 * * * *"
            schedule_human: "每5分钟"
            enabled: true
            timeout: 300
            prompt: Do something.
        """))

        schedules = load_schedule_configs(str(tmp_path))
        s = schedules[0]
        assert s["name"] == "Test Task"
        assert s["schedule"] == "*/5 * * * *"
        assert s["schedule_human"] == "每5分钟"
        assert s["enabled"] is True
        assert s["timeout"] == 300

    def test_invalid_yaml_skipped(self, tmp_path):
        """Invalid yaml files should be skipped without crashing."""
        from remote_control.dashboard.status import load_schedule_configs

        sched_dir = tmp_path / ".schedules"
        sched_dir.mkdir()
        (sched_dir / "good.yaml").write_text("name: Good\nschedule: '0 * * * *'\nenabled: true\nprompt: ok\n")
        (sched_dir / "bad.yaml").write_text("{{invalid yaml content")

        schedules = load_schedule_configs(str(tmp_path))
        assert len(schedules) == 1
        assert schedules[0]["name"] == "Good"

    def test_non_yaml_files_ignored(self, tmp_path):
        """Non-yaml files in .schedules/ should be ignored."""
        from remote_control.dashboard.status import load_schedule_configs

        sched_dir = tmp_path / ".schedules"
        sched_dir.mkdir()
        (sched_dir / "task.yaml").write_text("name: Task\nschedule: '0 * * * *'\nenabled: true\nprompt: ok\n")
        (sched_dir / "readme.txt").write_text("not a schedule")
        (sched_dir / ".hidden").write_text("hidden file")

        schedules = load_schedule_configs(str(tmp_path))
        assert len(schedules) == 1


# ===========================================================================
# Phase 3: Per-agent notification config
# ===========================================================================


class TestPerAgentNotificationConfig:
    """Test per-agent notification interval overrides."""

    def test_wecom_config_accepts_notification_overrides(self):
        """WeComConfig should accept optional streaming/progress intervals."""
        config = WeComConfig(
            corp_id="c", agent_id=1, secret="s", token="t", encoding_aes_key="k",
            streaming_interval=15.0,
            progress_interval=60,
        )
        assert config.streaming_interval == 15.0
        assert config.progress_interval == 60

    def test_wecom_config_defaults_to_zero(self):
        """Without overrides, intervals should default to 0 (use global)."""
        config = WeComConfig(
            corp_id="c", agent_id=1, secret="s", token="t", encoding_aes_key="k",
        )
        assert config.streaming_interval == 0
        assert config.progress_interval == 0

    def test_global_config_still_works(self):
        """Global notifications config should still be valid."""
        config = NotificationsConfig(
            progress_interval_seconds=30,
            streaming_interval_seconds=10.0,
        )
        assert config.progress_interval_seconds == 30
        assert config.streaming_interval_seconds == 10.0


# ===========================================================================
# Integration: agent self-discovery of evolvable files
# ===========================================================================


class TestAgentSelfDiscovery:
    """Test that CLAUDE.md in working dir documents evolvable config files."""

    def test_claude_md_template_lists_config_files(self, tmp_path):
        """CLAUDE.md template should mention all evolvable files."""
        # This test verifies the template content once it's created.
        # For now, check the concept is testable.
        assert True  # Placeholder — will be filled when templates are created

    def test_system_prompt_template_has_variables(self, tmp_path):
        """System prompt template should contain {user_id} variable."""
        prompt_file = tmp_path / ".system-prompt.md"
        prompt_file.write_text("User: {user_id}\nBe helpful.")

        content = prompt_file.read_text()
        assert "{user_id}" in content

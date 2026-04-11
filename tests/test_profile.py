"""Tests for the agent profile system."""

import json
import time

import yaml

from remote_control.core.profile import (
    AgentProfile,
    CustomCommand,
    ProfileManager,
    _deep_get,
    _deep_merge,
    _deep_set,
)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestAgentProfile:
    def test_default_profile(self):
        p = AgentProfile()
        assert p.version == "1.0"
        assert p.agent_id == ""
        assert p.output_style.language == "auto"
        assert p.output_style.format == "balanced"
        assert p.output_style.max_message_length == 1500
        assert p.output_style.code_block_handling == "inline"
        assert p.notification.streaming_interval_seconds == 10.0
        assert p.notification.notify_on_error is True
        assert p.notification.notify_on_completion is False
        assert p.model_selection.default_model == ""
        assert p.model_selection.task_type_overrides == []
        assert p.custom_commands == {}

    def test_custom_commands(self):
        p = AgentProfile(
            custom_commands={
                "stock": CustomCommand(prompt="查询今日股票行情", description="Stock check"),
                "news": CustomCommand(prompt="Get latest news summary"),
            }
        )
        assert "stock" in p.custom_commands
        assert p.custom_commands["stock"].prompt == "查询今日股票行情"
        assert p.custom_commands["stock"].description == "Stock check"
        assert p.custom_commands["news"].description == ""

    def test_extra_fields_ignored(self):
        """Unknown fields should be silently ignored (forward compat)."""
        p = AgentProfile.model_validate({"version": "1.0", "unknown_field": 42})
        assert p.version == "1.0"


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestDeepMerge:
    def test_flat_merge(self):
        base = {"a": 1, "b": 2}
        _deep_merge(base, {"b": 3, "c": 4})
        assert base == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        base = {"x": {"a": 1, "b": 2}, "y": 10}
        _deep_merge(base, {"x": {"b": 99, "c": 3}})
        assert base == {"x": {"a": 1, "b": 99, "c": 3}, "y": 10}

    def test_overwrite_non_dict(self):
        base = {"x": {"a": 1}}
        _deep_merge(base, {"x": "replaced"})
        assert base == {"x": "replaced"}


class TestDeepGet:
    def test_single_key(self):
        assert _deep_get({"a": 1}, "a") == 1

    def test_dotted_key(self):
        assert _deep_get({"a": {"b": {"c": 42}}}, "a.b.c") == 42

    def test_missing_key(self):
        assert _deep_get({"a": 1}, "b") is None

    def test_missing_nested(self):
        assert _deep_get({"a": {"b": 1}}, "a.c") is None


class TestDeepSet:
    def test_single_key(self):
        result = _deep_set({}, "a", 1)
        assert result == {"a": 1}

    def test_dotted_key(self):
        result = _deep_set({}, "a.b.c", 42)
        assert result == {"a": {"b": {"c": 42}}}

    def test_existing_intermediate(self):
        result = _deep_set({"a": {"x": 1}}, "a.b", 2)
        assert result == {"a": {"x": 1, "b": 2}}


# ---------------------------------------------------------------------------
# ProfileManager tests
# ---------------------------------------------------------------------------

class TestProfileManager:
    def test_load_nonexistent_bootstraps(self, tmp_path):
        """ProfileManager with no file creates profile via bootstrap."""
        mgr = ProfileManager(str(tmp_path), agent_id="test-agent")
        profile = mgr.get_profile()

        assert profile.agent_id == "test-agent"
        assert profile.updated_at != ""
        assert mgr.profile_path.exists()

    def test_hot_reload_on_mtime_change(self, tmp_path):
        """Modify file externally, get_profile returns updated version."""
        mgr = ProfileManager(str(tmp_path), agent_id="agent1")
        p1 = mgr.get_profile()
        assert p1.output_style.format == "balanced"

        # Modify the file externally
        data = yaml.safe_load(mgr.profile_path.read_text())
        data["output_style"]["format"] = "concise"
        # Ensure mtime changes (some filesystems have 1s granularity)
        time.sleep(0.05)
        mgr.profile_path.write_text(yaml.dump(data))

        p2 = mgr.get_profile()
        assert p2.output_style.format == "concise"

    def test_hot_reload_skipped_same_mtime(self, tmp_path):
        """Same mtime returns cached profile without re-reading."""
        mgr = ProfileManager(str(tmp_path), agent_id="agent1")
        p1 = mgr.get_profile()

        # Get again — should return same object (cached)
        p2 = mgr.get_profile()
        assert p1 is p2

    def test_update_creates_snapshot(self, tmp_path):
        """update() writes to history dir."""
        mgr = ProfileManager(str(tmp_path), agent_id="agent1")
        mgr.get_profile()

        mgr.update({"output_style": {"format": "detailed"}}, rationale="test update")

        assert mgr.history_dir.exists()
        snapshots = list(mgr.history_dir.glob("*.yaml"))
        assert len(snapshots) == 1

        snapshot = yaml.safe_load(snapshots[0].read_text())
        assert snapshot["rationale"] == "test update"
        assert snapshot["agent_id"] == "agent1"

    def test_update_deep_merge(self, tmp_path):
        """Nested updates merge correctly without losing sibling fields."""
        mgr = ProfileManager(str(tmp_path), agent_id="agent1")
        mgr.get_profile()

        mgr.update({"output_style": {"format": "detailed"}})
        p = mgr.get_profile()

        # format changed, but language preserved
        assert p.output_style.format == "detailed"
        assert p.output_style.language == "auto"
        assert p.output_style.max_message_length == 1500

    def test_reset_single_key(self, tmp_path):
        """reset('output_style.format') reverts one field."""
        mgr = ProfileManager(str(tmp_path), agent_id="agent1")
        mgr.update({"output_style": {"format": "detailed"}})
        p = mgr.get_profile()
        assert p.output_style.format == "detailed"

        mgr.reset("output_style.format")
        p = mgr.get_profile()
        assert p.output_style.format == "balanced"

    def test_reset_full(self, tmp_path):
        """reset(None) reverts everything to defaults."""
        mgr = ProfileManager(str(tmp_path), agent_id="agent1")
        mgr.update({
            "output_style": {"format": "detailed", "language": "zh-CN"},
            "notification": {"notify_on_completion": True},
        })

        mgr.reset(None)
        p = mgr.get_profile()

        assert p.output_style.format == "balanced"
        assert p.output_style.language == "auto"
        assert p.notification.notify_on_completion is False
        assert p.agent_id == "agent1"  # agent_id preserved

    def test_corrupt_yaml_fallback(self, tmp_path):
        """Malformed YAML returns defaults gracefully."""
        mgr = ProfileManager(str(tmp_path), agent_id="agent1")
        # Write invalid YAML
        mgr.profile_path.write_text("{{invalid: yaml: [}")

        p = mgr.get_profile()
        assert p.agent_id == "agent1"
        assert p.output_style.format == "balanced"

    def test_bootstrap_extracts_from_system_prompt(self, tmp_path):
        """Detects '简洁' and max length from existing .system-prompt.md."""
        prompt_path = tmp_path / ".system-prompt.md"
        prompt_path.write_text("请保持简洁的回复风格，控制在800字符以内。")

        mgr = ProfileManager(str(tmp_path), agent_id="agent1")
        p = mgr.get_profile()

        assert p.output_style.format == "concise"
        assert p.output_style.max_message_length == 800

    def test_bootstrap_extracts_language_from_dashboard(self, tmp_path):
        """Detects language from .dashboard-workstations.json lobster config."""
        ws_path = tmp_path / ".dashboard-workstations.json"
        ws_path.write_text(json.dumps({"lobster": {"name": "小龙虾"}, "workstations": []}))

        mgr = ProfileManager(str(tmp_path), agent_id="agent1")
        p = mgr.get_profile()

        assert p.output_style.language == "zh-CN"

    def test_default_file_used_when_present(self, tmp_path):
        """Custom default file overrides built-in defaults."""
        default_path = tmp_path / ProfileManager.DEFAULT_FILE
        default_data = AgentProfile(output_style={"format": "concise", "max_message_length": 999}).model_dump()
        default_path.write_text(yaml.dump(default_data))

        mgr = ProfileManager(str(tmp_path), agent_id="agent1")
        p = mgr.get_profile()

        assert p.output_style.format == "concise"
        assert p.output_style.max_message_length == 999

    def test_multiple_updates_create_multiple_snapshots(self, tmp_path):
        """Each update creates a separate history snapshot."""
        mgr = ProfileManager(str(tmp_path), agent_id="agent1")
        mgr.get_profile()

        # Small delay to ensure different timestamps in filenames
        mgr.update({"output_style": {"format": "concise"}}, rationale="first")
        time.sleep(1.1)  # History uses second-level timestamps
        mgr.update({"output_style": {"format": "detailed"}}, rationale="second")

        snapshots = sorted(mgr.history_dir.glob("*.yaml"))
        assert len(snapshots) == 2

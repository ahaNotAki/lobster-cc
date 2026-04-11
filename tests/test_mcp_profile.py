"""Tests for the Profile MCP server tools."""

import json

import pytest

from remote_control.mcp import profile_server
from remote_control.mcp.profile_server import (
    get_agent_config,
    set_agent_config,
    list_agent_config,
    reset_agent_config,
    _get_manager,
)


@pytest.fixture(autouse=True)
def reset_manager():
    """Reset the module-level manager singleton between tests."""
    profile_server._manager = None
    yield
    profile_server._manager = None


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    """Set up environment variables pointing to a temp working dir."""
    monkeypatch.setenv("AGENT_WORKING_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_ID", "1000002")
    return tmp_path


# --- get_agent_config ---


def test_get_config_single_key(profile_env):
    result = json.loads(get_agent_config("output_style.format"))
    assert result["name"] == "output_style.format"
    assert result["value"] == "balanced"  # default
    assert result["type"] == "str"


def test_get_config_all(profile_env):
    result = json.loads(get_agent_config(""))
    assert result["name"] == "all"
    assert result["type"] == "object"
    assert "output_style" in result["value"]
    assert "notification" in result["value"]
    assert "custom_commands" in result["value"]


def test_get_config_all_explicit(profile_env):
    result = json.loads(get_agent_config("all"))
    assert result["name"] == "all"
    assert result["type"] == "object"


def test_get_config_invalid_key(profile_env):
    result = json.loads(get_agent_config("nonexistent.key"))
    assert "error" in result or result.get("value") is None


def test_get_config_nested_int(profile_env):
    result = json.loads(get_agent_config("output_style.max_message_length"))
    assert result["value"] == 1500
    assert result["type"] == "int"


def test_get_config_nested_bool(profile_env):
    result = json.loads(get_agent_config("notification.notify_on_error"))
    assert result["value"] is True
    assert result["type"] == "bool"


# --- set_agent_config ---


def test_set_config(profile_env):
    result = json.loads(set_agent_config("output_style.format", '"concise"', "user prefers short answers"))
    assert result["status"] == "ok"
    assert result["old_value"] == "balanced"
    assert result["new_value"] == "concise"

    # Verify it persisted
    verify = json.loads(get_agent_config("output_style.format"))
    assert verify["value"] == "concise"


def test_set_config_int(profile_env):
    result = json.loads(set_agent_config("output_style.max_message_length", "2000", "increase limit"))
    assert result["status"] == "ok"
    assert result["old_value"] == 1500
    assert result["new_value"] == 2000


def test_set_config_bool(profile_env):
    result = json.loads(set_agent_config("notification.notify_on_completion", "true", "want notifications"))
    assert result["status"] == "ok"
    assert result["old_value"] is False
    assert result["new_value"] is True


def test_set_config_invalid_key(profile_env):
    result = json.loads(set_agent_config("nonexistent.key", '"value"'))
    assert "error" in result
    assert "Unknown" in result["error"] or "nvalid" in result["error"].lower() or result["error"]


def test_set_config_invalid_json(profile_env):
    result = json.loads(set_agent_config("output_style.format", "not valid json{"))
    assert "error" in result


def test_set_config_creates_audit_snapshot(profile_env):
    set_agent_config("output_style.format", '"detailed"', "testing audit")
    history_dir = profile_env / ".agent-profile-history"
    assert history_dir.exists()
    snapshots = list(history_dir.glob("*.yaml"))
    assert len(snapshots) >= 1


# --- list_agent_config ---


def test_list_config(profile_env):
    result = list_agent_config()
    assert "output_style" in result
    assert "notification" in result
    assert "custom_commands" in result
    assert "Agent Profile Configuration" in result
    # Should be YAML-like text, not JSON
    assert "{" not in result.split("\n")[0]  # first line is a comment, not JSON


def test_list_config_shows_agent_id(profile_env):
    result = list_agent_config()
    assert "1000002" in result


# --- reset_agent_config ---


def test_reset_single(profile_env):
    # Change a value first
    set_agent_config("output_style.format", '"detailed"', "change for test")
    verify = json.loads(get_agent_config("output_style.format"))
    assert verify["value"] == "detailed"

    # Reset it
    result = json.loads(reset_agent_config("output_style.format"))
    assert result["status"] == "ok"

    # Verify it's back to default
    verify = json.loads(get_agent_config("output_style.format"))
    assert verify["value"] == "balanced"


def test_reset_all(profile_env):
    # Change multiple values
    set_agent_config("output_style.format", '"detailed"', "test")
    set_agent_config("output_style.max_message_length", "3000", "test")

    # Reset all
    result = json.loads(reset_agent_config(""))
    assert result["status"] == "ok"
    assert "All settings reset" in result["message"]

    # Verify defaults restored
    fmt = json.loads(get_agent_config("output_style.format"))
    assert fmt["value"] == "balanced"
    length = json.loads(get_agent_config("output_style.max_message_length"))
    assert length["value"] == 1500


def test_reset_all_explicit(profile_env):
    result = json.loads(reset_agent_config("all"))
    assert result["status"] == "ok"


def test_reset_invalid_key(profile_env):
    result = json.loads(reset_agent_config("nonexistent.key"))
    assert "error" in result


# --- _get_manager ---


def test_get_manager_missing_env(monkeypatch):
    monkeypatch.delenv("AGENT_WORKING_DIR", raising=False)
    monkeypatch.delenv("AGENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="AGENT_WORKING_DIR"):
        _get_manager()

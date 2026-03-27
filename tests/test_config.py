"""Tests for config loading and validation."""

import pytest
import yaml

from remote_control.config import WeComConfig, load_config


def test_load_config_single_agent(tmp_path):
    """Single wecom dict is auto-normalized to a list."""
    config_data = {
        "wecom": {
            "corp_id": "corp123",
            "agent_id": 1000002,
            "secret": "sec",
            "token": "tok",
            "encoding_aes_key": "key43chars_____________________________X",
        },
        "agent": {"claude_command": "/bin/echo"},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(config_data))

    config = load_config(path)
    assert len(config.wecom) == 1
    assert config.wecom[0].corp_id == "corp123"
    assert config.wecom[0].agent_id == 1000002
    assert config.agent.claude_command == "/bin/echo"
    assert config.server.port == 8080  # default
    assert config.wecom[0].mode == "relay"  # default


def test_load_config_multi_agent(tmp_path):
    """Multiple wecom agents in a list."""
    config_data = {
        "wecom": [
            {
                "name": "agent-a",
                "corp_id": "corp1",
                "agent_id": 1000002,
                "secret": "s1",
                "token": "t1",
                "encoding_aes_key": "k1",
            },
            {
                "name": "agent-b",
                "corp_id": "corp1",
                "agent_id": 1000003,
                "secret": "s2",
                "token": "t2",
                "encoding_aes_key": "k2",
            },
        ],
        "agent": {"claude_command": "/bin/echo"},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(config_data))

    config = load_config(path)
    assert len(config.wecom) == 2
    assert config.wecom[0].name == "agent-a"
    assert config.wecom[0].agent_id == 1000002
    assert config.wecom[1].name == "agent-b"
    assert config.wecom[1].agent_id == 1000003


def test_load_config_file_not_found():
    with pytest.raises(FileNotFoundError, match="not found"):
        load_config("/nonexistent/config.yaml")


def test_load_config_missing_required_field(tmp_path):
    config_data = {"wecom": {"corp_id": "corp123"}}  # missing required fields
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(config_data))

    with pytest.raises(Exception):  # pydantic ValidationError
        load_config(path)


def test_load_config_with_all_fields(tmp_path):
    config_data = {
        "wecom": {
            "corp_id": "corp",
            "agent_id": 1,
            "secret": "s",
            "token": "t",
            "encoding_aes_key": "k",
            "mode": "relay",
            "relay_url": "http://relay.example.com",
            "relay_poll_interval_seconds": 10.0,
        },
        "agent": {
            "claude_command": "/usr/local/bin/claude",
            "default_working_dir": "/projects",
            "model": "opus",
            "task_timeout_seconds": 300,
        },
        "server": {"host": "127.0.0.1", "port": 9090},
        "storage": {"db_path": "/tmp/test.db"},
        "notifications": {"progress_interval_seconds": 60},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(config_data))

    config = load_config(path)
    assert config.wecom[0].mode == "relay"
    assert config.wecom[0].relay_url == "http://relay.example.com"
    assert config.wecom[0].relay_poll_interval_seconds == 10.0
    assert config.agent.claude_command == "/usr/local/bin/claude"
    assert config.agent.model == "opus"
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 9090


def test_load_config_claude_not_in_path(tmp_path):
    """load_config should fail fast if claude_command is not found in PATH."""
    config_data = {
        "wecom": {
            "corp_id": "c",
            "agent_id": 1,
            "secret": "s",
            "token": "t",
            "encoding_aes_key": "k",
        },
        "agent": {"claude_command": "nonexistent_command_xyz_12345"},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(config_data))

    with pytest.raises(ValueError, match="not found in PATH"):
        load_config(path)


def test_load_config_resolves_bare_command(tmp_path):
    """load_config should resolve a bare command name to its absolute path."""
    config_data = {
        "wecom": {
            "corp_id": "c",
            "agent_id": 1,
            "secret": "s",
            "token": "t",
            "encoding_aes_key": "k",
        },
        "agent": {"claude_command": "echo"},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(config_data))

    config = load_config(path)
    # Should be resolved to an absolute path
    assert "/" in config.agent.claude_command or "\\" in config.agent.claude_command


def test_wecom_config_defaults():
    config = WeComConfig(
        corp_id="c", agent_id=1, secret="s", token="t", encoding_aes_key="k"
    )
    assert config.mode == "relay"
    assert config.relay_url == ""
    assert config.relay_poll_interval_seconds == 5.0
    assert config.name == ""

"""Configuration loading and validation."""

import shutil
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class WeComConfig(BaseModel):
    name: str = ""  # Optional label for this agent (for logging)
    corp_id: str
    agent_id: int
    secret: str
    token: str
    encoding_aes_key: str
    mode: str = "relay"  # "relay" or "callback"
    relay_url: str = ""  # URL of the relay service (required for relay mode)
    relay_poll_interval_seconds: float = 5.0
    proxy: str = ""  # SOCKS5 proxy for outbound API calls, e.g. "socks5://127.0.0.1:1080"
    working_dir: str = ""  # Per-agent working dir override (falls back to agent.default_working_dir)
    streaming_interval: float = 0  # Per-agent override (0 = use global notifications.streaming_interval_seconds)
    progress_interval: int = 0  # Per-agent override (0 = use global notifications.progress_interval_seconds)


class AgentConfig(BaseModel):
    claude_command: str = "claude"
    default_working_dir: str = "."
    allowed_tools: list[str] = Field(default_factory=list)
    model: str = ""
    task_timeout_seconds: int = 600
    max_output_length: int = 4000
    watchdog_interval_seconds: int = 60
    watchdog_timeout_seconds: int = 1200  # 20 minutes


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080


class StorageConfig(BaseModel):
    db_path: str = "./remote_control.db"


class MemoryConfig(BaseModel):
    enabled: bool = True
    raw_summary_max_chars: int = 500
    recent_context_limit: int = 5
    keyword_match_limit: int = 5
    max_context_chars: int = 2000


class NotificationsConfig(BaseModel):
    progress_interval_seconds: int = 30
    streaming_interval_seconds: float = 10.0


class DashboardConfig(BaseModel):
    enabled: bool = False
    password: str = ""
    secret: str = "rc-dashboard-default-secret"  # HMAC signing key for auth tokens


class AppConfig(BaseModel):
    wecom: list[WeComConfig]
    agent: AgentConfig = Field(default_factory=AgentConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)


def load_config(path: str | Path) -> AppConfig:
    """Load and validate config from a YAML file.

    The `wecom` field can be a single dict (backwards compatible) or a list of dicts.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path) as f:
        raw = yaml.safe_load(f)

    # Normalize wecom: single dict → list
    wecom_raw = raw.get("wecom", [])
    if isinstance(wecom_raw, dict):
        raw["wecom"] = [wecom_raw]

    config = AppConfig(**raw)

    # Resolve claude_command: if it's a bare name, look it up in PATH and
    # replace with the absolute path. Fail fast if not found.
    cmd = config.agent.claude_command
    if "/" not in cmd and "\\" not in cmd:
        resolved = shutil.which(cmd)
        if resolved is None:
            raise ValueError(
                f"Claude CLI command '{cmd}' not found in PATH. "
                f"Set agent.claude_command to the full path (e.g., /usr/local/bin/claude)."
            )
        config.agent.claude_command = resolved

    return config

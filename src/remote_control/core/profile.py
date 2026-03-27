"""Agent profile system — per-agent preferences with hot-reload and audit trail."""

import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class OutputStyle(BaseModel):
    language: str = "auto"  # "auto" | "zh-CN" | "en-US"
    format: str = "balanced"  # "concise" | "balanced" | "detailed"
    max_message_length: int = 1500
    code_block_handling: str = "inline"  # "inline" | "file" | "truncate"


class NotificationPrefs(BaseModel):
    streaming_interval_seconds: float = 10.0
    progress_interval_seconds: float = 30.0
    notify_on_completion: bool = False
    notify_on_error: bool = True


class ModelOverride(BaseModel):
    pattern: str  # regex or keywords, e.g. "股票|stock"
    model: str  # e.g. "claude-sonnet-4-5"
    rationale: str = ""


class ModelSelection(BaseModel):
    default_model: str = ""  # empty = use config.yaml default
    task_type_overrides: list[ModelOverride] = []


class MemoryPrefs(BaseModel):
    keyword_match_limit: int = 5
    recent_context_limit: int = 5
    max_context_chars: int = 2000


class CustomCommand(BaseModel):
    prompt: str  # prompt to expand to
    description: str = ""  # shown in /help


class AgentProfile(BaseModel):
    version: str = "1.0"
    agent_id: str = ""
    updated_at: str = ""

    output_style: OutputStyle = OutputStyle()
    notification: NotificationPrefs = NotificationPrefs()
    model_selection: ModelSelection = ModelSelection()
    memory: MemoryPrefs = MemoryPrefs()
    custom_commands: dict[str, CustomCommand] = {}  # key = command name WITHOUT /

    model_config = ConfigDict(extra="ignore")  # ignore unknown fields for forward compat


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, updates: dict) -> None:
    """Recursively merge updates into base dict."""
    for k, v in updates.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _deep_get(data: dict, dotted_key: str):
    """Get nested value by dotted key, e.g. 'output_style.format'."""
    keys = dotted_key.split(".")
    for k in keys:
        if isinstance(data, dict) and k in data:
            data = data[k]
        else:
            return None
    return data


def _deep_set(data: dict, dotted_key: str, value) -> dict:
    """Set nested value by dotted key, creating intermediate dicts."""
    keys = dotted_key.split(".")
    d = data
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value
    return data


# ---------------------------------------------------------------------------
# ProfileManager
# ---------------------------------------------------------------------------

class ProfileManager:
    """Manages per-agent profile with hot-reload and audit trail."""

    PROFILE_FILE = ".agent-profile.yaml"
    DEFAULT_FILE = ".agent-profile.default.yaml"
    HISTORY_DIR = ".agent-profile-history"

    def __init__(self, working_dir: str, agent_id: str = ""):
        self._working_dir = Path(working_dir)
        self._agent_id = agent_id
        self._profile: AgentProfile | None = None
        self._last_mtime: float = 0

    @property
    def profile_path(self) -> Path:
        return self._working_dir / self.PROFILE_FILE

    @property
    def default_path(self) -> Path:
        return self._working_dir / self.DEFAULT_FILE

    @property
    def history_dir(self) -> Path:
        return self._working_dir / self.HISTORY_DIR

    def get_profile(self) -> AgentProfile:
        """Get current profile, hot-reload if file changed (mtime check)."""
        path = self.profile_path
        if not path.exists():
            profile = self._bootstrap()
            self._profile = profile
            self._last_mtime = path.stat().st_mtime
            return profile

        try:
            mtime = path.stat().st_mtime
            if self._profile and mtime == self._last_mtime:
                return self._profile
            self._profile = self._load(path)
            self._last_mtime = mtime
            return self._profile
        except Exception:
            logger.warning("Failed to load profile from %s, using defaults", path, exc_info=True)
            return AgentProfile(agent_id=self._agent_id)

    def update(self, updates: dict, rationale: str = "") -> AgentProfile:
        """Update profile fields, save audit snapshot, write new profile."""
        old = self.get_profile()

        # Save snapshot before update
        self._save_snapshot(old, rationale)

        # Apply updates to a dict, then reconstruct
        data = old.model_dump()
        _deep_merge(data, updates)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()

        new_profile = AgentProfile.model_validate(data)
        self._save(new_profile)
        self._profile = new_profile
        self._last_mtime = self.profile_path.stat().st_mtime

        logger.info("Agent profile updated (agent=%s): %s", self._agent_id, rationale)
        return new_profile

    def reset(self, key: str | None = None) -> AgentProfile:
        """Reset one key or entire profile to defaults."""
        defaults = self._load_defaults()
        if key is None:
            # Full reset
            self._save_snapshot(self.get_profile(), "Full reset")
            defaults.agent_id = self._agent_id
            defaults.updated_at = datetime.now(timezone.utc).isoformat()
            self._save(defaults)
            self._profile = defaults
        else:
            # Single key reset — get default value and apply
            default_data = defaults.model_dump()
            value = _deep_get(default_data, key)
            if value is not None:
                updates = _deep_set({}, key, value)
                return self.update(updates, f"Reset {key} to default")
        return self.get_profile()

    def _bootstrap(self) -> AgentProfile:
        """Create initial profile — extract from existing config files if present."""
        profile = self._load_defaults()
        profile.agent_id = self._agent_id
        profile.updated_at = datetime.now(timezone.utc).isoformat()

        # Try to extract from existing .system-prompt.md
        prompt_path = self._working_dir / ".system-prompt.md"
        if prompt_path.exists():
            try:
                content = prompt_path.read_text()
                if "简洁" in content or "concise" in content.lower():
                    profile.output_style.format = "concise"
                if "适当详细" in content:
                    profile.output_style.format = "balanced"
                # Extract max length if mentioned
                m = re.search(r'(\d{3,4})\s*字符', content)
                if m:
                    profile.output_style.max_message_length = int(m.group(1))
            except OSError:
                pass

        # Try to extract lobster config from dashboard
        ws_path = self._working_dir / ".dashboard-workstations.json"
        if ws_path.exists():
            try:
                import json
                data = json.loads(ws_path.read_text())
                if data.get("lobster"):
                    profile.output_style.language = "zh-CN"
            except (OSError, json.JSONDecodeError):
                pass

        self._save(profile)
        logger.info("Bootstrapped agent profile for agent=%s at %s", self._agent_id, self.profile_path)
        return profile

    def _load(self, path: Path) -> AgentProfile:
        """Load profile from YAML file."""
        data = yaml.safe_load(path.read_text()) or {}
        return AgentProfile.model_validate(data)

    def _load_defaults(self) -> AgentProfile:
        """Load from .agent-profile.default.yaml if it exists, else built-in defaults."""
        if self.default_path.exists():
            try:
                return self._load(self.default_path)
            except Exception:
                pass
        return AgentProfile()

    def _save(self, profile: AgentProfile) -> None:
        """Write profile to YAML (atomic via temp file)."""
        data = profile.model_dump(exclude_defaults=False)
        content = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)

        # Atomic write
        fd, tmp = tempfile.mkstemp(dir=self._working_dir, suffix=".yaml")
        try:
            os.write(fd, content.encode())
            os.close(fd)
            fd = -1  # Mark as closed
            os.replace(tmp, self.profile_path)
        except Exception:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _save_snapshot(self, profile: AgentProfile, rationale: str) -> None:
        """Save timestamped snapshot to history dir."""
        self.history_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%d_%H%M%S_%f")
        snapshot = {
            "timestamp": now.isoformat(),
            "agent_id": self._agent_id,
            "rationale": rationale,
            "profile": profile.model_dump(),
        }
        path = self.history_dir / f"{ts}.yaml"
        path.write_text(yaml.dump(snapshot, default_flow_style=False, allow_unicode=True, sort_keys=False))

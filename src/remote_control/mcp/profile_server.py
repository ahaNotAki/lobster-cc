"""Profile MCP Server — exposes agent self-configuration as MCP tools.

This is a standalone stdio-based MCP server. It is registered in .mcp.json
so that Claude Code processes can read and modify their own agent profile.

Configuration is via environment variables (set in .mcp.json):
    AGENT_WORKING_DIR — the agent's working directory (.agent-profile.yaml lives here)
    AGENT_ID          — the agent's numeric ID
"""

import json
import os

import yaml
from mcp.server.fastmcp import FastMCP

from remote_control.core.profile import ProfileManager, _deep_get

# ---------------------------------------------------------------------------
# Singleton ProfileManager (created lazily on first tool call)
# ---------------------------------------------------------------------------

_manager: ProfileManager | None = None


def _get_manager() -> ProfileManager:
    global _manager
    if _manager is None:
        working_dir = os.environ.get("AGENT_WORKING_DIR", "")
        agent_id = os.environ.get("AGENT_ID", "")
        if not working_dir:
            raise RuntimeError("AGENT_WORKING_DIR environment variable must be set.")
        _manager = ProfileManager(working_dir=working_dir, agent_id=agent_id)
    return _manager


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "agent-profile",
    instructions="Read and modify agent configuration (output style, personality, behavior, etc.).",
)


@mcp.tool()
def get_agent_config(name: str = "") -> str:
    """Get an agent configuration value by dotted key path.

    Args:
        name: Dotted key path, e.g. "output_style.format". Empty or "all" returns the entire profile.
    """
    try:
        mgr = _get_manager()
        profile = mgr.get_profile()
        data = profile.model_dump()

        if not name or name.lower() == "all":
            return json.dumps(
                {"name": "all", "value": data, "type": "object"},
                ensure_ascii=False,
                indent=2,
            )

        value = _deep_get(data, name)
        if value is None:
            return json.dumps({"error": f"Unknown config key: {name}"})

        return json.dumps(
            {"name": name, "value": value, "type": type(value).__name__},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def set_agent_config(name: str, value: str, rationale: str = "") -> str:
    """Set an agent configuration value.

    Args:
        name: Dotted key path, e.g. "output_style.format".
        value: JSON-encoded value to set, e.g. '"concise"' or '1500' or 'true'.
        rationale: Why this change is being made (for audit trail).
    """
    try:
        mgr = _get_manager()
        profile = mgr.get_profile()
        data = profile.model_dump()

        # Validate key exists
        old_value = _deep_get(data, name)
        if old_value is None:
            return json.dumps({"error": f"Unknown config key: {name}"})

        # Parse the JSON value
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return json.dumps({"error": f"Invalid JSON value: {value}"})

        # Build nested update dict from dotted key
        updates = {}
        keys = name.split(".")
        d = updates
        for k in keys[:-1]:
            d[k] = {}
            d = d[k]
        d[keys[-1]] = parsed

        mgr.update(updates, rationale=rationale)

        return json.dumps(
            {
                "status": "ok",
                "key": name,
                "old_value": old_value,
                "new_value": parsed,
                "message": f"Updated {name}: {old_value!r} -> {parsed!r}",
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_agent_config() -> str:
    """List the full agent profile as formatted YAML with section descriptions."""
    try:
        mgr = _get_manager()
        profile = mgr.get_profile()
        data = profile.model_dump()

        section_descriptions = {
            "output_style": "Output style — controls response formatting",
            "notification": "Notification — streaming and progress intervals",
            "model_selection": "Model selection — default model and task-type overrides",
            "custom_commands": "Custom commands — user-defined slash commands",
        }

        lines = ["# Agent Profile Configuration", f"# Agent ID: {profile.agent_id}", ""]

        # Meta fields
        lines.append(f"version: {profile.version}")
        lines.append(f"agent_id: {profile.agent_id!r}")
        lines.append(f"updated_at: {profile.updated_at!r}")
        lines.append("")

        for section_key in ["output_style", "notification", "model_selection", "custom_commands"]:
            desc = section_descriptions.get(section_key, section_key)
            lines.append(f"# {desc}")
            section_data = data.get(section_key, {})
            section_yaml = yaml.dump(
                {section_key: section_data},
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            ).strip()
            lines.append(section_yaml)
            lines.append("")

        return "\n".join(lines)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def reset_agent_config(name: str = "") -> str:
    """Reset agent configuration to defaults.

    Args:
        name: Dotted key path to reset, e.g. "output_style.format". If empty, resets ALL settings.
    """
    try:
        mgr = _get_manager()

        if not name or name.lower() == "all":
            mgr.reset(key=None)
            return json.dumps(
                {"status": "ok", "message": "All settings reset to defaults."},
            )

        # Validate key exists
        profile = mgr.get_profile()
        data = profile.model_dump()
        old_value = _deep_get(data, name)
        if old_value is None:
            return json.dumps({"error": f"Unknown config key: {name}"})

        mgr.reset(key=name)

        new_profile = mgr.get_profile()
        new_data = new_profile.model_dump()
        new_value = _deep_get(new_data, name)

        return json.dumps(
            {
                "status": "ok",
                "key": name,
                "old_value": old_value,
                "new_value": new_value,
                "message": f"Reset {name}: {old_value!r} -> {new_value!r}",
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run()

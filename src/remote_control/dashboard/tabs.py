"""Dashboard custom tabs — load tab configs and tab data from working directory."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

TABS_FILE = ".dashboard-tabs.json"
MAX_FILE_SIZE = 1_000_000  # 1MB

_REQUIRED_FIELDS = {"id", "label", "type", "source"}


def load_tab_configs(working_dir: str) -> list[dict]:
    """Load tab definitions from .dashboard-tabs.json. Returns [] on error."""
    path = Path(working_dir) / TABS_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, list):
            return []
        # Filter out tabs missing required fields
        valid = []
        for tab in data:
            if not isinstance(tab, dict):
                continue
            if _REQUIRED_FIELDS.issubset(tab.keys()):
                valid.append(tab)
            else:
                missing = _REQUIRED_FIELDS - tab.keys()
                logger.warning("Tab %r missing fields: %s — skipped", tab.get("id", "?"), missing)
        return valid
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to load %s, returning empty", path, exc_info=True)
        return []


def load_tab_data(working_dir: str, tab_config: dict) -> dict:
    """Load tab data from source file.

    Returns {template, data} for data tabs, {html} for html tabs,
    {chart_type, title, labels, datasets} for chart tabs, or {error} on failure.

    Path security: resolved source must be within working_dir.
    """
    source = tab_config.get("source", "")
    tab_type = tab_config.get("type", "data")
    working_real = os.path.realpath(working_dir)

    # Resolve the source path
    if os.path.isabs(source):
        resolved = os.path.realpath(source)
    else:
        resolved = os.path.realpath(os.path.join(working_dir, source))

    # Security: must be within working_dir
    if not resolved.startswith(working_real + os.sep) and resolved != working_real:
        return {"error": "source path outside working directory"}

    if not os.path.exists(resolved):
        return {"error": f"file not found: {source}"}

    # Size check
    try:
        size = os.path.getsize(resolved)
        if size > MAX_FILE_SIZE:
            return {"error": f"file too large: {size} bytes (max {MAX_FILE_SIZE})"}
    except OSError as e:
        return {"error": f"cannot stat file: {e}"}

    try:
        content = Path(resolved).read_text()
    except OSError as e:
        return {"error": f"cannot read file: {e}"}

    if tab_type == "html":
        return {"html": content}

    if tab_type == "chart":
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return {"error": "invalid JSON in chart source"}
        chart_options = tab_config.get("chart_options", {})
        return {
            "chart_type": chart_options.get("chart_type", "line"),
            "title": chart_options.get("title", ""),
            "labels": data.get("labels", []),
            "datasets": data.get("datasets", []),
        }

    # type == "data" (default)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {"error": "invalid JSON in data source"}

    # Determine template: explicit override, or auto-detect from data shape
    template = tab_config.get("template")
    if not template:
        template = "table" if isinstance(data, list) else "key-value"

    return {"template": template, "data": data}

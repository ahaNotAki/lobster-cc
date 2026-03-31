"""Tests for remote_control.dashboard.tabs module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from remote_control.dashboard.tabs import (
    TABS_FILE,
    MAX_FILE_SIZE,
    load_tab_configs,
    load_tab_data,
)


# ===========================================================================
# 1. Config loading
# ===========================================================================


class TestLoadTabConfigs:

    def test_load_tab_configs_empty(self, tmp_path):
        """No file returns empty list."""
        result = load_tab_configs(str(tmp_path))
        assert result == []

    def test_load_tab_configs_valid(self, tmp_path):
        """Valid JSON returns tab list."""
        tabs = [
            {"id": "stocks", "label": "Stocks", "type": "data", "source": "stocks.json"},
            {"id": "page", "label": "Page", "type": "html", "source": "page.html"},
        ]
        (tmp_path / TABS_FILE).write_text(json.dumps(tabs))
        result = load_tab_configs(str(tmp_path))
        assert len(result) == 2
        assert result[0]["id"] == "stocks"
        assert result[1]["type"] == "html"

    def test_load_tab_configs_invalid_json(self, tmp_path):
        """Malformed JSON returns empty list."""
        (tmp_path / TABS_FILE).write_text("{not valid json!!!")
        result = load_tab_configs(str(tmp_path))
        assert result == []

    def test_load_tab_configs_missing_required_fields(self, tmp_path):
        """Tabs without id/label/type/source are skipped."""
        tabs = [
            {"id": "ok", "label": "OK", "type": "data", "source": "ok.json"},
            {"id": "no-label", "type": "data", "source": "x.json"},  # missing label
            {"label": "no-id", "type": "data", "source": "x.json"},  # missing id
            {"id": "no-type", "label": "NT", "source": "x.json"},   # missing type
            {"id": "no-src", "label": "NS", "type": "data"},        # missing source
        ]
        (tmp_path / TABS_FILE).write_text(json.dumps(tabs))
        result = load_tab_configs(str(tmp_path))
        assert len(result) == 1
        assert result[0]["id"] == "ok"


# ===========================================================================
# 2. Data loading
# ===========================================================================


class TestLoadTabData:

    def test_load_tab_data_json_table(self, tmp_path):
        """Reads JSON array, returns {template: 'table', data: [...]}."""
        data = [{"name": "AAPL", "price": 150}, {"name": "GOOG", "price": 2800}]
        (tmp_path / "stocks.json").write_text(json.dumps(data))
        tab = {"id": "s", "label": "S", "type": "data", "source": "stocks.json"}
        result = load_tab_data(str(tmp_path), tab)
        assert result["template"] == "table"
        assert result["data"] == data

    def test_load_tab_data_json_kv(self, tmp_path):
        """Reads JSON object, returns {template: 'key-value', data: {...}}."""
        data = {"status": "healthy", "uptime": "3d 2h"}
        (tmp_path / "info.json").write_text(json.dumps(data))
        tab = {"id": "i", "label": "I", "type": "data", "source": "info.json"}
        result = load_tab_data(str(tmp_path), tab)
        assert result["template"] == "key-value"
        assert result["data"] == data

    def test_load_tab_data_json_table_explicit_template(self, tmp_path):
        """Explicit template override is honored."""
        data = {"status": "healthy"}
        (tmp_path / "info.json").write_text(json.dumps(data))
        tab = {"id": "i", "label": "I", "type": "data", "source": "info.json",
               "template": "table"}
        result = load_tab_data(str(tmp_path), tab)
        assert result["template"] == "table"

    def test_load_tab_data_json_chart(self, tmp_path):
        """Reads chart JSON, returns {chart_type, labels, datasets}."""
        chart_data = {
            "labels": ["Mon", "Tue", "Wed"],
            "datasets": [
                {"label": "Price", "data": [100, 110, 105], "color": "#4ade80"}
            ],
        }
        (tmp_path / "chart.json").write_text(json.dumps(chart_data))
        tab = {"id": "c", "label": "C", "type": "chart", "source": "chart.json",
               "chart_options": {"chart_type": "line", "title": "Price"}}
        result = load_tab_data(str(tmp_path), tab)
        assert result["chart_type"] == "line"
        assert result["title"] == "Price"
        assert result["labels"] == ["Mon", "Tue", "Wed"]
        assert len(result["datasets"]) == 1

    def test_load_tab_data_html(self, tmp_path):
        """Reads HTML file, returns {html: '...'}."""
        html = "<h1>Hello</h1><p>World</p>"
        (tmp_path / "page.html").write_text(html)
        tab = {"id": "p", "label": "P", "type": "html", "source": "page.html"}
        result = load_tab_data(str(tmp_path), tab)
        assert result["html"] == html

    def test_load_tab_data_file_not_found(self, tmp_path):
        """Missing file returns error."""
        tab = {"id": "m", "label": "M", "type": "data", "source": "missing.json"}
        result = load_tab_data(str(tmp_path), tab)
        assert "error" in result

    def test_load_tab_data_path_traversal_blocked(self, tmp_path):
        """Source like '../../etc/passwd' is rejected."""
        tab = {"id": "x", "label": "X", "type": "html", "source": "../../etc/passwd"}
        result = load_tab_data(str(tmp_path), tab)
        assert "error" in result

    def test_load_tab_data_absolute_path_outside_working_dir_blocked(self, tmp_path):
        """/etc/passwd rejected."""
        tab = {"id": "x", "label": "X", "type": "html", "source": "/etc/passwd"}
        result = load_tab_data(str(tmp_path), tab)
        assert "error" in result

    def test_load_tab_data_file_too_large(self, tmp_path):
        """>1MB file returns error."""
        big_file = tmp_path / "big.json"
        big_file.write_text("[" + "0," * (MAX_FILE_SIZE // 2) + "0]")
        tab = {"id": "b", "label": "B", "type": "data", "source": "big.json"}
        result = load_tab_data(str(tmp_path), tab)
        assert "error" in result

    def test_load_tab_data_absolute_path_inside_working_dir_ok(self, tmp_path):
        """Absolute path within working_dir works."""
        data = [{"a": 1}]
        sub = tmp_path / "sub"
        sub.mkdir()
        data_file = sub / "data.json"
        data_file.write_text(json.dumps(data))
        tab = {"id": "a", "label": "A", "type": "data", "source": str(data_file)}
        result = load_tab_data(str(tmp_path), tab)
        assert result["template"] == "table"
        assert result["data"] == data


# ===========================================================================
# 3. Integration with status
# ===========================================================================


class TestStatusIncludesTabs:

    def test_status_includes_tabs(self, tmp_path):
        """get_agent_status returns 'tabs' key."""
        from remote_control.dashboard.status import get_agent_status

        # Reset cache
        import remote_control.dashboard.status as mod
        mod._config_cache = {}

        tabs = [
            {"id": "test", "label": "Test", "type": "data", "source": "t.json"},
        ]
        (tmp_path / TABS_FILE).write_text(json.dumps(tabs))

        store = MagicMock(spec=["get_running_task", "get_latest_task_any_user",
                                "list_tasks_all_users", "list_all_cron_jobs"])
        store.get_running_task.return_value = None
        store.get_latest_task_any_user.return_value = None
        store.list_tasks_all_users.return_value = []

        result = get_agent_status(store, None, working_dir=str(tmp_path))
        assert "tabs" in result
        assert len(result["tabs"]) == 1
        assert result["tabs"][0]["id"] == "test"

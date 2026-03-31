"""Tests for remote_control.dashboard.status module."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock


from remote_control.core.models import Task, TaskStatus
from remote_control.dashboard.status import (
    _DEFAULT_LOBSTER,
    _DEFAULT_WORKSTATIONS,
    WORKSTATION_FILE,
    _clean_message,
    _state_label,
    classify_task_state,
    get_agent_status,
    load_dashboard_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_cache():
    """Reset the module-level config cache before tests that use it."""
    import remote_control.dashboard.status as mod
    mod._config_cache = {}


SAMPLE_WORKSTATIONS = [
    {"id": "coding", "label": "CODE", "icon": "C", "keywords": ["code", "fix"]},
    {"id": "stock", "label": "STOCK", "icon": "S", "keywords": ["stock"]},
    {"id": "general", "label": "WORK", "icon": "G", "keywords": []},
]


# ===========================================================================
# 1. load_dashboard_config
# ===========================================================================


class TestLoadDashboardConfig:
    """Tests for load_dashboard_config."""

    def setup_method(self):
        _reset_cache()

    def test_new_format_object(self, tmp_path):
        """New format: JSON object with lobster and workstations keys."""
        custom_lobster = {"name": "crabby", "emoji": "X"}
        config = {"lobster": custom_lobster, "workstations": SAMPLE_WORKSTATIONS}
        (tmp_path / WORKSTATION_FILE).write_text(json.dumps(config))

        lobster, ws = load_dashboard_config(str(tmp_path))
        assert lobster == custom_lobster
        assert ws == SAMPLE_WORKSTATIONS

    def test_new_format_object_no_lobster_key(self, tmp_path):
        """Object format without lobster key falls back to default lobster."""
        config = {"workstations": SAMPLE_WORKSTATIONS}
        (tmp_path / WORKSTATION_FILE).write_text(json.dumps(config))

        lobster, ws = load_dashboard_config(str(tmp_path))
        assert lobster == _DEFAULT_LOBSTER
        assert ws == SAMPLE_WORKSTATIONS

    def test_old_format_list(self, tmp_path):
        """Old format: JSON is a bare list of workstations."""
        (tmp_path / WORKSTATION_FILE).write_text(json.dumps(SAMPLE_WORKSTATIONS))

        lobster, ws = load_dashboard_config(str(tmp_path))
        assert lobster == _DEFAULT_LOBSTER
        assert ws == SAMPLE_WORKSTATIONS

    def test_missing_file_returns_defaults(self, tmp_path):
        """When file doesn't exist, return defaults."""
        lobster, ws = load_dashboard_config(str(tmp_path))
        assert lobster == _DEFAULT_LOBSTER
        assert ws == _DEFAULT_WORKSTATIONS

    def test_invalid_json_returns_defaults(self, tmp_path):
        """Invalid JSON falls back to defaults."""
        (tmp_path / WORKSTATION_FILE).write_text("{not valid json!!!")

        lobster, ws = load_dashboard_config(str(tmp_path))
        assert lobster == _DEFAULT_LOBSTER
        assert ws == _DEFAULT_WORKSTATIONS

    def test_empty_list_returns_defaults(self, tmp_path):
        """Empty list falls back to defaults (isinstance list but not truthy)."""
        (tmp_path / WORKSTATION_FILE).write_text("[]")

        lobster, ws = load_dashboard_config(str(tmp_path))
        assert lobster == _DEFAULT_LOBSTER
        assert ws == _DEFAULT_WORKSTATIONS

    def test_object_with_empty_workstations_returns_defaults(self, tmp_path):
        """Object with empty workstations list falls back to defaults."""
        config = {"lobster": {"name": "X"}, "workstations": []}
        (tmp_path / WORKSTATION_FILE).write_text(json.dumps(config))

        lobster, ws = load_dashboard_config(str(tmp_path))
        assert lobster == _DEFAULT_LOBSTER
        assert ws == _DEFAULT_WORKSTATIONS

    def test_caching_by_mtime(self, tmp_path):
        """Config is cached; same mtime returns cached result without re-reading."""
        config = {"workstations": SAMPLE_WORKSTATIONS}
        path = tmp_path / WORKSTATION_FILE
        path.write_text(json.dumps(config))

        # First load populates cache
        lobster1, ws1 = load_dashboard_config(str(tmp_path))
        assert ws1 == SAMPLE_WORKSTATIONS

        # Overwrite file content but keep same mtime
        new_ws = [{"id": "x", "label": "X", "icon": "X", "keywords": ["x"]}]
        new_config = {"workstations": new_ws}
        mtime_before = path.stat().st_mtime
        path.write_text(json.dumps(new_config))
        # Force same mtime
        import os
        os.utime(str(path), (mtime_before, mtime_before))

        # Should return cached value (SAMPLE_WORKSTATIONS), not new_ws
        _, ws2 = load_dashboard_config(str(tmp_path))
        assert ws2 == SAMPLE_WORKSTATIONS

    def test_cache_invalidated_on_mtime_change(self, tmp_path):
        """When mtime changes, config is re-read."""
        config = {"workstations": SAMPLE_WORKSTATIONS}
        path = tmp_path / WORKSTATION_FILE
        path.write_text(json.dumps(config))

        load_dashboard_config(str(tmp_path))

        # Write new content with a different mtime
        new_ws = [{"id": "alpha", "label": "A", "icon": "A", "keywords": ["alpha"]}]
        time.sleep(0.05)  # ensure mtime changes
        path.write_text(json.dumps({"workstations": new_ws}))

        _, ws2 = load_dashboard_config(str(tmp_path))
        assert ws2 == new_ws


# ===========================================================================
# 2. classify_task_state
# ===========================================================================


class TestClassifyTaskState:

    def test_keyword_match(self):
        """Matches first workstation whose keyword appears in the message."""
        result = classify_task_state("please fix the code", SAMPLE_WORKSTATIONS)
        assert result == "coding"

    def test_case_insensitive(self):
        """Matching is case-insensitive."""
        result = classify_task_state("CHECK STOCK prices", SAMPLE_WORKSTATIONS)
        assert result == "stock"

    def test_first_match_priority(self):
        """When multiple workstations could match, the first one wins."""
        ws = [
            {"id": "a", "keywords": ["hello"]},
            {"id": "b", "keywords": ["hello", "world"]},
        ]
        assert classify_task_state("hello world", ws) == "a"

    def test_no_match_returns_general(self):
        """When no keyword matches, return 'general'."""
        result = classify_task_state("random unrelated text", SAMPLE_WORKSTATIONS)
        assert result == "general"

    def test_empty_message(self):
        """Empty message matches no keywords -> general."""
        assert classify_task_state("", SAMPLE_WORKSTATIONS) == "general"

    def test_empty_workstations(self):
        """No workstations at all -> general."""
        assert classify_task_state("code stuff", []) == "general"


# ===========================================================================
# 3. _state_label
# ===========================================================================


class TestStateLabel:

    def test_idle(self):
        assert _state_label("idle", SAMPLE_WORKSTATIONS) == "空闲"

    def test_error(self):
        assert _state_label("error", SAMPLE_WORKSTATIONS) == "出错了"

    def test_done(self):
        assert _state_label("done", SAMPLE_WORKSTATIONS) == "搞定了"

    def test_workstation_lookup(self):
        """Known workstation ID returns its label."""
        assert _state_label("coding", SAMPLE_WORKSTATIONS) == "CODE"
        assert _state_label("stock", SAMPLE_WORKSTATIONS) == "STOCK"

    def test_fallback_unknown_state(self):
        """Unknown state that's not special and not a workstation returns itself."""
        assert _state_label("unknown_xyz", SAMPLE_WORKSTATIONS) == "unknown_xyz"

    def test_workstation_without_label_key(self):
        """Workstation dict missing 'label' key falls back to its id."""
        ws = [{"id": "myws", "keywords": []}]
        assert _state_label("myws", ws) == "myws"


# ===========================================================================
# 4. _clean_message
# ===========================================================================


class TestCleanMessage:

    def test_strip_system_prefix(self):
        msg = "[System: some info]\n\nactual message"
        assert _clean_message(msg) == "actual message"

    def test_strip_context_block(self):
        msg = "<context>some context here</context>  real content"
        assert _clean_message(msg) == "real content"

    def test_strip_both(self):
        msg = "[System: info]\n\n<context>ctx data</context>\nhello"
        assert _clean_message(msg) == "hello"

    def test_no_stripping_needed(self):
        msg = "just a normal message"
        assert _clean_message(msg) == "just a normal message"

    def test_empty_string(self):
        assert _clean_message("") == ""

    def test_system_prefix_no_closing(self):
        """System prefix without proper closing is not stripped."""
        msg = "[System: no end"
        assert _clean_message(msg) == "[System: no end"

    def test_context_no_closing(self):
        """Context tag without closing is not stripped."""
        msg = "<context>no end tag"
        assert _clean_message(msg) == "<context>no end tag"

    def test_whitespace_after_context(self):
        """Leading whitespace after context block is stripped."""
        msg = "<context>data</context>   \n  result"
        assert _clean_message(msg) == "result"


# ===========================================================================
# 5. get_agent_status
# ===========================================================================


def _make_store(running_task=None, latest_task=None, recent_tasks=None):
    """Create a mock Store."""
    store = MagicMock(spec=["get_running_task", "get_latest_task_any_user",
                            "list_tasks_all_users", "list_all_cron_jobs"])
    store.get_running_task.return_value = running_task
    store.get_latest_task_any_user.return_value = latest_task
    # list_tasks_all_users returns dicts, not Task objects
    task_dicts = []
    for t in (recent_tasks or []):
        task_dicts.append({
            "id": t.id, "user_id": t.user_id, "status": t.status.value,
            "message": t.message, "created_at": t.created_at,
            "started_at": t.started_at, "finished_at": t.finished_at,
            "agent_id": "",
        })
    store.list_tasks_all_users.return_value = task_dicts
    return store


def _make_runner(is_running=False, pid=None, model_info=None):
    """Create a mock runner."""
    runner = MagicMock()
    runner.is_running = is_running
    runner.model_info = model_info or {}
    if pid:
        runner._process = MagicMock()
        runner._process.pid = pid
    else:
        runner._process = None
    return runner


class TestGetAgentStatus:

    def setup_method(self):
        _reset_cache()

    def test_idle_state(self, tmp_path):
        """No running task and no recent completed -> idle."""
        store = _make_store()
        runner = _make_runner()

        result = get_agent_status(store, runner, working_dir=str(tmp_path))

        assert result["agent"]["state"] == "idle"
        assert result["agent"]["state_label"] == "空闲"
        assert result["agent"]["task_id"] is None
        assert result["agent"]["task_message"] == ""
        assert result["agent"]["elapsed_seconds"] == 0

    def test_running_task(self, tmp_path):
        """Running task populates agent info with correct state."""
        now = datetime.now(timezone.utc)
        task = Task(
            id="t1",
            user_id="u1",
            message="fix the code please",
            status=TaskStatus.RUNNING,
            started_at=(now - timedelta(seconds=30)).isoformat(),
        )
        store = _make_store(running_task=task)
        runner = _make_runner()

        result = get_agent_status(store, runner, working_dir=str(tmp_path))

        assert result["agent"]["state"] == "coding"
        assert result["agent"]["task_id"] == "t1"
        assert result["agent"]["elapsed_seconds"] >= 29  # allow small timing diff
        assert "fix the code please" in result["agent"]["task_message"]

    def test_running_task_with_streaming(self, tmp_path):
        """Streaming ref buffer and thinking are passed through."""
        task = Task(
            id="t2",
            message="check stock",
            status=TaskStatus.RUNNING,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        store = _make_store(running_task=task)
        runner = _make_runner()
        streaming = {"buffer": "partial output", "thinking": "reasoning..."}

        result = get_agent_status(store, runner, streaming_ref=streaming,
                                  working_dir=str(tmp_path))

        assert result["agent"]["streaming_output"] == "partial output"
        assert result["agent"]["thinking"] == "reasoning..."

    def test_done_flash_within_10s(self, tmp_path):
        """Recently completed task (<10s) triggers 'done' flash state."""
        now = datetime.now(timezone.utc)
        completed_task = Task(
            id="t3",
            message="deploy the app",
            status=TaskStatus.COMPLETED,
            finished_at=(now - timedelta(seconds=3)).isoformat(),
        )
        store = _make_store(latest_task=completed_task)
        runner = _make_runner()

        result = get_agent_status(store, runner, working_dir=str(tmp_path))

        assert result["agent"]["state"] == "done"
        assert result["agent"]["state_label"] == "搞定了"
        assert "deploy the app" in result["agent"]["task_message"]

    def test_no_done_flash_after_10s(self, tmp_path):
        """Completed task older than 10s does NOT trigger done flash."""
        now = datetime.now(timezone.utc)
        completed_task = Task(
            id="t4",
            message="old task",
            status=TaskStatus.COMPLETED,
            finished_at=(now - timedelta(seconds=30)).isoformat(),
        )
        store = _make_store(latest_task=completed_task)
        runner = _make_runner()

        result = get_agent_status(store, runner, working_dir=str(tmp_path))

        assert result["agent"]["state"] == "idle"

    def test_no_done_flash_if_running(self, tmp_path):
        """Done flash doesn't fire when a task is currently running."""
        now = datetime.now(timezone.utc)
        running_task = Task(
            id="t5",
            message="code stuff",
            status=TaskStatus.RUNNING,
            started_at=now.isoformat(),
        )
        completed_task = Task(
            id="t6",
            message="old completed",
            status=TaskStatus.COMPLETED,
            finished_at=(now - timedelta(seconds=2)).isoformat(),
        )
        store = _make_store(running_task=running_task, latest_task=completed_task)
        runner = _make_runner()

        result = get_agent_status(store, runner, working_dir=str(tmp_path))

        # Should be the running task state, not "done"
        assert result["agent"]["state"] != "done"
        assert result["agent"]["task_id"] == "t5"

    def test_model_info(self, tmp_path):
        """Model info is extracted from runner."""
        store = _make_store()
        runner = _make_runner(model_info={
            "model": "claude-opus-4-6",
            "claude_code_version": "2.1.79",
            "context_window": 200000,
            "max_output_tokens": 65536,
            "input_tokens": 1000,
            "output_tokens": 500,
        })

        result = get_agent_status(store, runner, working_dir=str(tmp_path))

        assert result["model"]["model"] == "claude-opus-4-6"
        assert result["model"]["context_window"] == 200000
        assert result["model"]["input_tokens"] == 1000

    def test_model_info_none_runner(self, tmp_path):
        """When runner is None, model info is empty dict."""
        store = _make_store()
        result = get_agent_status(store, None, working_dir=str(tmp_path))
        assert result["model"] == {}

    def test_process_info_no_runner(self, tmp_path):
        """Process info shows None pid when runner has no process."""
        store = _make_store()
        runner = _make_runner()

        result = get_agent_status(store, runner, working_dir=str(tmp_path))

        assert result["process"]["pid"] is None
        assert result["process"]["rss_mb"] is None

    def test_process_info_with_pid(self, tmp_path):
        """Process info includes pid when runner has a running process."""
        store = _make_store()
        runner = _make_runner(is_running=True, pid=12345)

        result = get_agent_status(store, runner, working_dir=str(tmp_path))

        assert result["process"]["pid"] == 12345

    def test_recent_tasks(self, tmp_path):
        """Recent tasks are listed with correct fields."""
        now = datetime.now(timezone.utc)
        tasks = [
            Task(
                id="r1",
                message="task one",
                status=TaskStatus.COMPLETED,
                created_at=now.isoformat(),
                started_at=now.isoformat(),
                finished_at=(now + timedelta(seconds=45)).isoformat(),
            ),
            Task(
                id="r2",
                message="task two",
                status=TaskStatus.FAILED,
                created_at=now.isoformat(),
            ),
        ]
        store = _make_store(recent_tasks=tasks)
        runner = _make_runner()

        result = get_agent_status(store, runner, working_dir=str(tmp_path))

        assert len(result["recent_tasks"]) == 2
        assert result["recent_tasks"][0]["id"] == "r1"
        assert result["recent_tasks"][0]["status"] == "completed"
        assert result["recent_tasks"][0]["duration"] == 45
        assert result["recent_tasks"][1]["id"] == "r2"
        assert result["recent_tasks"][1]["duration"] is None

    def test_workstations_in_output(self, tmp_path):
        """Workstations list is included in the output."""
        store = _make_store()
        runner = _make_runner()

        result = get_agent_status(store, runner, working_dir=str(tmp_path))

        assert "workstations" in result
        assert len(result["workstations"]) == len(_DEFAULT_WORKSTATIONS)
        # Each entry should have id, label, icon
        for ws in result["workstations"]:
            assert "id" in ws
            assert "label" in ws
            assert "icon" in ws

    def test_lobster_in_output(self, tmp_path):
        """Lobster config is included in the output."""
        store = _make_store()
        runner = _make_runner()

        result = get_agent_status(store, runner, working_dir=str(tmp_path))

        assert result["lobster"] == _DEFAULT_LOBSTER

    def test_timestamp_in_output(self, tmp_path):
        """Output includes a timestamp."""
        store = _make_store()
        runner = _make_runner()

        result = get_agent_status(store, runner, working_dir=str(tmp_path))

        assert "timestamp" in result
        # Should be parseable as ISO datetime
        datetime.fromisoformat(result["timestamp"])

    def test_recent_tasks_message_length(self, tmp_path):
        """Recent task messages are truncated to 200 chars (not 100)."""
        long_msg = "A" * 300
        tasks = [
            Task(
                id="long1",
                message=long_msg,
                status=TaskStatus.COMPLETED,
                created_at=datetime.now(timezone.utc).isoformat(),
            ),
        ]
        store = _make_store(recent_tasks=tasks)
        runner = _make_runner()

        result = get_agent_status(store, runner, working_dir=str(tmp_path))

        msg = result["recent_tasks"][0]["message"]
        assert len(msg) == 200
        assert msg == "A" * 200

    def test_running_task_cleans_message(self, tmp_path):
        """Running task message is cleaned of system prefixes."""
        task = Task(
            id="tc",
            message="[System: hint]\n\nreal instruction",
            status=TaskStatus.RUNNING,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        store = _make_store(running_task=task)
        runner = _make_runner()

        result = get_agent_status(store, runner, working_dir=str(tmp_path))

        assert result["agent"]["task_message"] == "real instruction"

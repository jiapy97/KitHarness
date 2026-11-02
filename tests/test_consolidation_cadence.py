"""Curator cadence follows project tool activity, not Stop events or session resets."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from autoharness import config
from autoharness.hook import dispatch
from autoharness.lib import counters, layer


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.delenv(config.CHILD_SESSION_ENV, raising=False)
    monkeypatch.setattr(config, "CONSOLIDATE_EVERY_N", 3)
    monkeypatch.setattr(config, "REFLECT_EVERY_N", 2)
    return {"project": tmp_path / "p", "global": tmp_path / "g"}


def tools(roots, count, sid="s1"):
    for _ in range(count):
        dispatch.dispatch({"hook_event_name": "PreToolUse", "session_id": sid, "tool_name": "Bash"}, roots=roots)


def stop(roots, calls, sid="s1", reflect=None):
    return dispatch.dispatch({"hook_event_name": "Stop", "session_id": sid, "transcript_path": "/fake"}, roots=roots,
                             reflect=reflect or (lambda *a: None), consolidate=lambda rid, r: calls.append(rid))["result"]


def test_chat_only_turns_never_launch_curator_but_still_age_lifecycle(roots):
    calls = []
    for _ in range(6):
        assert not stop(roots, calls)["consolidation"]["triggered"]
    assert calls == []
    assert counters.activity_count(roots["project"]) == 0
    assert counters.request_count("project", roots["project"]) == 6
    assert counters.request_count("global", roots["global"]) == 6


def test_heavy_turn_crossing_several_periods_launches_once_and_keeps_remainder(roots):
    calls = []
    tools(roots, 8)
    result = stop(roots, calls)
    assert result["consolidation"]["tool_calls"] == 8
    assert result["consolidation"]["watermark"] == 6
    assert len(calls) == 1
    stop(roots, calls)
    assert len(calls) == 1
    tools(roots, 1)
    assert stop(roots, calls)["consolidation"]["watermark"] == 9
    assert len(calls) == 2


def test_tools_do_not_launch_until_stop_and_repeated_stop_does_not_duplicate(roots):
    calls = []
    tools(roots, 3)
    assert counters.activity_count(roots["project"]) == 3
    assert not (layer.state_dir("project", roots["project"]) / "curator-watermark").exists()
    stop(roots, calls)
    stop(roots, calls)
    assert len(calls) == 1


def test_reflection_reset_and_session_end_do_not_erase_project_activity(roots):
    calls = []
    tools(roots, 2, "first")
    assert stop(roots, calls, "first")["triggered"]
    assert counters.session_count("first", roots["project"]) == 0
    dispatch.dispatch({"hook_event_name": "SessionEnd", "session_id": "first"}, roots=roots, reflect=lambda *a: None)
    tools(roots, 1, "second")
    assert stop(roots, calls, "second")["consolidation"]["triggered"]
    assert len(calls) == 1 and calls[0].startswith("second-c3-")


def test_consolidation_has_priority_and_preserves_due_reflection_for_next_stop(roots):
    calls, reflected = [], []
    tools(roots, 3)
    result = stop(roots, calls, reflect=lambda *a: reflected.append(a))
    assert result["reason"] == "consolidation_priority"
    assert not result["triggered"] and result["consolidation"]["triggered"]
    assert reflected == [] and len(calls) == 1
    assert counters.session_count("s1", roots["project"]) == 3
    assert stop(roots, calls, reflect=lambda *a: reflected.append(a))["triggered"]
    assert len(reflected) == 1 and len(calls) == 1


def test_deferred_reflection_is_still_flushed_at_session_end(roots):
    tools(roots, 3)
    stop(roots, [])
    calls = []
    result = dispatch.dispatch({"hook_event_name": "SessionEnd", "session_id": "s1", "transcript_path": "/fake"},
                               roots=roots, reflect=lambda *a: calls.append(a))
    assert result["result"]["triggered"] and len(calls) == 1
    assert counters.activity_count(roots["project"]) == 3


@pytest.mark.parametrize("sid", [None, "../unsafe", "", 23])
def test_invalid_session_tool_events_do_not_advance_curator(roots, sid):
    tools(roots, 3, sid)
    assert counters.activity_count(roots["project"]) == 0
    calls = []
    stop(roots, calls)
    assert calls == []


def test_child_tool_calls_and_stop_do_not_advance_either_counter_or_curator(roots, monkeypatch):
    monkeypatch.setenv(config.CHILD_SESSION_ENV, "1")
    for name in ("Read", "Grep", "Bash"):
        dispatch.dispatch({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": name}, roots=roots)
    calls = []
    stop(roots, calls)
    assert counters.activity_count(roots["project"]) == 0
    assert counters.session_count("s1", roots["project"]) == 0
    assert calls == []


def test_project_cadence_is_independent_with_global_layer_disabled(roots, monkeypatch):
    monkeypatch.setattr(config, "DISABLE_GLOBAL", True)
    tools(roots, 3)
    other = {**roots, "project": roots["project"].parent / "other"}
    calls = []
    stop(other, calls)
    assert calls == []
    stop(roots, calls)
    assert len(calls) == 1 and not roots["global"].exists()


@pytest.mark.parametrize("returned_error", [False, True])
def test_failed_launch_preserves_watermark_and_can_retry_with_unique_id(roots, returned_error):
    tools(roots, 3)
    attempts = []

    def launch(rid, r):
        attempts.append(rid)
        if returned_error:
            return {"error": "launch failed"}
        raise OSError("launch failed")

    event = {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": "/fake"}
    result = dispatch.dispatch(event, roots=roots, reflect=lambda *a: None, consolidate=launch)
    assert "error" in result["result"]["consolidation"]
    calls = []
    assert stop(roots, calls)["consolidation"]["triggered"]
    assert len(calls) == 1 and calls[0] != attempts[0]


def test_zero_disables_curator_without_discarding_activity(roots, monkeypatch):
    monkeypatch.setattr(config, "CONSOLIDATE_EVERY_N", 0)
    calls = []
    tools(roots, 6)
    stop(roots, calls)
    assert calls == []
    assert not (layer.state_dir("project", roots["project"]) / "curator-watermark").exists()
    monkeypatch.setattr(config, "CONSOLIDATE_EVERY_N", 3)
    assert stop(roots, calls)["consolidation"]["watermark"] == 6
    assert len(calls) == 1


def test_watermark_write_failure_does_not_launch_reflection_beside_started_curator(roots, monkeypatch):
    tools(roots, 3)
    original = counters.atomic.write_text

    def denied(path, text):
        if path.name == "curator-watermark":
            raise OSError("progress disk unavailable")
        return original(path, text)

    monkeypatch.setattr(counters.atomic, "write_text", denied)
    curated, reflected = [], []
    result = stop(roots, curated, reflect=lambda *a: reflected.append(a))
    assert result["consolidation"]["triggered"]
    assert "error" in result["consolidation"]
    assert len(curated) == 1 and reflected == []
    assert counters.session_count("s1", roots["project"]) == 3


def test_old_request_count_is_not_misinterpreted_as_tool_activity(roots):
    for _ in range(9):
        counters.bump_request("project", roots["project"])
    calls = []
    stop(roots, calls)
    assert calls == []
    tools(roots, 3)
    assert stop(roots, calls)["consolidation"]["triggered"]


def test_launch_error_diagnostics_redact_credentials(roots):
    tools(roots, 3)
    secret = "ghp_" + "a" * 36

    def denied(*a):
        raise OSError("token=" + secret)

    result = dispatch.dispatch({"hook_event_name": "Stop", "session_id": "s1"}, roots=roots,
                               reflect=lambda *a: None, consolidate=denied)
    assert secret not in json.dumps(result)
    assert "REDACTED" in result["result"]["consolidation"]["error"]


@pytest.mark.parametrize(("raw", "expected"), [("0", "0"), ("-1", "250"), ("bad", "250")])
def test_consolidation_config_accepts_zero_and_rejects_invalid_values(raw, expected):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
           "AUTOHARNESS_CONSOLIDATE_EVERY_N": raw}
    result = subprocess.run([sys.executable, "-c", "from autoharness import config; print(config.CONSOLIDATE_EVERY_N)"],
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=5)
    assert result.returncode == 0
    assert result.stdout.strip() == expected
    if raw != "0":
        assert "using default 250" in result.stderr


def test_simultaneous_stop_processes_launch_one_curator(roots, tmp_path):
    tools(roots, 3)
    log = tmp_path / "launches"
    script = '''
import json, sys, time
from pathlib import Path
from autoharness.hook import dispatch
roots = {k: Path(v) for k, v in json.loads(sys.argv[1]).items()}
def launch(rid, roots):
    with Path(sys.argv[2]).open('a', encoding='utf-8') as handle:
        handle.write(rid + '\\n')
    time.sleep(.03)
result = dispatch.dispatch({'hook_event_name':'Stop', 'session_id':'s1'}, roots=roots,
                           reflect=lambda *a: None, consolidate=launch)
assert 'error' not in result, result
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
           "AUTOHARNESS_CONSOLIDATE_EVERY_N": "3", "AUTOHARNESS_DISABLE_GLOBAL": "0"}
    env.pop(config.CHILD_SESSION_ENV, None)
    argv = [sys.executable, "-c", script, json.dumps({k: str(v) for k, v in roots.items()}), str(log)]
    processes = [subprocess.Popen(argv, env=env) for _ in range(4)]
    try:
        assert [p.wait(timeout=10) for p in processes] == [0] * 4
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert len(log.read_text().splitlines()) == 1


def test_activity_increments_are_not_lost_between_processes(roots):
    script = '''
import sys
from pathlib import Path
from autoharness.lib import counters
for _ in range(20):
    counters.bump_activity(Path(sys.argv[1]))
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    processes = [subprocess.Popen([sys.executable, "-c", script, str(roots["project"])], env=env) for _ in range(4)]
    try:
        assert [p.wait(timeout=10) for p in processes] == [0] * 4
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert counters.activity_count(roots["project"]) == 80

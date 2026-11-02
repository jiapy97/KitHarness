"""Background limits use real local processes, never a model or a personal skill tree."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kitharness import config
from kitharness.hook import jobs, promoter, recovery, spawn
from kitharness.lib import counters, intent_queue, layer, run_owners

SRC = str(Path(__file__).resolve().parents[1] / "src")
BODY = "---\nname: dates\ndescription: Use when formatting dates.\n---\nUse ISO dates.\n"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOB_PROJECT_LIMIT", 1)
    monkeypatch.setattr(config, "JOB_GLOBAL_LIMIT", 1)
    monkeypatch.setattr(config, "DISABLE_GLOBAL", False)
    return {"project": tmp_path / "p", "global": tmp_path / "g"}


def account(roots, run_id):
    return json.loads((layer.state_dir("project", roots["project"]) / "runs" / f"{run_id}.json").read_text())


@pytest.mark.parametrize("carrier", ["bundle", "fork"])
def test_both_carriers_pass_budget_and_request_machine_readable_usage(carrier):
    argv = (spawn.build_command(agent="reflector", claude_bin="claude") if carrier == "bundle" else
            spawn.build_fork_command(session_id="s", claude_bin="claude"))
    assert argv[argv.index("--max-budget-usd") + 1] == str(config.JOB_MAX_BUDGET_USD)
    assert argv[argv.index("--output-format") + 1] == "json"


_SLOTS = '''
import json, sys
from pathlib import Path
from kitharness.hook import jobs
roots = {key: Path(value) for key, value in json.loads(sys.argv[1]).items()}
with jobs.slots(roots) as acquired:
    print(acquired, flush=True)
    sys.stdin.readline()
'''


@pytest.mark.parametrize("shared", ["project", "global"])
def test_slots_exclude_other_process_and_release_on_process_exit(roots, tmp_path, shared):
    env = {**os.environ, "PYTHONPATH": SRC, "KITHARNESS_JOB_PROJECT_LIMIT": "1",
           "KITHARNESS_JOB_GLOBAL_LIMIT": "1", "KITHARNESS_DISABLE_GLOBAL": "0"}
    args = json.dumps({key: str(value) for key, value in roots.items()})
    with subprocess.Popen([sys.executable, "-c", _SLOTS, args], env=env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True, encoding="utf-8") as child:
        try:
            assert child.stdout.readline().strip() == "True"
            other = {key: value if key == shared else tmp_path / f"other-{key}" for key, value in roots.items()}
            with jobs.slots(other) as acquired:
                assert not acquired
        finally:
            child.kill()
            child.communicate(timeout=5)
    with jobs.slots(other) as acquired:
        assert acquired


def test_full_global_slot_releases_project_slot_and_disabled_global_stays_untouched(roots, monkeypatch):
    other = {**roots, "project": roots["project"].parent / "other"}
    with jobs.slots(roots) as first:
        assert first
        with jobs.slots(other) as second:
            assert not second
        monkeypatch.setattr(config, "DISABLE_GLOBAL", True)
        with jobs.slots(other) as independent:
            assert independent
    new_roots = {"project": roots["project"].parent / "new", "global": roots["global"].parent / "untouched"}
    with jobs.slots(new_roots) as acquired:
        assert acquired
    assert not new_roots["global"].exists()


@pytest.mark.parametrize("curator", [False, True])
def test_saturation_skips_child_and_curator_snapshot_but_records_reason(roots, monkeypatch, curator):
    def forbidden(*a, **k):
        pytest.fail("a saturated run must not spawn or snapshot")
    monkeypatch.setattr(spawn, "_snapshot_skills", forbidden)
    with jobs.slots(roots):
        with pytest.raises(jobs.Deferred, match="concurrency"):
            if curator:
                spawn.run_curator("busy", roots=roots, spawn_fn=forbidden)
            else:
                spawn.run("window", "busy", roots=roots, spawn_fn=forbidden)
    assert account(roots, "busy")["job"]["status"] == "concurrency_limit"


@pytest.mark.skipif(os.name != "posix", reason="process group termination is POSIX")
def test_real_timeout_terminates_child_and_grandchild(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOB_TIMEOUT_S", .4)
    marker = tmp_path / "survived"
    script = ("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
              "\"import time,pathlib; time.sleep(1); pathlib.Path(\"+repr(sys.argv[1])+\").touch()\"]); time.sleep(30)")
    result = jobs.execute([sys.executable, "-c", script, str(marker)], dict(os.environ), "input")
    assert result.timed_out
    assert result.returncode != 0
    assert result.elapsed_ms < 5000
    time.sleep(1.1)
    assert not marker.exists()


@pytest.mark.parametrize("reason", ["timed_out", "budget_exhausted", "child_failed"])
def test_partial_proposals_are_quarantined_and_offset_is_retained(roots, monkeypatch, reason):
    def partial(argv, env, bundle):
        intent_queue.append("limited", {"action": "create", "name": "dates", "body": BODY,
                                        "level": "project", "reason": "learned", "evidence": "dates"}, roots["project"])
        response = {"subtype": "error_max_budget_usd"} if reason == "budget_exhausted" else {}
        result = subprocess.CompletedProcess(argv, 1, json.dumps(response), "")
        result.timed_out = reason == "timed_out"
        return result
    monkeypatch.setattr(spawn, "_detached_spawn", partial)
    monkeypatch.setattr(spawn.capture, "window", lambda *a: ("window", 42))
    monkeypatch.setattr(spawn.capture, "digest", lambda *a: "")
    counters.write_session_offset("session", 7, roots["project"])
    spawn.main(["transcript", "session", "limited", str(roots["project"]), str(roots["global"])])
    assert counters.session_offset("session", roots["project"]) == 7
    assert account(roots, "limited")["job"]["status"] == reason
    assert intent_queue.pending("limited", roots["project"])
    assert run_owners.cancelled("limited", roots["project"])
    assert recovery.recover(roots=roots) == []
    assert promoter.drain("other", roots=roots) == []
    assert promoter.drain("limited", roots=roots) == []
    assert not layer.symbol_dir("project", "dates", roots["project"]).exists()
    with pytest.raises(OSError):
        intent_queue.append("limited", {"action": "create"}, roots["project"])


def test_usage_account_contains_only_numeric_allowlisted_fields(roots):
    response = {"usage": {"input_tokens": 42, "output_tokens": 8, "cache_read_input_tokens": 6,
                          "cache_creation_input_tokens": 3, "private": "secret"},
                "total_cost_usd": .01, "duration_ms": 12, "num_turns": 2, "result": "secret prompt"}
    proc = subprocess.CompletedProcess([], 0, json.dumps(response), "")
    spawn.run("private window", "ok", roots=roots, spawn_fn=lambda *a: proc)
    value = account(roots, "ok")
    assert value["job"]["usage"]["input_tokens"] == 42
    assert value["job"]["usage"]["total_cost_usd"] == .01
    assert "secret" not in json.dumps(value)
    assert "private window" not in json.dumps(value)


@pytest.mark.parametrize("raw", ["", "broken", "[]", '{"usage": {"input_tokens": true}, "total_cost_usd": NaN}',
                                 '{"usage": "invalid", "total_cost_usd": -1}',
                                 '{"total_cost_usd": ' + "9" * 400 + '}', "x" * (jobs.MAX_RESPONSE_BYTES + 1),
                                 "[" * 2000 + "0" + "]" * 2000])
def test_unavailable_usage_is_not_fabricated(raw):
    assert jobs.usage(subprocess.CompletedProcess([], 0, raw, "")) == {"available": False}


def test_launch_failure_is_recorded_instead_of_disappearing(roots):
    with pytest.raises(jobs.Deferred, match="launch_failed"):
        spawn.run("window", "missing", roots=roots, claude_bin=str(roots["project"] / "missing-binary"))
    assert account(roots, "missing")["spawn_error"]["returncode"] == 127


def test_landing_retry_preserves_usage_when_child_already_finished(roots, monkeypatch):
    original = promoter.drain

    def failed(*a, **k):
        raise OSError("account temporarily unavailable")

    def stage(argv, env, bundle):
        intent_queue.append("retry", {"action": "create", "name": "dates", "body": BODY,
                                      "level": "project", "reason": "learned", "evidence": "dates"}, roots["project"])
        return subprocess.CompletedProcess(argv, 0, '{"usage":{"input_tokens":42}}', "")

    monkeypatch.setattr(promoter, "drain", failed)
    with pytest.raises(OSError):
        spawn.run("window", "retry", roots=roots, spawn_fn=stage)
    assert account(roots, "retry")["job"]["usage"]["input_tokens"] == 42
    monkeypatch.setattr(promoter, "drain", original)
    assert promoter.drain("retry", roots=roots)[0]["ok"]
    assert account(roots, "retry")["job"]["usage"]["input_tokens"] == 42
    assert account(roots, "retry")["verdicts"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX groups and descriptor inheritance")
def test_real_timeout_quarantines_registered_writer_before_recovery(roots, monkeypatch):
    monkeypatch.setattr(config, "JOB_TIMEOUT_S", .5)
    target = run_owners.path("timeout", roots["project"])
    run_owners.write(target, {"version": 1, "status": "active", "process_groups": [os.getpgrp()]})
    script = ("import os,time,json; from pathlib import Path; from kitharness.lib import intent_queue; "
              "intent_queue.append('timeout',json.loads(os.environ['PROPOSAL']),Path(os.environ['KITHARNESS_PROJECT_ROOT'])); "
              "time.sleep(30)")
    proposal = {"action": "create", "name": "dates", "body": BODY, "level": "project", "reason": "r", "evidence": "e"}

    def child(argv, env, bundle):
        return jobs.execute([sys.executable, "-c", script],
                            {**env, "PYTHONPATH": SRC, "PROPOSAL": json.dumps(proposal)}, bundle)

    with pytest.raises(jobs.Deferred, match="timed_out"):
        spawn.run("window", "timeout", roots=roots, spawn_fn=child)
    assert intent_queue.pending("timeout", roots["project"])
    owner = json.loads(target.read_text())
    assert owner["status"] == "cancelled" and len(owner["process_groups"]) == 2
    assert recovery.recover(roots=roots) == []
    assert promoter.drain("interactive", roots=roots) == []
    assert not layer.symbol_dir("project", "dates", roots["project"]).exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX groups and descriptor inheritance")
def test_live_child_holds_slot_after_supervisor_crash(roots):
    script = '''
import json, os, sys
from pathlib import Path
from kitharness.hook import jobs
roots = {key: Path(value) for key, value in json.loads(sys.argv[1]).items()}
jobs._register_child = lambda env, pid: print(pid, flush=True)
with jobs.slots(roots) as acquired:
    print(acquired, flush=True)
    jobs.execute([sys.executable, '-c', 'import time; time.sleep(30)'],
                 {**os.environ, 'KITHARNESS_RUN_ID': 'crash'}, '')
'''
    env = {**os.environ, "PYTHONPATH": SRC, "KITHARNESS_JOB_PROJECT_LIMIT": "1",
           "KITHARNESS_JOB_GLOBAL_LIMIT": "1", "KITHARNESS_DISABLE_GLOBAL": "0"}
    pid = None
    with subprocess.Popen([sys.executable, "-c", script, json.dumps({k: str(v) for k, v in roots.items()})],
                          stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env) as supervisor:
        try:
            assert supervisor.stdout.readline().strip() == "True"
            pid = int(supervisor.stdout.readline().strip())
            supervisor.kill()
            supervisor.wait(timeout=5)
            with jobs.slots(roots) as acquired:
                assert not acquired
        finally:
            supervisor.kill()
            supervisor.wait(timeout=5)
            if pid is not None:
                os.killpg(pid, signal.SIGKILL)
    for _ in range(100):
        with jobs.slots(roots) as acquired:
            if acquired:
                break
        time.sleep(.01)
    assert acquired


@pytest.mark.parametrize("value", ["0", "-2", "nan", "inf", "bad"])
def test_invalid_budget_and_nonpositive_limits_fall_back_to_safe_defaults(value):
    env = {**os.environ, "PYTHONPATH": SRC, "KITHARNESS_JOB_MAX_BUDGET_USD": value,
           "KITHARNESS_JOB_TIMEOUT_S": "0", "KITHARNESS_JOB_PROJECT_LIMIT": "0",
           "KITHARNESS_JOB_GLOBAL_LIMIT": "-1", "KITHARNESS_VERSION_KEEP": "0"}
    result = subprocess.run([sys.executable, "-c", "from kitharness import config as c; "
                             "print(c.JOB_MAX_BUDGET_USD,c.JOB_TIMEOUT_S,c.JOB_PROJECT_LIMIT,c.JOB_GLOBAL_LIMIT,c.VERSION_KEEP)"],
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=5)
    assert result.returncode == 0
    assert result.stdout.strip() == "1.0 180 1 2 10"
    assert "using" in result.stderr

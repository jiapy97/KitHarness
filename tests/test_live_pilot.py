"""Pilot limits and receipts, using synthetic streams rather than model calls."""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kitharness.lib import live_pilot as pilot


def grant(tmp_path):
    ledger = tmp_path / "ledger.json"
    pilot.write_json(ledger, {"approved_cli_sessions": 3, "model_alias": "sonnet", "reservations": []})
    return ledger


def test_reservations_survive_failure_and_cannot_be_refunded(tmp_path):
    ledger = grant(tmp_path)
    for condition in pilot.CONDITIONS:
        pilot.reserve(ledger, condition, run_id="first")
    with pytest.raises(ValueError):
        pilot.reserve(ledger, "no_learning", run_id="retry")
    data = json.loads(ledger.read_text())
    assert len(data["reservations"]) == 3
    assert all(r["status"] == "charged_before_launch" for r in data["reservations"])


def test_concurrent_drivers_cannot_charge_same_condition_twice(tmp_path):
    ledger = grant(tmp_path)

    def charge(_):
        try:
            pilot.reserve(ledger, "original", run_id="same")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(charge, range(16))) == 1
    assert len(json.loads(ledger.read_text())["reservations"]) == 1


def test_missing_grant_is_never_implicitly_created(tmp_path):
    with pytest.raises(FileNotFoundError):
        pilot.reserve(tmp_path / "absent.json", "original", run_id="new")


@pytest.mark.parametrize("change", [{"approved_cli_sessions": 30}, {"model_alias": "opus"}, {"reservations": None}])
def test_invalid_grant_fails_closed(tmp_path, change):
    ledger = grant(tmp_path)
    pilot.write_json(ledger, {**json.loads(ledger.read_text()), **change})
    with pytest.raises(ValueError):
        pilot.reserve(ledger, "original", run_id="new")


def test_environment_drops_inherited_credentials_and_provider_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-api-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://untrusted.invalid")
    monkeypatch.setenv("PYTHONPATH", "/personal/hooks")
    env = pilot.environment(tmp_path)
    assert not {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "PYTHONPATH"} & env.keys()
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in env
    assert env["HOME"] == str(Path.home())
    assert env["KITHARNESS_CLAUDE_BIN"] == str(tmp_path / "shim/claude")
    assert env["CLAUDE_CODE_TMPDIR"] == str(tmp_path / "tmp")
    assert env["XDG_STATE_HOME"] == str(tmp_path / "state")


def task():
    return {"cases": [{"id": "case-one", "expected": 42}]}


def stream(*, tools=None, mcp=None, model="claude-sonnet-test", answer=None):
    return "\n".join(json.dumps(frame) for frame in [
        {"type": "system", "subtype": "init", "model": model, "tools": tools or [], "mcp_servers": mcp or []},
        {"type": "system", "subtype": "hook_response", "hook_event": "SessionStart", "outcome": "success", "output": "private-hook-body"},
        {"type": "assistant", "message": {"model": model, "content": [{"type": "thinking", "thinking": "private-thinking"}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": json.dumps(answer or {"answers": {"case-one": 42}}), "usage": {"input_tokens": 123}, "total_cost_usd": 0.01}])


def test_receipt_keeps_observed_identity_and_usage_without_thinking_or_hook_body():
    result = pilot.receipt(stream(), task())
    assert result["protocol_passed"] and result["grade"]["success"]
    assert result["observed_model_ids"] == ["claude-sonnet-test"]
    assert result["usage"] == {"input_tokens": 123}
    assert result["estimated_cost_usd"] == 0.01
    assert "private-" not in json.dumps(result)


@pytest.mark.parametrize("kwargs", [{"tools": ["Bash"]}, {"mcp": [{"name": "unapproved"}]}, {"answer": {"text": "wrong-shape"}}])
def test_unexpected_tool_server_or_invalid_answer_fails_protocol(kwargs):
    assert pilot.receipt(stream(**kwargs), task())["protocol_passed"] is False


@pytest.mark.parametrize("data", ["", "not-json", "[]", '{"type":"result"}'])
def test_incomplete_or_invalid_stream_is_not_success(data):
    try:
        result = pilot.receipt(data, task())
    except ValueError:
        return
    assert not result["protocol_passed"]


def test_tools_and_mcp_are_disabled_and_no_fallback_or_resume_is_enabled(tmp_path):
    args = pilot.command("/trusted/claude", tmp_path, tmp_path / "source")
    assert args[args.index("--tools") + 1] == ""
    assert json.loads(args[args.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert args[args.index("--model") + 1] == "sonnet"
    assert not {"--fallback-model", "--resume", "--continue", "--dangerously-skip-permissions"} & set(args)
    assert "--no-session-persistence" in args


def test_profile_does_not_grant_grading_or_other_arm_files(tmp_path):
    visible = tmp_path / "attempts/kit"
    profile = pilot.pilot_profile(visible, "/trusted/claude", tmp_path / "sources/kit")
    assert "(deny default)" in profile and "(allow default)" not in profile
    assert str(tmp_path / "grading") not in profile
    assert str(tmp_path / "attempts/original") not in profile
    assert str(tmp_path / "native-auth/.credentials.json") not in profile
    assert str(tmp_path / "native-auth/settings.json") not in profile
    assert '(remote tcp "*:443")' in profile
    assert str(tmp_path / "native-auth/.oauth_refresh.lock") not in profile


def test_relay_sandbox_has_no_personal_keychain_or_oauth_refresh_exemptions(tmp_path):
    profile = pilot.pilot_profile(tmp_path / "visible", "/trusted/claude")
    assert "login.keychain" not in profile
    assert ".credentials.json" not in profile and ".oauth_refresh.lock" not in profile


def test_relay_cannot_reuse_old_subscription_execution_grant(tmp_path):
    pilot.write_json(tmp_path / "pilot.json", {"auth_source": "cc-switch"})
    with pytest.raises(ValueError, match="API budget"):
        pilot.execute(tmp_path, grant(tmp_path))
    assert json.loads((tmp_path / "ledger.json").read_text())["reservations"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="macOS/POSIX pilot process groups")
def test_timeout_kills_process_group_without_retry(tmp_path):
    result = pilot.run_process([sys.executable, "-I", "-c", "import time;time.sleep(10)"],
                               env={}, cwd=tmp_path, timeout=0.05)
    assert result["timed_out"] and result["returncode"] != 0


def test_existing_receipts_cannot_be_overwritten(tmp_path):
    pilot.write_json(tmp_path / "pilot-report.json", {"attempts": ["evidence"]})
    with pytest.raises(FileExistsError):
        pilot.execute(tmp_path, tmp_path / "ledger", tmp_path / "auth")
    assert json.loads((tmp_path / "pilot-report.json").read_text())["attempts"] == ["evidence"]


def test_local_synthetic_error_is_not_a_server_model_identity():
    frames = [json.loads(line) for line in stream(model="<synthetic>").splitlines()]
    frames[-1].update(is_error=True, result="OAuth refresh failed")
    result = pilot.receipt("\n".join(json.dumps(f) for f in frames), task())
    assert result["observed_model_ids"] == []
    assert result["error_result_redacted"] == "OAuth refresh failed"
    assert not result["protocol_passed"]


def driver_fixture(tmp_path, monkeypatch, *, host_passed=True):
    binary = tmp_path / "binary"
    binary.write_bytes(b"fake-CLI")
    plan = {"binary": str(binary), "binary_sha256": pilot.hashlib.sha256(binary.read_bytes()).hexdigest(),
            "sources": dict.fromkeys(pilot.CONDITIONS), "cli_version": "test", "selected": []}
    for condition in pilot.CONDITIONS:
        (tmp_path / condition / "workspace").mkdir(parents=True)
        plan["selected"].append({"condition": condition, "visible": condition})
    pilot.write_json(tmp_path / "pilot.json", plan)
    monkeypatch.setattr(pilot.task_eval, "_load_run", lambda _: (
        tmp_path, {"sources": plan["sources"], "suite_sha256": "frozen"},
        {"tasks": [{"id": pilot.TASK, "instruction": "synthetic", **task()}]}, "digest"))
    monkeypatch.setattr(pilot.task_eval, "prompt_for", lambda _: "public task without grading keys")
    monkeypatch.setattr(pilot, "check_host", lambda *a, **k: {"passed": host_passed})
    return grant(tmp_path)


def test_failed_host_gate_spends_no_session(tmp_path, monkeypatch):
    ledger = driver_fixture(tmp_path, monkeypatch, host_passed=False)
    monkeypatch.setattr(pilot, "run_process", lambda *a, **k: pytest.fail("must not launch CLI"))
    with pytest.raises(ValueError, match="host preflight"):
        pilot.execute(tmp_path, ledger, tmp_path / "auth")
    assert json.loads(ledger.read_text())["reservations"] == []


def test_spawn_failure_still_consumes_a_slot(tmp_path, monkeypatch):
    ledger = driver_fixture(tmp_path, monkeypatch)

    def fail(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(pilot, "run_process", fail)
    with pytest.raises(OSError):
        pilot.execute(tmp_path, ledger, tmp_path / "auth")
    assert len(json.loads(ledger.read_text())["reservations"]) == 1


def test_three_successful_sessions_are_charged_and_recorded_once(tmp_path, monkeypatch):
    ledger = driver_fixture(tmp_path, monkeypatch)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return {"returncode": 0, "timed_out": False, "stdout": stream(), "stderr": ""}

    monkeypatch.setattr(pilot, "run_process", run)
    result = pilot.execute(tmp_path, ledger, tmp_path / "auth")
    assert len(calls) == len(result["attempts"]) == len(json.loads(ledger.read_text())["reservations"]) == 3
    assert all(row["protocol_passed"] and row["grade"]["success"] for row in result["attempts"])
    assert len({kwargs["input_text"] for _, kwargs in calls}) == 1
    with pytest.raises(FileExistsError):
        pilot.execute(tmp_path, ledger, tmp_path / "auth")
    assert len(calls) == 3


def test_model_identity_drift_stops_before_third_session(tmp_path, monkeypatch):
    ledger = driver_fixture(tmp_path, monkeypatch)
    responses = iter([stream(model="claude-sonnet-a"), stream(model="claude-sonnet-b")])
    monkeypatch.setattr(pilot, "run_process", lambda *a, **k: {
        "returncode": 0, "timed_out": False, "stdout": next(responses), "stderr": ""})
    result = pilot.execute(tmp_path, ledger, tmp_path / "auth")
    assert len(result["attempts"]) == 2
    assert not result["attempts"][-1]["model_identity_matches"]
    assert len(json.loads(ledger.read_text())["reservations"]) == 2


def test_timeout_stops_without_launching_other_conditions(tmp_path, monkeypatch):
    ledger = driver_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(pilot, "run_process", lambda *a, **k: {
        "returncode": -9, "timed_out": True, "stdout": "", "stderr": ""})
    result = pilot.execute(tmp_path, ledger, tmp_path / "auth")
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["timed_out"]
    assert len(json.loads(ledger.read_text())["reservations"]) == 1

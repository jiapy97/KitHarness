"""Readiness checks fail closed and never turn a helper probe into a live result."""
import json
import subprocess
from pathlib import Path

import pytest

from autoharness.lib import live_preflight as preflight


def completed(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


def test_cli_inspection_only_calls_help_version_and_optional_auth(monkeypatch):
    calls = []
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/fake/claude")

    def run(argv, **kwargs):
        calls.append(argv)
        if "--version" in argv:
            return completed("2.1.test (Claude Code)")
        if "--help" in argv:
            return completed(" ".join(preflight.REQUIRED_FLAGS))
        return completed(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "private-email", "accessToken": "private-token"}))

    monkeypatch.setattr(preflight, "_run", run)
    result = preflight.inspect_cli(check_auth=True)
    assert result["available"] and all(result["flags"].values())
    assert result["auth"]["logged_in"]
    assert "private" not in json.dumps(result)
    assert calls == [["/fake/claude", "--version"], ["/fake/claude", "--help"], ["/fake/claude", "auth", "status", "--json"]]


def test_auth_not_read_unless_requested(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/fake/claude")

    def run(argv, **kwargs):
        assert "auth" not in argv
        return completed("version")

    monkeypatch.setattr(preflight, "_run", run)
    assert preflight.inspect_cli()["auth"] == {"checked": False}


@pytest.mark.parametrize("failure", ["missing", "error", "timeout"])
def test_unavailable_cli_is_not_treated_as_ready(monkeypatch, failure):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None if failure == "missing" else "/fake/claude")

    def denied(*a, **k):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("cli", 1)
        raise OSError("not accessible")

    monkeypatch.setattr(preflight, "_run", denied)
    assert not preflight.inspect_cli()["available"]


def test_isolated_auth_uses_new_config_without_copying_personal_files(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/fake/claude")

    def run(argv, **kwargs):
        assert "--print" not in argv and "-p" not in argv
        root = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
        assert list(root.iterdir()) == []
        assert Path(kwargs["cwd"]) == root
        return completed(json.dumps({"loggedIn": False, "authMethod": "none", "configDirectory": str(root), "email": "do-not-expose"}), 1)

    monkeypatch.setattr(preflight, "_run", run)
    result = preflight.inspect_isolated_auth()
    assert result == {"checked": True, "logged_in": False, "config_directory_matches": True, "credentials_copied": False}


def _baseline():
    return {"allowed_read": True, "allowed_write": True, "grading_read_denied": False,
            "outside_write_denied": False, "network_denied": False}


def _sandboxed():
    return {key: True for key in _baseline()}


def test_sandbox_success_requires_positive_control_and_every_boundary():
    assert preflight.classify_probe(_baseline(), _sandboxed()) == "passed"


@pytest.mark.parametrize("boundary", list(_baseline()))
def test_a_failed_sandbox_boundary_cannot_be_ignored(boundary):
    result = _sandboxed()
    result[boundary] = False
    assert preflight.classify_probe(_baseline(), result) == "failed"


@pytest.mark.parametrize("baseline", [{}, {**_baseline(), "network_denied": True}, {**_baseline(), "grading_read_denied": True}])
def test_outer_environment_denial_cannot_fake_an_isolation_pass(baseline):
    assert preflight.classify_probe(baseline, _sandboxed()) == "inconclusive_baseline"


def test_generated_probe_profile_does_not_allow_default_or_hidden_root(tmp_path):
    visible, hidden = tmp_path / "visible", tmp_path / "hidden"
    profile = preflight.probe_profile(visible, tmp_path / "helper.py")
    assert "(deny default)" in profile
    assert "(allow default)" not in profile
    assert str(visible.resolve()) in profile
    assert str(hidden.resolve()) not in profile
    assert "allow network" not in profile


def test_control_characters_cannot_inject_sandbox_rules(tmp_path):
    with pytest.raises(ValueError):
        preflight.probe_profile(tmp_path / 'visible\n(allow default)', tmp_path / "helper")


def test_probe_pass_does_not_authorize_or_mark_a_live_experiment_ready(monkeypatch):
    monkeypatch.setattr(preflight, "inspect_cli", lambda *a, **k: {"available": True})
    monkeypatch.setattr(preflight, "probe_sandbox", lambda: {"status": "passed", "models_called": 0})
    result = preflight.collect(sandbox=True)
    assert result["sandbox_probe"]["status"] == "passed"
    assert result["models_called"] == 0 and result["live_experiment_ready"] is False
    assert result["pending"]

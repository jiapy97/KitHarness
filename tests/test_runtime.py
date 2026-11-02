import ast
import json
import os
import subprocess
import sys
from pathlib import Path

from autoharness import runtime


def test_bootstrap_source_remains_python39_compatible():
    ast.parse(Path(runtime.__file__).read_text(encoding="utf-8"), feature_version=(3, 9))


def test_supported_current_python_needs_no_subprocess(monkeypatch):
    monkeypatch.delenv(runtime.PYTHON_ENV, raising=False)
    monkeypatch.setattr(runtime, "_probe", lambda *a: (_ for _ in ()).throw(AssertionError("unexpected probe")))
    assert runtime.select_python()[0] == sys.executable


def test_old_default_selects_supported_path_candidate(monkeypatch):
    monkeypatch.delenv(runtime.PYTHON_ENV, raising=False)
    monkeypatch.setattr(runtime.sys, "version_info", (3, 9, 6))
    monkeypatch.setattr(runtime.shutil, "which", lambda name: "/fake/" + name)
    monkeypatch.setattr(runtime, "_probe", lambda p: (3, 12, 0) if p.endswith("3.12") else (3, 9, 6))
    path, version, error = runtime.select_python()
    assert path == "/fake/python3.12" and version == (3, 12, 0) and error is None


def test_invalid_explicit_override_does_not_silently_fallback(monkeypatch):
    monkeypatch.setenv(runtime.PYTHON_ENV, "/missing/python")
    monkeypatch.setattr(runtime.shutil, "which", lambda *a: None)
    assert runtime.select_python()[0] is None
    assert "explicit" in runtime.select_python()[2]


def test_explicit_override_is_one_executable_not_a_shell_command(monkeypatch):
    monkeypatch.setenv(runtime.PYTHON_ENV, "/fake/python; touch unwanted")
    seen = []
    monkeypatch.setattr(runtime.shutil, "which", lambda name: seen.append(name))
    assert runtime.select_python()[0] is None
    assert seen == ["/fake/python; touch unwanted"]


def test_doctor_is_read_only_and_has_no_model_call(tmp_path):
    src = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(src), "AUTOHARNESS_PYTHON": sys.executable}
    result = subprocess.run([sys.executable, "-m", "autoharness.runtime", "doctor"],
                            env=env, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=5)
    report = json.loads(result.stdout)
    assert report["selected_python"]["version"][:2] >= [3, 11]
    assert list(tmp_path.iterdir()) == []


def test_bootstrap_mcp_keeps_stdout_as_jsonrpc(tmp_path):
    src = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(src), "AUTOHARNESS_PYTHON": sys.executable}
    result = subprocess.run([sys.executable, "-m", "autoharness.runtime", "autoharness.stage_skill.server"],
                            env=env, input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
                            cwd=tmp_path, text=True, encoding="utf-8", capture_output=True, timeout=5)
    assert result.returncode == 0
    assert json.loads(result.stdout)["id"] == 1


def test_broken_override_is_actionable_and_mcp_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(runtime, "select_python", lambda: (None, None, "bad interpreter"))
    assert runtime.main(["autoharness.stage_skill.server"]) == 2
    assert "doctor" in capsys.readouterr().err

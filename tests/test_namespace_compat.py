"""Upgrade checks across real import, CLI, environment and plugin boundaries."""
import ast
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kitharness import _compat, config
from kitharness.hook import dispatch, spawn
from kitharness.lib import layer, live_study, notify

ROOT = Path(__file__).resolve().parents[1]
LEGACY = _compat.LEGACY_PACKAGE
OLD_PREFIX = _compat.LEGACY_ENV_PREFIX


def isolated_env(tmp_path):
    env = {name: value for name, value in os.environ.items()
           if not name.startswith((_compat.ENV_PREFIX, OLD_PREFIX)) and name != "CLAUDE_PROJECT_DIR"}
    env.update(PYTHONPATH=str(ROOT / "src"), KITHARNESS_GLOBAL_ROOT=str(tmp_path / "global"),
               KITHARNESS_NOTIFY="", KITHARNESS_NOTIFY_CMD="")
    return env


@pytest.mark.parametrize("module", ["config", "hook.spawn", "hook.promoter", "lib.layer", "lib.lock", "stage_skill.server"])
def test_legacy_imports_share_canonical_module_and_reload_identity(module):
    current = importlib.import_module("kitharness." + module)
    previous = importlib.import_module(LEGACY + "." + module)
    assert previous is current
    assert previous.__spec__.name == "kitharness." + module


def test_old_config_import_can_be_reloaded_without_forking_state(tmp_path):
    source = ("import importlib; from kitharness import config; "
              f"old=importlib.import_module('{LEGACY}.config'); "
              "assert importlib.reload(old) is config")
    subprocess.run([sys.executable, "-c", source], env=isolated_env(tmp_path), cwd=tmp_path,
                   capture_output=True, text=True, check=True, timeout=10)


@pytest.mark.parametrize("suffix,value,expected", [
    ("REFLECT_EVERY_N", "7", 7), ("DISABLE_GLOBAL", "1", True),
    ("RECALL_MODE", "catalog", "catalog"), ("JOB_MAX_BUDGET_USD", "0.25", 0.25),
    ("NOTIFY_CMD", "custom-notifier", "custom-notifier"),
])
def test_existing_environment_settings_still_configure_current_package(tmp_path, suffix, value, expected):
    env = isolated_env(tmp_path)
    env.pop("KITHARNESS_" + suffix, None)
    env[OLD_PREFIX + suffix] = value
    source = "import json; from kitharness import config; print(json.dumps(config." + suffix + "))"
    result = subprocess.run([sys.executable, "-c", source], env=env, cwd=tmp_path,
                            capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout) == expected


def test_existing_roots_are_read_dynamically_and_explicit_new_values_win(tmp_path, monkeypatch):
    monkeypatch.delenv(layer.PROJECT_ROOT_ENV, raising=False)
    monkeypatch.delenv(layer.GLOBAL_ROOT_ENV, raising=False)
    monkeypatch.setenv(OLD_PREFIX + "PROJECT_ROOT", str(tmp_path / "project"))
    monkeypatch.setenv(OLD_PREFIX + "GLOBAL_ROOT", str(tmp_path / "old-global"))
    assert layer.default_root(layer.PROJECT) == tmp_path / "project"
    assert layer.default_root(layer.GLOBAL) == tmp_path / "old-global"
    monkeypatch.setenv(layer.GLOBAL_ROOT_ENV, str(tmp_path / "new-global"))
    assert layer.default_root(layer.GLOBAL) == tmp_path / "new-global"
    monkeypatch.setenv(layer.GLOBAL_ROOT_ENV, "")
    assert layer.default_root(layer.GLOBAL) == Path.home() / ".claude"


def test_new_setting_wins_over_existing_setting_including_zero(tmp_path):
    env = isolated_env(tmp_path)
    env.update({OLD_PREFIX + "RECALL_MAX_BYTES": "2000", "KITHARNESS_RECALL_MAX_BYTES": "0"})
    result = subprocess.run([sys.executable, "-c", "from kitharness import config; print(config.RECALL_MAX_BYTES)"],
                            env=env, cwd=tmp_path, capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout.strip() == "0"


def test_existing_recursion_guard_prevents_writes_and_background_work(tmp_path, monkeypatch):
    monkeypatch.delenv(config.CHILD_SESSION_ENV, raising=False)
    monkeypatch.setenv(OLD_PREFIX + "CHILD_SESSION", "1")
    roots = {layer.PROJECT: tmp_path / "project", layer.GLOBAL: tmp_path / "global"}
    stop = dispatch.dispatch({"hook_event_name": "Stop", "session_id": "legacy-child"}, roots=roots)
    assert stop["result"]["reason"] == "recursion_guard"
    write = dispatch.dispatch({"hook_event_name": "PreToolUse", "tool_name": "Write"}, roots=roots)
    assert write["deny"]
    assert list(tmp_path.iterdir()) == []


def test_child_and_notifier_environments_support_existing_integrations(tmp_path, monkeypatch):
    env = spawn.child_env("upgrade-run", tmp_path, base_env={OLD_PREFIX + "CHILD_SESSION": "0"})
    assert env[OLD_PREFIX + "CHILD_SESSION"] == env[config.CHILD_SESSION_ENV] == "1"
    assert env[OLD_PREFIX + "RUN_ID"] == env[config.RUN_ID_ENV] == "upgrade-run"
    assert env[OLD_PREFIX + "PROJECT_ROOT"] == env[config.PROJECT_ROOT_ENV] == str(tmp_path)
    calls = []
    monkeypatch.setattr(notify, "_run", lambda *args, **kwargs: calls.append(kwargs))
    notify._command("notify-tool", {"verdicts": []}, "upgrade completed")
    assert calls[0]["env"][OLD_PREFIX + "NOTIFY_SUMMARY"] == "upgrade completed"
    assert calls[0]["env"][OLD_PREFIX + "CHILD_SESSION"] == "1"


@pytest.mark.parametrize("namespace", ["kitharness", LEGACY])
def test_both_module_entry_points_serve_mcp_with_existing_interpreter_setting(tmp_path, namespace):
    env = isolated_env(tmp_path)
    env[OLD_PREFIX + "PYTHON"] = sys.executable
    result = subprocess.run([sys.executable, "-m", namespace + ".runtime", namespace + ".stage_skill.server"],
                            input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
                            env=env, cwd=tmp_path, capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout)["result"]["tools"][0]["name"] == "stage_skill"
    assert result.stderr == ""


def test_legacy_direct_hook_entry_point_preserves_recursion_guard(tmp_path):
    env = isolated_env(tmp_path)
    env[OLD_PREFIX + "CHILD_SESSION"] = "1"
    event = {"hook_event_name": "Stop", "session_id": "upgrade"}
    result = subprocess.run([sys.executable, "-m", LEGACY + ".hook.dispatch"], input=json.dumps(event),
                            env=env, cwd=tmp_path, capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stderr)["result"]["reason"] == "recursion_guard"


def test_existing_configuration_can_stage_land_and_recall_skill_via_current_manifest(tmp_path):
    env = isolated_env(tmp_path)
    project = tmp_path / ".claude"
    env.update({OLD_PREFIX + "PROJECT_ROOT": str(project), OLD_PREFIX + "DISABLE_GLOBAL": "1"})
    body = "---\nname: date-format\ndescription: Use when formatting a date.\n---\nUse ISO date formatting.\n"
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "stage_skill", "arguments": {"action": "create", "name": "date-format", "body": body,
                                                "reason": "repeatable date rule", "evidence": "use ISO formatting"}}}
    manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    result = subprocess.run([sys.executable, *manifest["mcpServers"]["stage_skill"]["args"]],
                            input=json.dumps(request) + "\n", env=env, cwd=tmp_path,
                            capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(json.loads(result.stdout)["result"]["content"][0]["text"])["ok"]
    assert not (project / "skills/date-format/SKILL.md").exists()
    hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
    env["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
    for event, payload in (("Stop", {}), ("UserPromptSubmit", {"prompt": "formatting date"})):
        command = hooks[event][0]["hooks"][0]["command"]
        result = subprocess.run(command, shell=True, input=json.dumps({"hook_event_name": event,
                                "session_id": "upgrade", **payload}), env=env, cwd=tmp_path,
                                capture_output=True, text=True, check=True, timeout=10)
        assert '"error"' not in result.stderr
    assert (project / "skills/date-format/SKILL.md").read_text() == body
    assert (project / "skills/date-format/.ledger.jsonl").is_file()
    assert "date-format" in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert not (tmp_path / "global").exists()


def test_compatibility_helpers_remain_python39_compatible():
    for path in (Path(_compat.__file__), ROOT / "src" / LEGACY / "__init__.py"):
        ast.parse(path.read_text(), feature_version=(3, 9))


def test_pinned_source_mcp_uses_original_package_and_both_environment_prefixes(tmp_path):
    package = tmp_path / "source/src" / LEGACY
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    (package / "config.py").touch()
    source = tmp_path / "source"
    assert live_study.source_package(source) == LEGACY
    live_study.context_env(tmp_path, "pinned-check")
    path = live_study.mcp_config(tmp_path, source, "pinned-check")
    server = json.loads(path.read_text())["mcpServers"]["plugin_kitharness_stage_skill"]
    assert server["args"] == ["-m", LEGACY + ".stage_skill.server"]
    assert server["env"][OLD_PREFIX + "DISABLE_GLOBAL"] == "1"
    assert server["env"]["KITHARNESS_DISABLE_GLOBAL"] == "1"

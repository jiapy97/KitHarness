import json
from pathlib import Path

import pytest

from autoharness.lib import live_auth
from autoharness.lib import live_study as study


def provider():
    return live_auth.RelayConfig({"ANTHROPIC_BASE_URL": "https://relay.example", "ANTHROPIC_AUTH_TOKEN": "private-real-key"})


def test_every_role_uses_same_fixed_model_and_explicit_cost_limit(tmp_path):
    plan = {"binary": "/pinned/claude"}
    for stage in study.ALLOWANCE:
        _, allowed = study.command(plan, tmp_path, tmp_path / "source", stage, stage, "pinned system")
        args, _ = study.command(plan, tmp_path, tmp_path / "source", stage, stage, "pinned system")
        assert args[args.index("--model") + 1] == "claude-sonnet-4-6"
        assert float(args[args.index("--max-budget-usd") + 1]) == study.ALLOWANCE[stage]
        assert "--dangerously-skip-permissions" not in args
        assert "--system-prompt" not in args and "--append-system-prompt" in args
        assert not {"Bash", "Write", "Edit", "Agent"} & allowed
        if stage in ("reflector", "curator"):
            assert study.STAGE_TOOL in allowed
        else:
            assert study.STAGE_TOOL not in allowed


def test_mcp_server_receives_no_relay_key_and_cannot_spawn_models(tmp_path):
    env = study.context_env(tmp_path, "reflector-original", provider(), source=tmp_path / "pinned", child=True)
    path = study.mcp_config(tmp_path, tmp_path / "pinned", "reflector-original")
    config = json.loads(path.read_text())
    mcp_env = config["mcpServers"]["plugin_kitharness_stage_skill"]["env"]
    assert mcp_env["ANTHROPIC_AUTH_TOKEN"] == "" and mcp_env["ANTHROPIC_API_KEY"] == ""
    assert "private-real-key" not in path.read_text()
    assert env["AUTOHARNESS_CHILD_SESSION"] == "1"
    assert env["AUTOHARNESS_CLAUDE_BIN"].endswith("shim/claude")


def test_solver_contexts_have_distinct_cli_configs_but_same_arm_library(tmp_path):
    first = study.context_env(tmp_path, "first", provider())
    second = study.context_env(tmp_path, "second", provider())
    assert first["CLAUDE_CONFIG_DIR"] != second["CLAUDE_CONFIG_DIR"]
    assert first["AUTOHARNESS_PROJECT_ROOT"] == second["AUTOHARNESS_PROJECT_ROOT"]
    assert "AUTOHARNESS_CHILD_SESSION" not in first


def test_documented_api_key_mode_is_explicit_and_does_not_modify_source(tmp_path):
    configured = provider()
    env = study.context_env(tmp_path, "official-api-key-mode", configured, credential_mode="api_key")
    assert env["ANTHROPIC_API_KEY"] == "private-real-key" and "ANTHROPIC_AUTH_TOKEN" not in env
    assert configured.env["ANTHROPIC_AUTH_TOKEN"] == "private-real-key"


def test_stream_accounting_separates_model_identity_and_read_paths():
    frames = [{"type": "system", "subtype": "init", "tools": ["Read"]},
              {"type": "assistant", "message": {"model": study.MODEL, "content": [
                  {"type": "tool_use", "name": "Read", "input": {"file_path": "/fixture/SKILL.md"}}]}},
              {"type": "result", "is_error": False, "subtype": "success"}]
    _, _, final, models, tools, paths = study.parse_stream("\n".join(json.dumps(f) for f in frames))
    assert models == [study.MODEL] and tools == ["Read"] and paths == ["/fixture/SKILL.md"]
    assert final["is_error"] is False


def test_retained_streams_never_include_thinking_or_actual_key():
    frames = [{"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "private-reasoning"},
        {"type": "text", "text": "echo private-real-key"}]}}]
    text = json.dumps(study.safe_frames(frames, provider()))
    assert "private-reasoning" not in text and "private-real-key" not in text


def test_library_content_hash_ignores_read_counters_but_detects_mutated_rules(tmp_path):
    project, global_root = tmp_path / "project", tmp_path / "global"
    (project / "skills/example").mkdir(parents=True)
    global_root.mkdir()
    skill = project / "skills/example/SKILL.md"
    skill.write_text("public rule", encoding="utf-8")
    before = study.library_hash(project, global_root, content_only=True)
    (skill.parent / ".sidecar.json").write_text('{"view":1}', encoding="utf-8")
    assert study.library_hash(project, global_root, content_only=True) == before
    skill.write_text("changed rule", encoding="utf-8")
    assert study.library_hash(project, global_root, content_only=True) != before


def test_snapshot_hash_covers_counter_state_and_all_layers(tmp_path):
    project, global_root = tmp_path / "project", tmp_path / "global"
    project.mkdir()
    global_root.mkdir()
    before = study.library_hash(project, global_root)
    (global_root / "state.json").write_text("{}", encoding="utf-8")
    assert study.library_hash(project, global_root) != before


def test_worker_payload_source_does_not_collide_with_helper_argument(tmp_path, monkeypatch):
    calls = []

    def run(*a, **kwargs):
        calls.append(kwargs)
        return {"returncode": 0, "stdout": '{"prompt":"public"}', "stderr": ""}

    monkeypatch.setattr(study.live_pilot, "run_process", run)
    result = study.worker(tmp_path, tmp_path / "pinned", "bundle", source=str(tmp_path / "pinned"))
    assert result["prompt"] == "public"
    assert json.loads(calls[0]["input_text"])["source"].endswith("pinned")


def test_study_refuses_evaluation_until_all_pilot_arms_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "_load", lambda root: (Path(root), {}, {}, {}, provider(), {"pilot": []}))
    with pytest.raises(ValueError, match="pilot"):
        study.run(tmp_path, tmp_path / "grant")

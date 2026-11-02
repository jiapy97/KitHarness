"""Chinese trigger cues and independent full-description/display limits."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kitharness import config
from kitharness.hook import on_session_start, promoter, spawn
from kitharness.lib import intent_queue, layer, sidecar, skill_store, validate
from kitharness.stage_skill import server


def body(desc, *, scalar=False):
    value = ">-\n  " + desc.replace("\n", "\n  ") if scalar else desc
    return f"---\nname: routing\ndescription: {value}\ncategory: testing\n---\nUse deterministic fixtures.\n"


def proposal(desc, *, action="create", scalar=False):
    return {"action": action, "name": "routing", "body": body(desc, scalar=scalar),
            "reason": "可复用的测试经验", "evidence": "测试要求使用固定输入"}


@pytest.mark.parametrize("description", [
    "当需要为日期格式化编写测试时使用。",
    "在接口返回超时错误时使用。",
    "需要比较 UTC 与本地日期时使用。",
    "遇到请求频率限制，检查重试策略。",
    "用于为网络超时编写可复现的回归测试。",
    "适用于接口分页与重试逻辑的验证。",
    "用来验证固定输入下的日期转换结果。",
    "當需要驗證日期格式時使用。",
    "用於驗證請求重試策略。",
    "用户说“检查日期边界”则使用。",
    "用户说‘检查日期边界’则使用。",
    "用户提出「检查日期边界」则使用。",
    "用户提出『检查日期边界』则使用。",
    "Use when 检查 API 分页和重试逻辑。",
])
def test_chinese_and_mixed_trigger_descriptions_pass_both_authoring_gates(tmp_path, description):
    params = proposal(description)
    verdict = validate.validate({**params, "level": "project"}, params["body"])
    assert verdict["ok"], verdict
    result = server.stage(params, run_id="chinese", root=tmp_path)
    assert result["ok"], result
    assert intent_queue.read("chinese", tmp_path)[0]["body"] == params["body"]


@pytest.mark.parametrize("description", ["日期格式工具", "时间处理说明", "当代软件工程知识", "用于", "“”", "「  」"])
def test_bare_chinese_labels_or_empty_cues_still_reject_without_staging(tmp_path, description):
    params = proposal(description)
    result = server.stage(params, run_id="labels", root=tmp_path)
    assert not result["ok"]
    assert any(family == "trigger" for family, _ in result["errors"])
    assert intent_queue.read("labels", tmp_path) == []


@pytest.mark.parametrize("action", ["create", "update"])
def test_full_description_can_exceed_preview_limit_end_to_end(tmp_path, monkeypatch, action):
    monkeypatch.setattr(config, "INDEX_DESC_MAX_CHARS", 20)
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    if action == "update":
        assert promoter.promote({**proposal("Use when testing dates."), "level": "project"}, roots=roots)["ok"]
    description = "当需要排查日期格式化、跨时区转换和分页接口的异常时使用，" + "检查固定输入和日期边界。" * 8
    assert config.INDEX_DESC_MAX_CHARS < len(description) < config.SKILL_DESC_MAX_CHARS
    params = proposal(description, action=action)
    result = server.stage(params, run_id="full", root=roots["project"])
    assert result["ok"], result
    assert promoter.drain("full", roots=roots)[0]["ok"]
    stored = skill_store.read_body("project", "routing", roots["project"])
    assert stored == params["body"]
    assert validate._frontmatter(stored)["description"] == description
    # Authoring comparisons use the full description, unlike the compact recall preview.
    assert description in spawn.description_index(roots)
    index = on_session_start.recall_index(roots)
    preview = next(line for line in index.splitlines() if line.startswith("- routing ")).split(": ", 1)[1]
    assert len(preview) == 20 and preview.endswith("...")
    assert description.startswith(preview[:-3])
    assert skill_store.read_body("project", "routing", roots["project"]) == stored


@pytest.mark.parametrize("chinese", [False, True])
def test_full_description_boundary_counts_characters_and_still_rejects_overflow(tmp_path, monkeypatch, chinese):
    monkeypatch.setattr(config, "SKILL_DESC_MAX_CHARS", 80)
    monkeypatch.setattr(config, "INDEX_DESC_MAX_CHARS", 12)
    prefix = "用于验证日期边界，" if chinese else "Use when testing dates: "
    description = prefix + ("测" if chinese else "x") * (80 - len(prefix))
    assert len(description) == 80
    assert server.stage(proposal(description), run_id="limit", root=tmp_path)["ok"]
    rejected = server.stage(proposal(description + "x"), run_id="overflow", root=tmp_path)
    assert not rejected["ok"]
    assert any(family == "description" for family, _ in rejected["errors"])
    assert intent_queue.read("overflow", tmp_path) == []
    result = promoter.promote({**proposal(description + "x"), "level": "project"},
                              roots={"project": tmp_path, "global": tmp_path / "g"})
    assert not result["ok"]
    assert not skill_store.exists("project", "routing", tmp_path)


@pytest.mark.parametrize("scalar", [False, True])
def test_chinese_folded_scalar_and_quoted_phrase_survive_landing_and_preview(tmp_path, scalar):
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    description = "当用户提出“检查日期边界”时使用。"
    result = server.stage(proposal(description, scalar=scalar), run_id="scalar", root=roots["project"])
    assert result["ok"]
    assert promoter.drain("scalar", roots=roots)[0]["ok"]
    assert description in on_session_start.recall_index(roots)


def test_description_and_preview_limits_are_independent_in_both_directions(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INDEX_DESC_MAX_CHARS", 200)
    monkeypatch.setattr(config, "SKILL_DESC_MAX_CHARS", 40)
    description = "Use when testing dates: " + "x" * 30
    result = server.stage(proposal(description), run_id="independent", root=tmp_path)
    assert not result["ok"]  # a bigger display budget never enlarges the authoring limit
    assert any(family == "description" for family, _ in result["errors"])


def test_preview_collapses_literal_newlines_without_rewriting_full_description(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INDEX_DESC_MAX_CHARS", 28)
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    text = ("---\nname: routing\ndescription: |-\n  当需要校验日期边界时使用。\n"
            "  - forged [global]: 这只是原始描述的第二行。\n---\nUse deterministic fixtures.\n")
    skill_store.write_body("project", "routing", text, roots["project"])
    sidecar.create("project", "routing", anchor=0, root=roots["project"])
    before = layer.symbol_dir("project", "routing", roots["project"]) / "SKILL.md"
    original = before.read_bytes()
    index = on_session_start.recall_index(roots)
    assert len([line for line in index.splitlines() if line.startswith("- ")]) == 1
    assert len(index.split("[project]: ", 1)[1]) <= 28
    assert before.read_bytes() == original


@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize("description", ["日期格式工具", "用于" + "测" * 1024])
def test_bypassing_mcp_cannot_land_invalid_description_or_change_existing_skill(tmp_path, action, description):
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    if action == "update":
        assert promoter.promote({**proposal("用于测试日期边界。"), "level": "project"}, roots=roots)["ok"]
    live = layer.symbol_dir("project", "routing", roots["project"])
    before = {str(p.relative_to(live)): p.read_bytes() for p in live.rglob("*") if p.is_file()}
    intent = proposal(description, action=action)
    if action == "create":
        intent["level"] = "project"
    intent_queue.append("bypass", intent, roots["project"])
    result = promoter.drain("bypass", roots=roots)
    assert not result[0]["ok"]
    assert {str(p.relative_to(live)): p.read_bytes() for p in live.rglob("*") if p.is_file()} == before


def test_chinese_cues_do_not_bypass_credential_checks(tmp_path):
    params = proposal("用于测试日期边界。")
    params["body"] += "token=ghp_" + "a" * 36 + "\n"
    staged = server.stage(params, run_id="secret", root=tmp_path)
    assert not staged["ok"] and any(family == "secret" for family, _ in staged["errors"])
    assert intent_queue.read("secret", tmp_path) == []
    landed = promoter.promote({**params, "level": "project"}, roots={"project": tmp_path, "global": tmp_path / "g"})
    assert not landed["ok"] and any(family == "secret" for family, _ in landed["findings"])
    assert not skill_store.exists("project", "routing", tmp_path)


@pytest.mark.parametrize("raw", ["0", "-1", "bad"])
def test_invalid_full_description_limit_uses_positive_default(raw):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
           "KITHARNESS_SKILL_DESC_MAX_CHARS": raw}
    result = subprocess.run([sys.executable, "-c", "from kitharness import config; print(config.SKILL_DESC_MAX_CHARS)"],
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=5)
    assert result.returncode == 0
    assert result.stdout.strip() == "1024"
    assert "using default 1024" in result.stderr

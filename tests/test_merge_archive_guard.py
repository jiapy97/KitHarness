"""Model proposals must name a merge target; lifecycle retirement stays independent."""
import json

import pytest

from kitharness import config
from kitharness.hook import on_session_start, promoter
from kitharness.lib import counters, intent_queue, layer, skill_store
from kitharness.stage_skill import server

INVALID_TARGETS = [
    pytest.param({}, id="omitted"),
    pytest.param({"absorbed_into": None}, id="null"),
    pytest.param({"absorbed_into": ""}, id="empty"),
    pytest.param({"absorbed_into": " \t"}, id="blank"),
    pytest.param({"absorbed_into": 7}, id="number"),
    pytest.param({"absorbed_into": []}, id="list"),
    pytest.param({"absorbed_into": {}}, id="object"),
]


def _create(name):
    return {"action": "create", "name": name, "level": "project",
            "body": f"---\nname: {name}\ndescription: Use when formatting dates.\n---\nUse ISO dates.\n",
            "reason": "learned a date convention", "evidence": "format dates as ISO"}


def _delete(**fields):
    return {"action": "delete", "name": "narrow", "reason": "Absorbed into umbrella",
            "evidence": "narrow [project] / umbrella [project]", **fields}


def _snapshot(root):
    directory = layer.symbol_dir("project", "narrow", root)
    return {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}


@pytest.mark.parametrize("fields", INVALID_TARGETS)
def test_stage_rejects_missing_or_invalid_merge_target_without_queue_write(tmp_path, fields):
    result = server.stage(_delete(**fields), run_id="merge", root=tmp_path)

    assert not result["ok"]
    assert any("absorbed_into" in detail for _, detail in result["errors"])
    assert not intent_queue.pending("merge", tmp_path)
    assert not layer.skills_dir("project", tmp_path).exists()


@pytest.mark.parametrize("fields", INVALID_TARGETS)
def test_promoter_checks_merge_target_even_when_staging_is_bypassed(tmp_path, fields):
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    assert promoter.promote(_create("narrow"), roots=roots)["ok"]
    before = _snapshot(roots["project"])

    result = promoter.promote(_delete(**fields), roots=roots)

    assert not result["ok"]
    assert any(family == "absorbed_into" for family, _ in result["findings"])
    assert _snapshot(roots["project"]) == before
    assert not (layer.archive_dir("project", roots["project"]) / "narrow").exists()


@pytest.mark.parametrize("with_target", [False, True], ids=["omitted-target", "explicit-target"])
def test_rejected_umbrella_create_keeps_source_skill_and_records_rejection(tmp_path, with_target):
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    root = roots["project"]
    assert promoter.promote(_create("narrow"), roots=roots)["ok"]
    before = _snapshot(root)
    umbrella = _create("umbrella")
    umbrella["body"] += "Rule.\n" * config.SKILL_BODY_MAX_LINES
    intent_queue.append("merge", umbrella, root)
    intent_queue.append("merge", _delete(**({"absorbed_into": "umbrella"} if with_target else {})), root)

    verdicts = promoter.drain("merge", roots=roots)

    assert [v["ok"] for v in verdicts] == [False, False]
    assert not skill_store.exists("project", "umbrella", root)
    assert _snapshot(root) == before
    assert not (layer.archive_dir("project", root) / "narrow").exists()
    account = json.loads((layer.state_dir("project", root) / "runs" / "merge.json").read_text())
    assert [row["ok"] for row in account["verdicts"]] == [False, False]
    if with_target:
        assert account["verdicts"][1]["findings"] == ["merge_dependency"]
        assert account["verdicts"][1]["depends_on"] == [0]
    else:
        assert "absorbed_into" in account["verdicts"][1]["findings"]


def test_successful_umbrella_create_allows_explicit_merge_archive(tmp_path):
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    root = roots["project"]
    assert promoter.promote(_create("narrow"), roots=roots)["ok"]
    assert server.stage(_create("umbrella"), run_id="merge", root=root)["ok"]
    assert server.stage(_delete(absorbed_into="umbrella"), run_id="merge", root=root)["ok"]

    verdicts = promoter.drain("merge", roots=roots)

    assert [v["ok"] for v in verdicts] == [True, True]
    assert skill_store.exists("project", "umbrella", root)
    assert not skill_store.exists("project", "narrow", root)
    assert (layer.archive_dir("project", root) / "narrow" / "SKILL.md").is_file()
    last_run = json.loads((layer.state_dir("project", root) / "last_run.json").read_text())
    assert last_run["absorbed"] == 1


def test_lifecycle_can_still_retire_an_unused_skill_without_merge_target(tmp_path, monkeypatch):
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    root = roots["project"]
    assert promoter.promote(_create("narrow"), roots=roots)["ok"]
    monkeypatch.setattr(config, "DISABLE_GLOBAL", True)
    monkeypatch.setattr(config, "MATURITY_THRESHOLD", {"project": 1})
    monkeypatch.setattr(config, "GRADUATION_REVIEW_SUSPENDED", False)
    counters.bump_request("project", root)

    result = on_session_start.on_session_start(roots=roots)

    assert result["archived"]["project"] == ["narrow"]
    assert not skill_store.exists("project", "narrow", root)
    assert (layer.archive_dir("project", root) / "narrow" / "SKILL.md").is_file()

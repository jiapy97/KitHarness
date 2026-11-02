"""Batch merges archive sources only after their target changes succeed."""
import errno
import json

import pytest

from kitharness import config
from kitharness.hook import promoter
from kitharness.lib import intent_queue, layer, skill_store
from kitharness.stage_skill import server


def _create(name):
    return {"action": "create", "name": name, "level": "project",
            "body": f"---\nname: {name}\ndescription: Use when formatting dates.\n---\nUse ISO dates.\n",
            "reason": "learned a convention", "evidence": "format dates as ISO"}


def _change(action, *, valid=True, name="umbrella"):
    if action == "patch":
        return {"action": "patch", "name": name,
                "old_string": "Use ISO dates." if valid else "text absent from target",
                "new_string": "Use ISO dates.\nMerged narrow lesson.",
                "reason": "absorb narrow lesson", "evidence": "narrow lesson"}
    intent = _create(name)
    intent["action"] = action
    intent["body"] += "Merged narrow lesson.\n" if valid else "Rule.\n" * config.SKILL_BODY_MAX_LINES
    return intent


def _delete(name="narrow", target="umbrella"):
    return {"action": "delete", "name": name, "absorbed_into": target,
            "reason": "merge into umbrella", "evidence": "overlapping skill descriptions"}


@pytest.fixture
def roots(tmp_path):
    result = {"project": tmp_path / "p", "global": tmp_path / "g"}
    for name in ("narrow", "umbrella"):
        assert promoter.promote(_create(name), roots=result)["ok"]
    return result


def _snapshot(roots, name="narrow"):
    directory = layer.symbol_dir("project", name, roots["project"])
    return {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}


def _drain(roots, intents, run_id="merge"):
    for intent in intents:
        intent_queue.append(run_id, intent, roots["project"])
    return promoter.drain(run_id, roots=roots)


def _account(roots, run_id="merge"):
    path = layer.state_dir("project", roots["project"]) / "runs" / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("action", ["create", "update", "patch"])
@pytest.mark.parametrize("archive_first", [False, True], ids=["write-first", "archive-first"])
def test_failed_target_change_preserves_source_and_records_dependency(roots, action, archive_first):
    before = _snapshot(roots)
    intents = [_change(action, valid=False), _delete()]
    if archive_first:
        intents.reverse()

    verdicts = _drain(roots, intents)

    assert [v["ok"] for v in verdicts] == [False, False]
    assert _snapshot(roots) == before
    assert not (layer.archive_dir("project", roots["project"]) / "narrow").exists()
    delete_index, write_index = (0, 1) if archive_first else (1, 0)
    assert verdicts[delete_index]["depends_on"] == [write_index]
    rows = _account(roots)["verdicts"]
    assert [r["action"] for r in rows] == [i["action"] for i in intents]
    assert rows[delete_index]["findings"] == ["merge_dependency"]
    assert rows[delete_index]["depends_on"] == [write_index]
    summary_path = layer.state_dir("project", roots["project"]) / "last_run.json"
    summary = json.loads(summary_path.read_text())
    assert summary["absorbed"] == 0 and summary["rejected"] == 2


@pytest.mark.parametrize("action", ["create", "update", "patch"])
@pytest.mark.parametrize("archive_first", [False, True], ids=["write-first", "archive-first"])
def test_successful_target_change_precedes_archive_and_preserves_report_order(roots, monkeypatch, action, archive_first):
    # Use staging too: forward references must not be rejected merely because delete was queued first.
    intents = [_change(action), _delete()]
    if archive_first:
        intents.reverse()
    for intent in intents:
        assert server.stage(intent, run_id="merge", root=roots["project"])["ok"]
    original_archive = skill_store.archive

    def archive_after_content(lyr, name, root=None, **kwargs):
        assert "Merged narrow lesson." in skill_store.read_body("project", "umbrella", roots["project"])
        return original_archive(lyr, name, root, **kwargs)

    monkeypatch.setattr(skill_store, "archive", archive_after_content)

    verdicts = promoter.drain("merge", roots=roots)

    assert [v["ok"] for v in verdicts] == [True, True]
    assert "Merged narrow lesson." in skill_store.read_body("project", "umbrella", roots["project"])
    assert not skill_store.exists("project", "narrow", roots["project"])
    assert (layer.archive_dir("project", roots["project"]) / "narrow" / "SKILL.md").is_file()
    assert [row["action"] for row in _account(roots)["verdicts"]] == [i["action"] for i in intents]


def test_archive_can_reference_a_new_target_created_later_in_the_batch(roots):
    verdicts = _drain(roots, [_delete(target="new-umbrella"), _change("create", name="new-umbrella")])

    assert [v["ok"] for v in verdicts] == [True, True]
    assert "Merged narrow lesson." in skill_store.read_body("project", "new-umbrella", roots["project"])
    assert not skill_store.exists("project", "narrow", roots["project"])


@pytest.mark.parametrize("failure_first", [False, True], ids=["failure-last", "failure-first"])
def test_any_failed_target_change_blocks_archive_even_after_another_change_succeeds(roots, failure_first):
    before = _snapshot(roots)
    changes = [_change("update"), _change("patch", valid=False)]
    if failure_first:
        changes.reverse()
    intents = [changes[0], _delete(), changes[1]]

    verdicts = _drain(roots, intents)

    assert [v["ok"] for v in verdicts] == ([False, False, True] if failure_first else [True, False, False])
    assert verdicts[1]["depends_on"] == ([0] if failure_first else [2])
    assert _snapshot(roots) == before


def test_dependency_account_identifies_every_failed_target_change(roots):
    before = _snapshot(roots)

    verdicts = _drain(roots, [_change("update", valid=False), _delete(), _change("patch", valid=False)])

    assert [v["ok"] for v in verdicts] == [False, False, False]
    assert verdicts[1]["depends_on"] == [0, 2]
    assert _account(roots)["verdicts"][1]["depends_on"] == [0, 2]
    assert _snapshot(roots) == before


def test_rejected_global_target_change_preserves_project_source(roots):
    global_target = {**_create("global-dates"), "level": "global"}
    assert promoter.promote(global_target, roots=roots)["ok"]
    before = _snapshot(roots)

    verdicts = _drain(roots, [_delete(target="global-dates"),
                               _change("patch", valid=False, name="global-dates")])

    assert [v["ok"] for v in verdicts] == [False, False]
    assert verdicts[0]["depends_on"] == [1]
    assert _snapshot(roots) == before


def test_independent_merge_can_succeed_when_another_target_change_fails(roots):
    for name in ("other", "other-narrow"):
        assert promoter.promote(_create(name), roots=roots)["ok"]
    before = _snapshot(roots, "other-narrow")
    intents = [_change("patch", valid=False, name="other"), _delete("other-narrow", "other"),
               _delete(), _change("patch")]

    verdicts = _drain(roots, intents)

    assert [v["ok"] for v in verdicts] == [False, False, True, True]
    assert verdicts[1]["depends_on"] == [0]
    assert _snapshot(roots, "other-narrow") == before
    assert not skill_store.exists("project", "narrow", roots["project"])


@pytest.mark.parametrize("target_archive_first", [False, True])
def test_merge_target_scheduled_for_archive_cannot_receive_another_source(roots, target_archive_first):
    assert promoter.promote(_create("final"), roots=roots)["ok"]
    before = _snapshot(roots)
    intents = [_delete(), _delete("umbrella", "final")]
    if target_archive_first:
        intents.reverse()

    verdicts = _drain(roots, intents)

    source_index, target_index = (1, 0) if target_archive_first else (0, 1)
    assert not verdicts[source_index]["ok"]
    assert verdicts[source_index]["depends_on"] == [target_index]
    assert verdicts[target_index]["ok"]
    assert _snapshot(roots) == before
    assert not skill_store.exists("project", "umbrella", roots["project"])


def test_cyclic_merges_are_rejected_without_changing_either_skill(roots):
    before = {name: _snapshot(roots, name) for name in ("narrow", "umbrella")}

    verdicts = _drain(roots, [_delete(), _delete("umbrella", "narrow")])

    assert [v["ok"] for v in verdicts] == [False, False]
    assert [v["depends_on"] for v in verdicts] == [[1], [0]]
    assert {name: _snapshot(roots, name) for name in before} == before


def test_failed_target_subfile_removal_also_blocks_source_archive(roots):
    before = _snapshot(roots)
    removal = {"action": "remove_file", "name": "umbrella", "path": "../outside.md",
               "reason": "remove obsolete detail", "evidence": "obsolete reference"}

    verdicts = _drain(roots, [_delete(), removal])

    assert [v["ok"] for v in verdicts] == [False, False]
    assert verdicts[0]["depends_on"] == [1]
    assert _snapshot(roots) == before


def test_malformed_unrelated_intent_does_not_crash_batch_dependency_tracking(roots):
    malformed = {"action": [], "name": [], "reason": "r", "evidence": "e"}

    verdicts = _drain(roots, [malformed, _delete(), _change("patch")])

    assert [v["ok"] for v in verdicts] == [False, True, True]
    assert not skill_store.exists("project", "narrow", roots["project"])


def test_transient_target_write_error_leaves_source_live_until_retry(roots, monkeypatch):
    before = _snapshot(roots)
    for intent in (_delete(), _change("update")):
        intent_queue.append("merge", intent, roots["project"])
    original_write = skill_store.write_body

    def unavailable(lyr, name, body, root=None):
        if name == "umbrella":
            raise OSError(errno.EIO, "simulated transient write error")
        return original_write(lyr, name, body, root)

    with monkeypatch.context() as scope:
        scope.setattr(skill_store, "write_body", unavailable)
        with pytest.raises(OSError, match="simulated transient"):
            promoter.drain("merge", roots=roots)
    assert _snapshot(roots) == before
    assert intent_queue.pending("merge", roots["project"])

    verdicts = promoter.drain("merge", roots=roots)

    assert [v["ok"] for v in verdicts] == [True, True]
    assert not intent_queue.pending("merge", roots["project"])
    assert not skill_store.exists("project", "narrow", roots["project"])


def test_orphan_recovery_uses_the_same_dependency_guard(roots):
    before = _snapshot(roots)
    for intent in (_delete(), _change("patch", valid=False)):
        intent_queue.append("abandoned", intent, roots["project"])
    intent_queue.take("abandoned", roots["project"])  # a crashed drain left its claim on disk

    assert promoter.drain("interactive", roots=roots) == []

    rows = _account(roots, "abandoned")["verdicts"]
    assert [row["ok"] for row in rows] == [False, False]
    assert rows[0]["depends_on"] == [1]
    assert _snapshot(roots) == before
    assert not intent_queue.pending("abandoned", roots["project"])

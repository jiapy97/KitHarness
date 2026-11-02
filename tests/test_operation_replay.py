"""Operation receipts survive queue release, account errors, and abandoned claims."""
import errno
import json

import pytest

from autoharness.hook import promoter
from autoharness.lib import (
    intent_queue,
    layer,
    ledger,
    operations,
    sidecar,
    skill_store,
)

BODY = "---\nname: dates\ndescription: Use when formatting dates.\n---\nUse ISO dates.\n"


@pytest.fixture
def roots(tmp_path):
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    assert promoter.promote({"action": "create", "name": "dates", "level": "project", "body": BODY,
                             "reason": "learned", "evidence": "example"}, roots=roots)["ok"]
    return roots


def _patch():
    return {"action": "patch", "name": "dates", "old_string": "Use ISO dates.",
            "new_string": "Use ISO dates.\nUse UTC.", "reason": "timezone", "evidence": "UTC"}


@pytest.mark.parametrize("orphan", [False, True])
def test_account_failure_retries_without_repeating_a_patch(roots, monkeypatch, orphan):
    root = roots["project"]
    operation_id = intent_queue.append("run", _patch(), root)
    with monkeypatch.context() as scope:
        scope.setattr(promoter, "_account", lambda *a: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full")))
        with pytest.raises(OSError):
            promoter.drain("run", roots=roots)
    assert operations.path(operation_id, root).is_file()
    assert intent_queue.pending("run", root)
    promoter.drain("interactive" if orphan else "run", roots=roots)
    assert skill_store.read_body("project", "dates", root).count("Use UTC.") == 1
    assert sidecar.read("project", "dates", root)["patch"] == 1
    assert len(ledger.read("project", "dates", root)) == 2
    account = json.loads((layer.state_dir("project", root) / "runs/run.json").read_text())
    assert account["verdicts"][0]["operation_id"] == operation_id


def test_identical_proposals_have_distinct_ids_and_both_execute(roots):
    root = roots["project"]
    ids = [intent_queue.append("run", _patch(), root) for _ in range(2)]
    assert ids[0] != ids[1]
    assert all(v["ok"] for v in promoter.drain("run", roots=roots))
    assert sidecar.read("project", "dates", root)["patch"] == 2


def test_legacy_claim_gets_durable_ids_that_survive_release(roots):
    root = roots["project"]
    path = layer.state_dir("project", root) / "intents/legacy.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_patch()) + "\n")
    first = intent_queue.take("legacy", root)
    intent_queue.release("legacy", root)
    second = intent_queue.take("legacy", root)
    assert operations.valid_id(first[0]["operation_id"])
    assert first == second


def test_receipt_cannot_be_reused_for_different_content(roots):
    root = roots["project"]
    intent_queue.append("run", _patch(), root)
    proposal = intent_queue.take("run", root)[0]
    operations.remember(proposal, {"ok": True, "findings": []}, root)
    changed = {**proposal, "new_string": "different"}
    with pytest.raises(ValueError, match="does not match"):
        operations.lookup(changed, root)


def test_append_replaces_a_caller_supplied_operation_id(roots):
    root = roots["project"]
    operation_id = intent_queue.append("run", {**_patch(), "operation_id": "../escape"}, root)
    assert operations.valid_id(operation_id)
    assert intent_queue.take("run", root)[0]["operation_id"] == operation_id


def test_corrupt_receipt_stops_drain_and_retains_queue(roots):
    root = roots["project"]
    operation_id = intent_queue.append("run", _patch(), root)
    path = operations.path(operation_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        promoter.drain("run", roots=roots)
    assert intent_queue.pending("run", root)
    assert sidecar.read("project", "dates", root)["patch"] == 0

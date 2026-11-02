"""Pruning must preserve recovery evidence and exactly-once completion outcomes."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from autoharness import config
from autoharness.hook import promoter
from autoharness.lib import intent_queue, layer, lock, operations, skill_store, versions

BODY = "---\nname: dates\ndescription: Use when formatting dates.\n---\nUse ISO dates.\n"


@pytest.fixture
def roots(tmp_path):
    result = {"project": tmp_path / "p", "global": tmp_path / "g"}
    assert promoter.promote({"action": "create", "name": "dates", "body": BODY,
                             "level": "project", "reason": "learned", "evidence": "dates"}, roots=result)["ok"]
    for index in range(4):
        intent_queue.append(f"run-{index}", {"action": "update", "name": "dates", "body": BODY + f"Revision {index}.\n",
                                             "reason": "revised", "evidence": "dates"}, result["project"])
        assert promoter.drain(f"run-{index}", roots=result)[0]["ok"]
    return result


def _history(root):
    return versions.list_versions("project", "dates", root)


def test_preview_then_apply_keeps_latest_versions_and_live_skill(roots):
    root = roots["project"]
    before = _history(root)
    body = skill_store.read_body("project", "dates", root)
    report = versions.prune("project", "dates", root, keep=2)
    assert not report["applied"]
    assert len(report["candidates"]) == 3
    assert report["bytes"] > 0
    assert _history(root) == before
    assert not (layer.state_dir("project", root) / "version-trash").exists()
    applied = versions.prune("project", "dates", root, keep=2, apply=True)
    assert applied["candidates"] == report["candidates"]
    assert _history(root) == before[-2:]
    assert skill_store.read_body("project", "dates", root) == body
    assert versions.prune("project", "dates", root, keep=2, apply=True)["candidates"] == []
    result = versions.rollback("project", "dates", before[-1]["id"], root)
    assert result["undo_version"]


def test_unfinished_old_transaction_survives_prune_and_still_recovers(roots):
    root = roots["project"]
    record = versions.begin("project", "dates", {"action": "update", "name": "dates"}, root)
    original = skill_store.read_body("project", "dates", root)
    skill_store.write_body("project", "dates", "interrupted", root)
    # Extra completed pre-images make the pending recovery version older than keep=1.
    for _ in range(2):
        completed = versions.begin("project", "dates", {"action": "update", "name": "dates"}, root)
        versions.commit("project", completed, {"ok": True}, root)
    report = versions.prune("project", "dates", root, keep=1, apply=True)
    assert record["id"] in report["protected"]
    versions.recover_all(roots)
    assert skill_store.read_body("project", "dates", root) == original


def test_pruning_preserves_receipts_and_committed_transaction_fallback(roots):
    root = roots["project"]
    state = layer.state_dir("project", root)
    journals = {p: p.read_bytes() for p in (state / "transactions").glob("*.json")}
    receipts = {p: p.read_bytes() for p in (state / "operations").glob("*.json")}
    old = _history(root)[1]
    versions.prune("project", "dates", root, keep=1, apply=True)
    assert not versions._version_dir("project", "dates", old["id"], root).exists()
    assert all(p.read_bytes() == data for p, data in {**journals, **receipts}.items())
    proposal = {"action": "update", "name": "dates", "body": BODY + "Revision 0.\n",
                "reason": "revised", "evidence": "dates", "operation_id": old["operation_id"]}
    assert operations.lookup(proposal, root)["ok"]
    operations.path(old["operation_id"], root).unlink()
    assert versions.completed(proposal, roots)["ok"]
    current = skill_store.read_body("project", "dates", root)
    # Reconstruct an old claim exactly as it would exist after an account crash.
    claim = intent_queue._claim_path("retry", root)
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text(json.dumps(proposal) + "\n")
    assert promoter.drain("retry", roots=roots)[0]["ok"]
    assert skill_store.read_body("project", "dates", root) == current


@pytest.mark.parametrize("keep", [0, -1, True, "2"])
def test_reject_invalid_keep_without_deleting_any_version(roots, keep):
    before = _history(roots["project"])
    with pytest.raises(ValueError, match="keep"):
        versions.prune("project", "dates", roots["project"], keep=keep, apply=True)
    assert _history(roots["project"]) == before


def test_corrupt_history_prevents_any_deletion(roots):
    root = roots["project"]
    history = _history(root)
    directory = versions._version_dir("project", "dates", history[-1]["id"], root)
    (directory / "tree" / "SKILL.md").write_text("damaged")
    with pytest.raises(ValueError, match="integrity"):
        versions.prune("project", "dates", root, keep=1, apply=True)
    assert all(versions._version_dir("project", "dates", item["id"], root).exists() for item in history)


@pytest.mark.parametrize("location", ["version", "trash", "transactions"])
def test_linked_paths_are_rejected_and_external_files_survive(roots, tmp_path, dir_link, location):
    root = roots["project"]
    state = layer.state_dir("project", root)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "private"
    sentinel.write_text("leave alone")
    if location == "version":
        target = versions._version_dir("project", "dates", _history(root)[0]["id"], root)
    else:
        target = state / ("version-trash" if location == "trash" else "transactions")
    if target.exists():
        target.rename(tmp_path / "saved")
    dir_link(target, external)
    with pytest.raises(ValueError, match="symlink"):
        versions.prune("project", "dates", root, keep=1, apply=True)
    assert sentinel.read_text() == "leave alone"


def test_prune_waits_for_same_skill_transaction_lock(roots):
    root = roots["project"]
    entered, finished = threading.Event(), threading.Event()
    errors = []

    def prune():
        entered.set()
        try:
            versions.prune("project", "dates", root, keep=1, apply=True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    with lock.file_lock(layer.symbol_lock_path("project", "dates", root)):
        worker = threading.Thread(target=prune)
        worker.start()
        assert entered.wait(2)
        assert not finished.wait(.1)
        pending = versions.begin("project", "dates", {"action": "update", "name": "dates"}, root)
    worker.join(5)
    assert finished.is_set() and not errors
    assert versions._version_dir("project", "dates", pending["id"], root).exists()


def test_crash_during_removal_does_not_expose_half_deleted_version(roots, monkeypatch):
    root = roots["project"]
    before = _history(root)
    original = versions.shutil.rmtree

    def fail(path, *args, **kwargs):
        if path.parent.name == "version-trash":
            raise OSError("interrupted removal")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(versions.shutil, "rmtree", fail)
    with pytest.raises(OSError, match="interrupted"):
        versions.prune("project", "dates", root, keep=1, apply=True)
    assert _history(root) == before[1:]
    versions.recover_all(roots)
    assert list((layer.state_dir("project", root) / "version-trash").iterdir())


def test_global_disable_and_configured_default(roots, monkeypatch):
    monkeypatch.setattr(config, "VERSION_KEEP", 3)
    assert len(versions.prune("project", "dates", roots["project"])["candidates"]) == 2
    monkeypatch.setattr(config, "DISABLE_GLOBAL", True)
    with pytest.raises(ValueError, match="disabled"):
        versions.prune("global", "dates", roots["global"], apply=True)
    assert not roots["global"].exists()


def test_cli_prune_defaults_to_preview(roots):
    result = subprocess.run([sys.executable, "-m", "autoharness.lib.versions", "prune", "dates",
                             "--root", str(roots["project"]), "--keep", "1"],
                            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
                            capture_output=True, text=True, encoding="utf-8", timeout=5)
    assert result.returncode == 0, result.stderr
    assert not json.loads(result.stdout)["applied"]
    assert len(_history(roots["project"])) == 5

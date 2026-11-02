"""Exercise full-directory recovery, rollback, and real abrupt process exits."""
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from autoharness.hook import on_session_start, promoter
from autoharness.lib import (
    intent_queue,
    layer,
    ledger,
    operations,
    sidecar,
    skill_store,
    versions,
)

BODY = "---\nname: dates\ndescription: Use when formatting dates.\n---\nUse ISO dates.\nRead scripts/helper.py.\n"


def _create(name="dates"):
    return {"action": "create", "name": name, "level": "project", "body": BODY.replace("name: dates", f"name: {name}"),
            "files": {"scripts/helper.py": "print('v1')\n"}, "reason": "learned", "evidence": "example"}


def _update():
    return {**_create(), "action": "update", "body": BODY + "Use UTC.\nRead references/new.md.\n",
            "files": {"scripts/helper.py": "print('v2')\n", "references/new.md": "new detail"}}


@pytest.fixture
def roots(tmp_path):
    roots = {"project": tmp_path / "p", "global": tmp_path / "g"}
    assert promoter.promote(_create(), roots=roots)["ok"]
    return roots


def _snapshot(root):
    path = layer.symbol_dir("project", "dates", root)
    return {str(p.relative_to(path)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
            for p in path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("failure", ["body", "sidecar", "ledger"])
def test_partial_update_failure_restores_every_file_and_counter(roots, monkeypatch, failure):
    root = roots["project"]
    before = _snapshot(root)
    target, attr = {"body": (skill_store, "write_body"), "sidecar": (sidecar, "bump_patch"),
                    "ledger": (ledger, "append")}[failure]

    def denied(*args, **kwargs):
        raise PermissionError("simulated denial after subfile writes")

    monkeypatch.setattr(target, attr, denied)
    with pytest.raises(PermissionError):
        promoter.promote(_update(), roots=roots)
    assert _snapshot(root) == before


def test_rollback_restores_directory_and_can_itself_be_undone(roots):
    root = roots["project"]
    script = layer.symbol_dir("project", "dates", root) / "scripts/helper.py"
    script.chmod(0o700)
    before = _snapshot(root)
    assert promoter.promote(_update(), roots=roots)["ok"]
    updated = _snapshot(root)
    history = versions.list_versions("project", "dates", root)
    result = versions.rollback("project", "dates", history[-1]["id"], root)
    assert _snapshot(root) == before
    assert "references/new.md" not in _snapshot(root)
    versions.rollback("project", "dates", result["undo_version"], root)
    assert _snapshot(root) == updated


def test_rollback_rejects_tampered_snapshot_before_touching_live_skill(roots):
    root = roots["project"]
    assert promoter.promote(_update(), roots=roots)["ok"]
    before = _snapshot(root)
    version = versions.list_versions("project", "dates", root)[-1]
    tree = versions._version_dir("project", "dates", version["id"], root) / "tree"
    (tree / "SKILL.md").write_text("tampered")
    with pytest.raises(ValueError, match="integrity"):
        versions.rollback("project", "dates", version["id"], root)
    assert _snapshot(root) == before


def test_snapshot_rejects_links_without_reading_or_changing_external_content(roots, tmp_path, dir_link):
    root = roots["project"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_text("leave alone")
    live = layer.symbol_dir("project", "dates", root)
    dir_link(live / "assets", outside)
    result = promoter.promote(_update(), roots=roots)
    assert not result["ok"]
    assert (outside / "private").read_text() == "leave alone"
    assert not (outside / "new.md").exists()


def test_snapshot_size_limit_fails_before_changes(roots, monkeypatch):
    before = _snapshot(roots["project"])
    monkeypatch.setattr(versions, "MAX_SNAPSHOT_BYTES", 1)
    result = promoter.promote(_update(), roots=roots)
    assert not result["ok"]
    assert _snapshot(roots["project"]) == before


_CRASH_SCRIPT = '''
import json, os, sys
from pathlib import Path
from autoharness.hook import promoter
from autoharness.lib import ledger, operations, skill_store, versions
args = json.loads(sys.argv[1])
roots = {key: Path(value) for key, value in args['roots'].items()}
module, attribute = {
    'body': (skill_store, 'write_body'),
    'ledger': (ledger, 'append'),
    'commit': (versions, 'commit'),
    'receipt': (operations, 'remember'),
    'archive': (skill_store, 'archive'),
}[args['phase']]
original = getattr(module, attribute)
def abrupt(*a, **k):
    result = original(*a, **k)
    os._exit(77)
setattr(module, attribute, abrupt)
promoter.drain('crash', roots=roots)
'''


def _crash(roots, phase):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    args = json.dumps({"roots": {key: str(value) for key, value in roots.items()}, "phase": phase})
    result = subprocess.run([sys.executable, "-c", _CRASH_SCRIPT, args], env=env, capture_output=True, timeout=10)
    assert result.returncode == 77, result.stderr.decode()


@pytest.mark.parametrize("phase", ["body", "ledger", "commit", "receipt"])
def test_real_process_exit_recovery_applies_patch_exactly_once(roots, phase):
    root = roots["project"]
    intent_queue.append("crash", {"action": "patch", "name": "dates", "old_string": "Use ISO dates.",
                                   "new_string": "Use ISO dates.\nUse UTC.", "reason": "timezone", "evidence": "UTC"}, root)
    _crash(roots, phase)
    assert intent_queue.pending("crash", root)
    result = promoter.drain("crash", roots=roots)
    assert result[0]["ok"]
    assert skill_store.read_body("project", "dates", root).count("Use UTC.") == 1
    assert sidecar.read("project", "dates", root)["patch"] == 1
    assert len(ledger.read("project", "dates", root)) == 2
    assert not intent_queue.pending("crash", root)


def test_real_exit_after_archive_restores_then_retries_without_duplicate_history(roots):
    root = roots["project"]
    assert promoter.promote(_create("umbrella"), roots=roots)["ok"]
    intent_queue.append("crash", {"action": "delete", "name": "dates", "absorbed_into": "umbrella",
                                  "reason": "merged", "evidence": "overlap"}, root)
    _crash(roots, "archive")
    assert not skill_store.exists("project", "dates", root)
    assert promoter.drain("crash", roots=roots)[0]["ok"]
    archive = layer.archive_dir("project", root)
    assert [p.name for p in archive.iterdir()] == ["dates"]
    assert len(ledger.read("project", "dates", root, archived=True)) == 2


def test_sidecar_use_increment_waits_for_skill_transaction(roots, monkeypatch):
    root = roots["project"]
    entered, release, used = threading.Event(), threading.Event(), threading.Event()
    original = promoter._land_files
    failures = []

    def paused(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    def update():
        try:
            assert promoter.promote(_update(), roots=roots)["ok"]
        except BaseException as exc:
            failures.append(exc)

    def use():
        try:
            sidecar.bump_use("project", "dates", root)
            used.set()
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(promoter, "_land_files", paused)
    writer = threading.Thread(target=update)
    reader = threading.Thread(target=use)
    writer.start()
    assert entered.wait(5)
    reader.start()
    try:
        assert not used.wait(0.05)
    finally:
        release.set()
        writer.join(5)
        reader.join(5)
    assert not failures and used.is_set()
    assert not writer.is_alive() and not reader.is_alive()
    assert sidecar.read("project", "dates", root)["use"] == 1


def test_receipt_write_failure_uses_committed_journal_on_retry(roots, monkeypatch):
    root = roots["project"]
    intent_queue.append("retry", _update(), root)
    with monkeypatch.context() as scope:
        def denied(*args):
            raise PermissionError("receipt unavailable")
        scope.setattr(operations, "remember", denied)
        with pytest.raises(PermissionError, match="receipt unavailable"):
            promoter.drain("retry", roots=roots)
    assert sidecar.read("project", "dates", root)["patch"] == 1
    assert promoter.drain("retry", roots=roots)[0]["ok"]
    assert sidecar.read("project", "dates", root)["patch"] == 1
    assert len(ledger.read("project", "dates", root)) == 2


def test_commit_failure_rolls_back_before_recording_rejection(roots, monkeypatch):
    root = roots["project"]
    before = _snapshot(root)
    intent_queue.append("retry", _update(), root)
    def denied(*args):
        raise PermissionError("commit unavailable")
    monkeypatch.setattr(versions, "commit", denied)
    assert not promoter.drain("retry", roots=roots)[0]["ok"]
    assert _snapshot(root) == before


def test_recovery_failure_retains_queue_and_preimage_for_later_retry(roots, monkeypatch):
    root = roots["project"]
    intent_queue.append("retry", _update(), root)
    with monkeypatch.context() as scope:
        def denied(*args):
            raise PermissionError("simulated write denial")
        scope.setattr(versions, "commit", denied)
        scope.setattr(versions, "_install", denied)
        with pytest.raises(versions.RecoveryError):
            promoter.drain("retry", roots=roots)
    assert intent_queue.pending("retry", root)
    assert any(record["existed"] for record in versions.list_versions("project", "dates", root))
    assert promoter.drain("retry", roots=roots)[0]["ok"]
    assert sidecar.read("project", "dates", root)["patch"] == 1
    assert len(ledger.read("project", "dates", root)) == 2


def test_rollback_refuses_to_replace_a_user_owned_skill(roots):
    root = roots["project"]
    assert promoter.promote(_update(), roots=roots)["ok"]
    version = versions.list_versions("project", "dates", root)[-1]
    sidecar.path("project", "dates", root).unlink()
    before = _snapshot(root)
    with pytest.raises(ValueError, match="agent-created"):
        versions.rollback("project", "dates", version["id"], root)
    assert _snapshot(root) == before


def test_session_start_recovers_partial_directory_before_recall(roots):
    root = roots["project"]
    before = _snapshot(root)
    intent_queue.append("crash", _update(), root)
    _crash(roots, "body")
    on_session_start.on_session_start(roots=roots)
    assert _snapshot(root) == before
    assert promoter.drain("crash", roots=roots)[0]["ok"]


def test_version_cli_lists_and_restores_a_complete_directory(roots):
    root = roots["project"]
    before = _snapshot(root)
    assert promoter.promote(_update(), roots=roots)["ok"]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    command = [sys.executable, "-m", "autoharness.lib.versions"]
    result = subprocess.run([*command, "list", "dates", "--root", str(root)],
                            env=env, capture_output=True, text=True, timeout=10, check=True)
    version_id = json.loads(result.stdout)[-1]["id"]
    subprocess.run([*command, "rollback", "dates", "--root", str(root), "--version", version_id],
                   env=env, capture_output=True, timeout=10, check=True)
    assert _snapshot(root) == before


def test_real_process_exit_during_manual_rollback_restores_pre_rollback_state(roots):
    root = roots["project"]
    assert promoter.promote(_update(), roots=roots)["ok"]
    before = _snapshot(root)
    version_id = versions.list_versions("project", "dates", root)[-1]["id"]
    script = '''
import os, sys
from pathlib import Path
from autoharness.lib import versions
original = os.replace
def abrupt(source, target):
    result = original(source, target)
    if Path(source).name == 'ready':
        os._exit(77)
    return result
os.replace = abrupt
versions.rollback('project', 'dates', sys.argv[2], Path(sys.argv[1]))
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run([sys.executable, "-c", script, str(root), version_id],
                            env=env, capture_output=True, timeout=10)
    assert result.returncode == 77, result.stderr.decode()
    versions.recover_all(roots)
    assert _snapshot(root) == before


@pytest.mark.skipif(os.name == "nt", reason="file symlinks require Windows developer mode")
def test_recovery_rejects_a_linked_journal_without_reading_external_file(roots, tmp_path, monkeypatch):
    root = roots["project"]
    external = tmp_path / "external.json"
    external.write_text("private", encoding="utf-8")
    journal = layer.state_dir("project", root) / "transactions" / ("a" * 32 + ".json")
    journal.symlink_to(external)
    original = Path.read_text

    def guarded(path, *args, **kwargs):
        assert path.resolve() != external.resolve(), "must reject the link before reading it"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    with pytest.raises(ValueError, match="symlink"):
        versions.recover_all(roots)


def test_recovery_never_deletes_an_archive_without_matching_operation_identity(roots):
    root = roots["project"]
    assert promoter.promote(_create("umbrella"), roots=roots)["ok"]
    intent_queue.append("crash", {"action": "delete", "name": "dates", "absorbed_into": "umbrella",
                                  "reason": "merged", "evidence": "overlap"}, root)
    _crash(roots, "archive")
    archived = layer.archive_dir("project", root) / "dates"
    ledger_path = archived / ledger.FILENAME
    entries = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    entries[-1]["operation_id"] = "b" * 32
    changed = "".join(json.dumps(entry) + "\n" for entry in entries)
    ledger_path.write_text(changed, encoding="utf-8")
    with pytest.raises(versions.RecoveryError):
        promoter.drain("crash", roots=roots)
    assert ledger_path.read_text(encoding="utf-8") == changed
    assert intent_queue.pending("crash", root)

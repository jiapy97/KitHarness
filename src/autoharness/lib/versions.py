"""Per-skill write-ahead snapshots, crash recovery, and explicit version rollback.

Prepared journals restore the complete pre-image before the next managed operation.
Committed journals preserve the outcome across the landing-to-receipt crash window.
Snapshots stay outside the host skill tree. This is recoverability for each skill,
not an atomic transaction across skills or isolation for the host's unlocked reads.
"""
import argparse
import errno
import hashlib
import json
import os
import shutil
import stat
import time
import uuid
from pathlib import Path

from autoharness.lib import atomic, layer, lock, operations, redact, skill_store

MAX_SNAPSHOT_BYTES = 32_000_000
MAX_SNAPSHOT_FILES = 4000


class RecoveryError(OSError):
    def __init__(self):
        super().__init__(errno.EIO, "skill recovery incomplete; retain the journal and retry")


def _state(lyr, root):
    state = layer.state_dir(lyr, root)
    if state.is_symlink():
        raise ValueError("version state directory must not be a symlink")
    return state


def _key(name):
    layer._check_name(name)
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def _directory(lyr, name, root):
    result = _state(lyr, root) / "versions" / _key(name)
    for p in (result.parent, result):
        if p.is_symlink():
            raise ValueError("version directory must not be a symlink")
    return result


def _version_dir(lyr, name, version_id, root):
    if not operations.valid_id(version_id):
        raise ValueError("invalid version id")
    result = _directory(lyr, name, root) / version_id
    if result.is_symlink():
        raise ValueError("version must not be a symlink")
    return result


def _journal(lyr, operation_id, root):
    if not operations.valid_id(operation_id):
        raise ValueError("invalid operation id")
    parent = _state(lyr, root) / "transactions"
    if parent.is_symlink():
        raise ValueError("transaction directory must not be a symlink")
    path = parent / f"{operation_id}.json"
    if path.is_symlink():
        raise ValueError("transaction journal must not be a symlink")
    return path


def _read_journal(lyr, path, root):
    if path.is_symlink():
        raise ValueError("transaction journal must not be a symlink")
    record = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(record, dict) or record.get("version") != 1 or record.get("level") != lyr
            or not operations.valid_id(record.get("id"))
            or path != _journal(lyr, record.get("operation_id"), root)
            or record.get("status") not in ("prepared", "committed", "rolled_back")):
        raise ValueError("invalid transaction journal")
    layer._check_name(record.get("name"))
    return record


def _tree_check(path):
    """Never dereference links or snapshot special devices; bound backup size."""
    if path.is_symlink() or not path.is_dir():
        raise ValueError("snapshot source must be a real directory")
    count = size = 0
    for parent, dirs, files in os.walk(path, followlinks=False):
        for name in [*dirs, *files]:
            p = Path(parent) / name
            mode = p.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError("skill versions cannot contain symlinks or special files")
            count += 1
            if stat.S_ISREG(mode):
                size += p.stat().st_size
            if count > MAX_SNAPSHOT_FILES or size > MAX_SNAPSHOT_BYTES:
                raise ValueError("skill exceeds version snapshot limits")


def _digest(path):
    _tree_check(path)
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        digest.update(str(item.relative_to(path)).encode("utf-8") + b"\0")
        digest.update(str(stat.S_IMODE(item.lstat().st_mode)).encode() + b"\0")
        if item.is_file():
            with item.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _copy_tree(source, destination):
    _tree_check(source)
    shutil.copytree(source, destination, symlinks=True)
    _tree_check(destination)  # also reject a link introduced while copying; never follow it
    for parent, _, files in os.walk(destination):
        for name in files:
            with (Path(parent) / name).open("rb") as handle:
                os.fsync(handle.fileno())
        atomic.sync_dir(parent)


def _write_journal(lyr, record, root):
    atomic.write_text(_journal(lyr, record["operation_id"], root), json.dumps(record, ensure_ascii=False))


def begin(lyr, name, intent, root=None):
    """Called under the stable symbol lock, after validation and before the first mutation."""
    operation_id = intent.get(operations.ID_FIELD) or uuid.uuid4().hex
    journal = _journal(lyr, operation_id, root)
    if journal.exists():
        previous = _read_journal(lyr, journal, root)
        if previous.get("fingerprint") != operations.fingerprint(intent) or previous["status"] != "rolled_back":
            raise ValueError("operation already has a transaction; recover or reuse its outcome")
    version_id = uuid.uuid4().hex
    live = layer.symbol_dir(lyr, name, root)
    if live.parent.is_symlink():
        raise ValueError("managed skill tree must not be a symlink")
    version = _version_dir(lyr, name, version_id, root)
    existed = live.exists()
    version.mkdir(parents=True, mode=0o700)
    try:
        if existed:
            _copy_tree(live, version / "tree")
        manifest = {"version": 1, "id": version_id, "name": name, "level": lyr,
                    "existed": existed, "operation_id": operation_id, "created_ns": time.time_ns(),
                    "action": intent.get("action")}
        if existed:
            manifest["digest"] = _digest(version / "tree")
        atomic.write_text(version / "manifest.json", json.dumps(manifest))
        record = {**manifest, "status": "prepared", "fingerprint": operations.fingerprint(intent)}
        if intent.get("action") == "delete":
            archive = layer.archive_dir(lyr, root)
            if archive.is_symlink():
                raise ValueError("archive must not be a symlink")
            record["archive_name"] = skill_store._collision_safe_dest(archive / name).name
        _write_journal(lyr, record, root)
        return record
    except BaseException:
        # An already-published prepared journal must retain its pre-image for recovery.
        if not _journal(lyr, operation_id, root).exists():
            shutil.rmtree(version)
        raise


def commit(lyr, record, verdict, root=None):
    record = {**record, "status": "committed", "verdict": redact.data(verdict)}
    _write_journal(lyr, record, root)


def _manifest(lyr, name, version_id, root):
    directory = _version_dir(lyr, name, version_id, root)
    if (directory / "manifest.json").is_symlink():
        raise ValueError("version manifest must not be a symlink")
    record = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if (not isinstance(record, dict) or record.get("version") != 1 or record.get("name") != name or record.get("level") != lyr
            or record.get("id") != version_id or type(record.get("existed")) is not bool):
        raise ValueError("invalid version manifest")
    if record["existed"] and _digest(directory / "tree") != record.get("digest"):
        raise ValueError("version content integrity check failed")
    return directory, record


def _install(lyr, name, version_id, root):
    directory, manifest = _manifest(lyr, name, version_id, root)
    live = layer.symbol_dir(lyr, name, root)
    work_parent = _state(lyr, root) / "restore-work"
    if work_parent.is_symlink():
        raise ValueError("restore work directory must not be a symlink")
    work = work_parent / uuid.uuid4().hex
    work.mkdir(parents=True, mode=0o700)
    try:
        if manifest["existed"]:
            _copy_tree(directory / "tree", work / "ready")
        if live.is_symlink() or live.parent.is_symlink():
            raise ValueError("refusing to restore over a symlink")
        live.parent.mkdir(parents=True, exist_ok=True)
        if live.parent.stat().st_dev != work.stat().st_dev:
            raise ValueError("skill and recovery state must share a filesystem")
        if live.exists():
            os.replace(live, work / "displaced")
        if manifest["existed"]:
            os.replace(work / "ready", live)
        atomic.sync_dir(live.parent)
    except BaseException:
        # Keep displaced content on failure. The immutable version remains the recovery source.
        raise
    else:
        shutil.rmtree(work)


def recover_record(lyr, record, root=None):
    name = record["name"]
    with lock.file_lock(layer.symbol_lock_path(lyr, name, root)):
        path = _journal(lyr, record["operation_id"], root)
        record = _read_journal(lyr, path, root)
        if record["name"] != name:
            raise RecoveryError()
        if record.get("status") != "prepared":
            return
        try:
            _, manifest = _manifest(lyr, name, record["id"], root)
            if manifest["operation_id"] != record["operation_id"]:
                raise ValueError("transaction pre-image belongs to another operation")
            _install(lyr, name, record["id"], root)
            if record.get("archive_name"):
                layer._check_name(record["archive_name"])
                archive = layer.archive_dir(lyr, root)
                archived = archive / record["archive_name"]
                if archive.is_symlink() or archived.is_symlink():
                    raise ValueError("refusing to recover a linked archive")
                if archived.exists():
                    _tree_check(archived)
                    from autoharness.lib import ledger
                    entries = [json.loads(line) for line in (archived / ledger.FILENAME).read_text(encoding="utf-8").split("\n") if line]
                    if not entries or entries[-1].get("operation_id") != record["operation_id"]:
                        raise ValueError("archive does not belong to this transaction")
                    shutil.rmtree(archived)
            _write_journal(lyr, {**record, "status": "rolled_back"}, root)
        except Exception as exc:
            raise RecoveryError() from exc


def recover_all(roots):
    from autoharness import config
    for lyr in config.active_layers():
        root = roots.get(lyr)
        directory = _state(lyr, root) / "transactions"
        for path in directory.glob("*.json"):
            record = _read_journal(lyr, path, root)
            if record.get("status") == "prepared":
                recover_record(lyr, record, root)


def completed(intent, roots):
    from autoharness import config
    operation_id = intent.get(operations.ID_FIELD)
    if operation_id is None:
        return None
    for lyr in config.active_layers():
        path = _journal(lyr, operation_id, roots.get(lyr))
        if path.exists():
            record = _read_journal(lyr, path, roots.get(lyr))
            if record.get("status") == "committed":
                if record.get("fingerprint") != operations.fingerprint(intent):
                    raise ValueError("committed transaction does not match its proposal")
                verdict = record["verdict"]
                if not isinstance(verdict, dict) or type(verdict.get("ok")) is not bool:
                    raise ValueError("invalid committed transaction outcome")
                verdict["findings"] = [tuple(item) for item in verdict.get("findings", [])]
                return verdict
    return None


def list_versions(lyr, name, root=None):
    result = []
    directory = _directory(lyr, name, root)
    for path in directory.iterdir() if directory.exists() else ():
        if path.is_symlink():
            raise ValueError("version must not be a symlink")
        _, manifest = _manifest(lyr, name, path.name, root)
        result.append(manifest)
    return sorted(result, key=lambda item: item["created_ns"])


def prune(lyr, name, root=None, *, keep=None, apply=False):
    """Preview by default. Keep recent pre-images and every unfinished transaction.

    Transaction journals and operation receipts are never removed: a committed
    outcome is still needed when an old queue or claim is retried.
    """
    from autoharness import config
    keep = config.VERSION_KEEP if keep is None else keep
    if type(keep) is not int or keep < 1:
        raise ValueError("keep must be an integer of at least 1")
    if lyr == layer.GLOBAL and config.DISABLE_GLOBAL:
        raise ValueError("global layer is disabled")
    with lock.file_lock(layer.symbol_lock_path(lyr, name, root)):
        protected = set()
        transactions = _state(lyr, root) / "transactions"
        if transactions.is_symlink():
            raise ValueError("transaction directory must not be a symlink")
        for path in transactions.glob("*.json"):
            record = _read_journal(lyr, path, root)
            if record["name"] == name and record["status"] == "prepared":
                # Validate recovery evidence before deleting anything else.
                _, manifest = _manifest(lyr, name, record["id"], root)
                if manifest.get("operation_id") != record["operation_id"]:
                    raise ValueError("transaction pre-image belongs to another operation")
                protected.add(record["id"])
        history = list_versions(lyr, name, root)
        protected.update(item["id"] for item in history[-keep:])
        restorable = [item for item in history if item["existed"]]
        if restorable:
            protected.add(restorable[-1]["id"])
        candidates = []
        for item in history:
            directory = _version_dir(lyr, name, item["id"], root)
            _tree_check(directory)
            if item["id"] not in protected:
                size = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
                candidates.append({"id": item["id"], "bytes": size})
        trash = _state(lyr, root) / "version-trash"
        if trash.is_symlink():
            raise ValueError("version trash must not be a symlink")
        if apply and candidates:
            trash.mkdir(mode=0o700, exist_ok=True)
            for item in candidates:
                source = _version_dir(lyr, name, item["id"], root)
                destination = trash / uuid.uuid4().hex
                # A crash during removal leaves an isolated trash directory,
                # never a half-deleted entry in the list of usable versions.
                os.replace(source, destination)
                atomic.sync_dir(source.parent)
                atomic.sync_dir(trash)
                shutil.rmtree(destination)
        return {"applied": apply, "keep": keep, "protected": sorted(protected),
                "candidates": candidates, "bytes": sum(item["bytes"] for item in candidates)}


def rollback(lyr, name, version_id, root=None):
    """Explicit operator API; preserve the current directory as a new undo version."""
    from autoharness import config
    from autoharness.lib import sidecar
    if lyr == layer.GLOBAL and config.DISABLE_GLOBAL:
        raise ValueError("global layer is disabled")
    with lock.file_lock(layer.symbol_lock_path(lyr, name, root)):
        for path in (_state(lyr, root) / "transactions").glob("*.json"):
            pending = _read_journal(lyr, path, root)
            if pending.get("name") == name and pending.get("status") == "prepared":
                recover_record(lyr, pending, root)
        directory, manifest = _manifest(lyr, name, version_id, root)
        if not manifest["existed"]:
            raise ValueError("select a version with an existing skill directory")
        _tree_check(directory / "tree")
        meta = json.loads((directory / "tree" / sidecar.FILENAME).read_text(encoding="utf-8"))
        live = layer.symbol_dir(lyr, name, root)
        if meta.get("created_by") != "agent" or (live.exists() and not sidecar.is_agent_created(lyr, name, root)):
            raise ValueError("rollback only manages agent-created skills")
        intent = {"action": "rollback", "name": name, "version_id": version_id}
        change = begin(lyr, name, intent, root)
        try:
            _install(lyr, name, version_id, root)
            verdict = {"ok": True, "action": "rollback", "level": lyr, "findings": [], "notes": []}
            commit(lyr, change, verdict, root)
        except BaseException:
            recover_record(lyr, change, root)
            raise
        return {"restored": version_id, "undo_version": change["id"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "rollback", "prune"))
    parser.add_argument("name")
    parser.add_argument("--level", choices=layer.LAYERS, default=layer.PROJECT)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--keep", type=int)
    parser.add_argument("--apply", action="store_true", help="actually remove prune candidates")
    args = parser.parse_args()
    if args.command == "rollback" and not args.version:
        parser.error("rollback requires --version")
    if args.command != "prune" and (args.keep is not None or args.apply):
        parser.error("--keep and --apply require prune")
    if args.command == "prune":
        if args.keep is not None and args.keep < 1:
            parser.error("--keep must be at least 1")
        result = prune(args.level, args.name, args.root, keep=args.keep, apply=args.apply)
    elif args.command == "list":
        result = list_versions(args.level, args.name, args.root)
    else:
        result = rollback(args.level, args.name, args.version, args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

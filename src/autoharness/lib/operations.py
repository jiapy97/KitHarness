"""Trusted queue identities and durable per-operation outcomes, outside skill directories."""
import hashlib
import json
import re

from autoharness.lib import atomic, layer, redact

ID_FIELD = "operation_id"
_ID = re.compile(r"[0-9a-f]{32}")


def valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def fingerprint(intent):
    payload = {key: value for key, value in intent.items() if key != ID_FIELD}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def path(operation_id, root=None):
    if not valid_id(operation_id):
        raise ValueError("invalid operation id")
    state = layer.state_dir(layer.PROJECT, root)
    directory = state / "operations"
    result = directory / f"{operation_id}.json"
    if state.is_symlink() or directory.is_symlink() or result.is_symlink():
        raise ValueError("operation receipts must not use symlinks")
    return result


def lookup(intent, root=None):
    operation_id = intent.get(ID_FIELD)
    if operation_id is None:
        return None  # standalone promotion and legacy callers have no queue identity
    p = path(operation_id, root)
    if not p.exists():
        return None
    record = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("version") != 1 or record.get("fingerprint") != fingerprint(intent):
        raise ValueError("operation receipt does not match its proposal")
    verdict = record["verdict"]
    if not isinstance(verdict, dict) or type(verdict.get("ok")) is not bool:
        raise ValueError("invalid operation receipt")
    verdict["findings"] = [tuple(finding) for finding in verdict.get("findings", [])]
    return verdict


def remember(intent, verdict, root=None):
    operation_id = intent.get(ID_FIELD)
    if operation_id is not None:
        record = {"version": 1, "fingerprint": fingerprint(intent), "verdict": redact.data(verdict)}
        atomic.write_text(path(operation_id, root), json.dumps(record, ensure_ascii=False))
    return verdict

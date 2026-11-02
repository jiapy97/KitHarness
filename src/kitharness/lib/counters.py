"""Tool activity counters (reflection per session, consolidation per project) and
per-layer request counters (MNG denominator: +1 per main-session Stop).

Single implementation, per-layer; O(1) read-modify-write, persisted immediately (each hook is a
separate short-lived process, nothing survives in memory). The numerator (call count) belongs to
sidecar (travels with the symbol across layers), not here. Session counts live in the repo-layer
state area (cap.md: session-scoped, written to this repo's .claude, not into live skills).

All read-modify-write increments use cross-process locks. The consolidation
watermark is separate from reflection resets and survives session cleanup.
"""
import re
from contextlib import contextmanager

from kitharness.lib import atomic, layer, lock

_SAFE_SESSION = re.compile(r"^[A-Za-z0-9_-]+$")


def _read_int(p):
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return 0


def _bump(p, delta=1):
    """Read-modify-write under an exclusive lock so concurrent hook processes
    cannot both read the same value and lose an increment."""
    with lock.file_lock(p.with_suffix(p.suffix + ".lock")):
        value = _read_int(p) + delta
        atomic.write_text(p, str(value))
    return value


def request_count(lyr, root=None):
    return _read_int(layer.state_dir(lyr, root) / "requests")


def bump_request(lyr, root=None):
    return _bump(layer.state_dir(lyr, root) / "requests")


def activity_count(root=None):
    return _read_int(layer.state_dir(layer.PROJECT, root) / "tool-calls")


def bump_activity(root=None):
    return _bump(layer.state_dir(layer.PROJECT, root) / "tool-calls")


@contextmanager
def consolidation_tick(n, root=None):
    """Reserve due activity while launching; only consume it after a normal exit.

    A heavy turn consumes all crossed periods in one launch, retaining the
    remainder. No modulo equality test, launch bursts or duplicate concurrent
    claims. A synchronous launch exception leaves the watermark unchanged.
    """
    if n <= 0:
        yield None
        return
    marker = layer.state_dir(layer.PROJECT, root) / "curator-watermark"
    with lock.file_lock(marker.with_suffix(".lock")):
        count = activity_count(root)
        previous = _read_int(marker)
        if count - previous < n:
            yield None
            return
        watermark = previous + ((count - previous) // n) * n
        yield {"count": count, "watermark": watermark}
        atomic.write_text(marker, str(watermark))


def _session_path(session_id, root=None):
    if not isinstance(session_id, str) or not _SAFE_SESSION.match(session_id):
        raise ValueError(f"unsafe session id: {session_id!r}")
    return layer.state_dir(layer.PROJECT, root) / f"session-{session_id}"


def session_count(session_id, root=None):
    return _read_int(_session_path(session_id, root))


def bump_session(session_id, root=None):
    return _bump(_session_path(session_id, root))


def reset_session(session_id, root=None):
    atomic.write_text(_session_path(session_id, root), "0")


def clear_session(session_id, root=None):
    p = _session_path(session_id, root)
    if p.exists():
        p.unlink()


def _offset_path(session_id, root=None):
    if not isinstance(session_id, str) or not _SAFE_SESSION.match(session_id):
        raise ValueError(f"unsafe session id: {session_id!r}")
    return layer.state_dir(layer.PROJECT, root) / f"offset-{session_id}"


def session_offset(session_id, root=None):
    return _read_int(_offset_path(session_id, root))


def write_session_offset(session_id, offset, root=None):
    atomic.write_text(_offset_path(session_id, root), str(int(offset)))

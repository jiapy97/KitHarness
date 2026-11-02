"""POSIX atomic-write primitive: same-dir temp + fsync + os.replace — live is never half-written.

validate-store's "the only way to persist = same-dir temp + os.replace" is implemented here alone;
skill_store / sidecar / counters all reuse it. A crash mid-write leaves only a same-dir .tmp orphan
(swept on skill_store startup); the target file is either the old content or the new content, never
a half-write.
"""
import errno
import os
import stat
import tempfile
from pathlib import Path


def sync_dir(path):
    """Persist rename metadata on platforms supporting directory fsync."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


def write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = None
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            if mode is not None:
                os.chmod(tmp, mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        sync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path, text):
    write_bytes(path, text.encode("utf-8"))

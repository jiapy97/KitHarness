"""Bound background jobs without sending prompts or raw model responses to logs."""
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from kitharness import config
from kitharness.lib import layer, lock, run_owners

_LOCAL = threading.local()
MAX_RESPONSE_BYTES = 1_000_000


class Deferred(RuntimeError):
    """Do not advance the transcript watermark or promote a partial proposal set."""


@contextmanager
def slots(roots):
    """Never queue expensive jobs behind each other; callers can retry next trigger."""
    limits = [(layer.PROJECT, config.JOB_PROJECT_LIMIT)]
    if not config.DISABLE_GLOBAL:
        limits.append((layer.GLOBAL, config.JOB_GLOBAL_LIMIT))
    with ExitStack() as stack:
        handles = []
        for lyr, count in limits:
            directory = layer.state_dir(lyr, roots.get(lyr)) / "job-slots"
            for index in range(count):
                handle = stack.enter_context(lock.try_file_lock(directory / f"{index}.lock"))
                if handle is not None:
                    handles.append(handle)
                    break
            else:
                yield False
                return
        previous = getattr(_LOCAL, "fds", ())
        _LOCAL.fds = tuple(handle.fileno() for handle in handles)
        try:
            yield True
        finally:
            _LOCAL.fds = previous


def _register_child(env, pid):
    target = run_owners.path(env[config.RUN_ID_ENV], Path(env[config.PROJECT_ROOT_ENV]))
    if target.exists():
        with lock.file_lock(target.with_suffix(".lock")):
            owner = json.loads(target.read_text(encoding="utf-8"))
            if owner.get("status") != "active":
                raise Deferred("run is no longer active")
            if pid not in owner["process_groups"]:
                owner["process_groups"].append(pid)
                run_owners.write(target, owner)


def cancel(run_id, root):
    """Quarantine partial proposals and stop registered writers before releasing slots."""
    target = run_owners.path(run_id, root)
    with lock.file_lock(target.with_suffix(".lock")):
        owner = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"version": 1, "process_groups": []}
        owner["status"] = "cancelled"
        run_owners.write(target, owner)
        groups = owner.get("process_groups", [])
    if hasattr(os, "killpg"):
        for group in groups:
            if type(group) is int and group > 0 and group != os.getpgrp():
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def response(proc):
    """Parse only a bounded host JSON response; unknown formats have unknown usage."""
    raw = getattr(proc, "stdout", None)
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def usage(proc):
    value = response(proc)
    result = {}
    fields = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    tokens = value.get("usage", {})
    if isinstance(tokens, dict):
        for field in fields:
            number = tokens.get(field)
            if type(number) is int and number >= 0:
                result[field] = number
    for field in ("total_cost_usd", "num_turns", "duration_ms"):
        number = value.get(field)
        if type(number) in (int, float) and 0 <= number <= 1e18:
            result[field] = number
    return {"available": bool(result), **result}


def execute(argv, env, bundle):
    """Time out the entire child process group; spool output instead of growing RAM."""
    started = time.monotonic()
    timed_out = False
    launch_failed = False
    kwargs = {"start_new_session": True} if os.name == "posix" else {}
    if os.name == "posix":
        kwargs["pass_fds"] = getattr(_LOCAL, "fds", ())
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                     env=env, **kwargs)
        except OSError as exc:
            launch_failed = True
            result = subprocess.CompletedProcess(argv, 127, "", str(exc))
        else:
            with child:
                try:
                    if os.name == "posix" and config.RUN_ID_ENV in env:
                        _register_child(env, child.pid)
                    child.communicate(bundle.encode("utf-8"), timeout=config.JOB_TIMEOUT_S)
                except BaseException as exc:
                    timed_out = isinstance(exc, subprocess.TimeoutExpired)
                    try:
                        if timed_out and config.RUN_ID_ENV in env:
                            cancel(env[config.RUN_ID_ENV], Path(env[config.PROJECT_ROOT_ENV]))
                    finally:
                        if os.name == "posix":
                            try:
                                os.killpg(child.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        else:
                            child.kill()
                        child.communicate()
                    if not timed_out:
                        raise
                returncode = child.returncode
            stdout.seek(0)
            stderr.seek(max(0, stderr.tell() - 8000))
            result = subprocess.CompletedProcess(argv, returncode,
                stdout.read(MAX_RESPONSE_BYTES + 1).decode("utf-8", errors="replace"),
                stderr.read(8000).decode("utf-8", errors="replace"))
    result.timed_out = timed_out
    result.launch_failed = launch_failed
    result.elapsed_ms = round((time.monotonic() - started) * 1000)
    return result

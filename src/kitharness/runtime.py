"""Python 3.9-compatible bootstrap shared by hooks, MCP and environment diagnostics."""
import json
import os
import runpy
import shutil
import subprocess
import sys

from kitharness._compat import canonical_module, env_get, export_env

MIN_PYTHON = (3, 11)
TARGETS = ("kitharness.hook.dispatch", "kitharness.stage_skill.server")
PYTHON_ENV = "KITHARNESS_PYTHON"


def _probe(executable):
    try:
        result = subprocess.run([executable, "-c", "import json,sys; print(json.dumps(list(sys.version_info[:3])))"],
                                capture_output=True, text=True, encoding="utf-8", timeout=2, check=False)
        version = json.loads(result.stdout)
        if result.returncode or not isinstance(version, list) or len(version) != 3:
            return None
        if not all(type(item) is int for item in version):
            return None
        return tuple(version)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def select_python():
    override = env_get(PYTHON_ENV, "").strip()
    if not override and sys.version_info[:2] >= MIN_PYTHON:
        return sys.executable, tuple(sys.version_info[:3]), None
    candidates = [override] if override else ["python3.14", "python3.13", "python3.12", "python3.11", "python3"]
    seen = set()
    for candidate in candidates:
        executable = shutil.which(os.path.expanduser(candidate))
        if not executable or os.path.realpath(executable) in seen:
            continue
        seen.add(os.path.realpath(executable))
        version = _probe(executable)
        if version is not None and version[:2] >= MIN_PYTHON:
            return executable, version, None
    detail = "explicit KITHARNESS_PYTHON is invalid or below 3.11" if override else "no Python 3.11+ found on PATH"
    return None, None, detail


def doctor():
    executable, version, error = select_python()
    claude = shutil.which(env_get("KITHARNESS_CLAUDE_BIN", "claude"))
    return {"ok": executable is not None and claude is not None,
            "bootstrap_python": {"executable": sys.executable, "version": list(sys.version_info[:3])},
            "selected_python": {"executable": executable, "version": list(version) if version else None},
            "claude_executable": claude, "python_error": error,
            "hint": "Set KITHARNESS_PYTHON to a Python 3.11+ executable; no login or paid model call is made."}


def main(argv=None):
    args = [canonical_module(arg) for arg in (sys.argv[1:] if argv is None else argv)]
    if args == ["doctor"]:
        report = doctor()
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0 if report["ok"] else 1
    if len(args) != 1 or args[0] not in TARGETS:
        print("Usage: python3 -m kitharness.runtime doctor | " + " | ".join(TARGETS), file=sys.stderr)
        return 2
    executable, _, error = select_python()
    if error:
        print("KitHarness runtime: " + error + "; run python3 -m kitharness.runtime doctor", file=sys.stderr)
        return 0 if args[0] == TARGETS[0] else 2
    os.environ[PYTHON_ENV] = executable
    if os.path.realpath(executable) != os.path.realpath(sys.executable):
        os.execvpe(executable, [executable, "-m", args[0]], export_env(os.environ))
    sys.argv = [args[0]]
    runpy.run_module(args[0], run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

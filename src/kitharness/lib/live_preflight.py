"""No-model readiness checks for a live experiment; never invoke claude --print.

The sandbox probe runs only a fixed local helper on synthetic files and loopback.
It is not a Claude sandbox or evidence that a live model experiment has run.
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from kitharness.lib import atomic

REQUIRED_FLAGS = ("--restricted", "--setting-sources", "--settings", "--plugin-dir",
                  "--max-budget-usd", "--output-format", "--include-hook-events")


def _run(argv, *, timeout=10, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, check=False, **kwargs)


def _object(text):
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def inspect_cli(binary="claude", *, check_auth=False):
    executable = shutil.which(binary)
    if not executable:
        return {"available": False}
    try:
        version = _run([executable, "--version"])
        help_result = _run([executable, "--help"])
        result = {"available": version.returncode == 0, "version": version.stdout.strip()[:100],
                  "flags": {flag: flag in help_result.stdout for flag in REQUIRED_FLAGS},
                  "auth": {"checked": False}}
        if check_auth:
            auth = _run([executable, "auth", "status", "--json"])
            data = _object(auth.stdout)
            method = data.get("authMethod")
            result["auth"] = {"checked": True, "logged_in": data.get("loggedIn") is True,
                              "method": method if method in ("none", "claude.ai", "oauth_token", "api_key", "api_key_helper", "third_party") else "unknown"}
        return result
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "reason": "local CLI inspection failed"}


def inspect_isolated_auth(binary="claude"):
    """Check a new configuration directory without copying settings or credentials."""
    executable = shutil.which(binary)
    if not executable:
        return {"checked": False, "reason": "CLI unavailable"}
    try:
        with tempfile.TemporaryDirectory(prefix="kitharness-isolated-auth-") as temporary:
            env = {**os.environ, "CLAUDE_CONFIG_DIR": temporary}
            result = _run([executable, "auth", "status", "--json"], env=env, cwd=temporary)
            data = _object(result.stdout)
            directory = data.get("configDirectory")
            return {"checked": True, "logged_in": data.get("loggedIn") is True,
                    "config_directory_matches": isinstance(directory, str) and Path(directory).resolve() == Path(temporary).resolve(),
                    "credentials_copied": False}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"checked": False, "reason": "isolated authentication inspection failed"}


def _literal(path):
    text = str(Path(path).resolve())
    if any(ord(char) < 32 for char in text):
        raise ValueError("control characters are not allowed in sandbox paths")
    return json.dumps(text, ensure_ascii=False)


def probe_profile(workspace, helper, interpreter=None):
    """A narrow profile for the fixed Python probe, not for arbitrary host tools."""
    interpreter = Path(interpreter or sys.executable).resolve()
    prefix = Path(sys.base_prefix).resolve()
    return (
        '(version 1)\n(deny default)\n(import "dyld-support.sb")\n'
        "(allow mach-bootstrap)\n(allow syscall*)\n"
        "(allow process*)\n(allow sysctl-read)\n(allow file-read-metadata)\n"
        '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))\n'
        f'(allow file-map-executable (subpath "/System") (subpath "/usr/lib") (subpath {_literal(prefix)}) (literal {_literal(interpreter)}))\n'
        "(allow file-read* (subpath \"/System\") (subpath \"/usr/lib\") (subpath \"/usr/share\")\n"
        f"  (subpath {_literal(prefix)}) (literal {_literal(interpreter)})\n"
        f"  (literal {_literal(helper)}) (subpath {_literal(workspace)})\n"
        '  (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))\n'
        f'(allow file-write* (subpath {_literal(workspace)}) (literal "/dev/null"))\n'
    )


_HELPER = '''
import errno, json, socket, sys
from pathlib import Path
visible, hidden, port = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
result = {'allowed_read': (visible/'input.txt').read_text(encoding='utf-8') == 'public-fixture'}
(visible/'output.txt').write_text('public-output', encoding='utf-8')
result['allowed_write'] = True
try:
    (hidden/'grading.json').read_bytes()
    result['grading_read_denied'] = False
except OSError as exc:
    result['grading_read_denied'] = exc.errno in (errno.EACCES, errno.EPERM)
try:
    (hidden/'outside-write').write_text('fixture', encoding='utf-8')
    result['outside_write_denied'] = False
except OSError as exc:
    result['outside_write_denied'] = exc.errno in (errno.EACCES, errno.EPERM)
try:
    with socket.socket() as connection:
        connection.settimeout(1)
        connection.connect(('127.0.0.1', port))
    result['network_denied'] = False
except OSError as exc:
    result['network_denied'] = exc.errno in (errno.EACCES, errno.EPERM)
print(json.dumps(result))
'''


def classify_probe(baseline, sandboxed):
    allowed = ("allowed_read", "allowed_write")
    denied = ("grading_read_denied", "outside_write_denied", "network_denied")
    if not all(baseline.get(key) is True for key in allowed) or not all(baseline.get(key) is False for key in denied):
        return "inconclusive_baseline"
    return "passed" if all(sandboxed.get(key) is True for key in (*allowed, *denied)) else "failed"


def probe_sandbox():
    binary = shutil.which("sandbox-exec")
    if sys.platform != "darwin" or not binary:
        return {"status": "unavailable", "scope": "fixed local helper only"}
    try:
        with tempfile.TemporaryDirectory(prefix="kitharness-isolation-probe-") as temporary:
            root = Path(temporary).resolve()
            visible, hidden = root / "visible", root / "hidden"
            visible.mkdir()
            hidden.mkdir()
            (visible / "input.txt").write_text("public-fixture", encoding="utf-8")
            (hidden / "grading.json").write_text('{"answer":"synthetic-only"}', encoding="utf-8")
            helper = root / "probe.py"
            helper.write_text(_HELPER, encoding="utf-8")
            profile = root / "probe.sb"
            profile.write_text(probe_profile(visible, helper), encoding="utf-8")
            env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(2)
                args = [str(Path(sys.executable).resolve()), "-I", "-B", "-S", str(helper), str(visible), str(hidden), str(listener.getsockname()[1])]
                baseline = _run(args, env=env)
                (hidden / "outside-write").unlink(missing_ok=True)
                sandboxed = _run([binary, "-f", str(profile), *args], env=env)
            first, second = _object(baseline.stdout), _object(sandboxed.stdout)
            status = classify_probe(first, second) if baseline.returncode == sandboxed.returncode == 0 else "unavailable"
            return {"status": status, "scope": "fixed synthetic helper; not a live Claude sandbox",
                    "baseline": first, "sandboxed": second,
                    "exit_codes": {"baseline": baseline.returncode, "sandboxed": sandboxed.returncode},
                    "profile_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
                    "models_called": 0}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"status": "unavailable", "scope": "fixed local helper only", "models_called": 0}


def collect(*, claude_bin="claude", check_auth=False, sandbox=False):
    return {"version": 1, "models_called": 0, "platform": platform.system(),
            "python": platform.python_version(), "claude": inspect_cli(claude_bin, check_auth=check_auth),
            "isolated_auth": inspect_isolated_auth(claude_bin) if check_auth else {"checked": False},
            "sandbox_probe": probe_sandbox() if sandbox else {"status": "not_checked"},
            "live_experiment_ready": False,
            "pending": ["explicit model and usage/budget limits", "isolated host authentication and provider access",
                        "verified live CLI sandbox", "warmup and library-freezing driver", "all-call accounting"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--check-auth", action="store_true")
    parser.add_argument("--probe-sandbox", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = collect(claude_bin=args.claude_bin, check_auth=args.check_auth, sandbox=args.probe_sandbox)
    text = json.dumps(result, ensure_ascii=True, indent=2) + "\n"
    if args.output:
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError("preflight output already exists")
        atomic.write_text(args.output, text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()

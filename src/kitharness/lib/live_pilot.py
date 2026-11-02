"""Bounded macOS host pilot, deliberately without tools, warmup or learning.

Only --execute starts model sessions. Reservations survive failures and restarts.
This is a protocol/host check, not a learning-effect evaluation.
"""
import argparse
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from kitharness._compat import export_env
from kitharness.lib import atomic, live_auth, lock, redact, task_eval
from kitharness.lib.live_preflight import _HELPER, _literal, _object, classify_probe

CONDITIONS = task_eval.CONDITIONS
LIMIT = 3
TASK = "utc-boundaries"
SYSTEM = "Solve the supplied synthetic data task. Return only the requested JSON object."


def write_json(path, value):
    atomic.write_text(Path(path), json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def reserve(ledger, condition, *, run_id):
    """Charge before launch; no refund, even if spawning fails or the driver dies."""
    ledger = Path(ledger)
    with lock.file_lock(ledger.with_suffix(".lock")):
        data = json.loads(ledger.read_text(encoding="utf-8"))
        if (data.get("approved_cli_sessions") != LIMIT or data.get("model_alias") != "sonnet"
                or not isinstance(data.get("reservations"), list)):
            raise ValueError("invalid session grant")
        used = data["reservations"]
        if condition not in CONDITIONS or len(used) >= LIMIT or any(item["condition"] == condition for item in used):
            raise ValueError("session grant exhausted or condition already reserved")
        used.append({"number": len(used) + 1, "condition": condition, "run_id": run_id,
                     "reserved_at_unix": time.time(), "status": "charged_before_launch"})
        write_json(ledger, data)
        return len(used)


def pilot_profile(visible, binary, source=None, *, relay_port=443):
    """Default deny; HTTPS/DNS allowed, not an endpoint allowlist or VM boundary."""
    readable = [Path(visible), Path(sys.base_prefix), Path(binary), Path(sys.executable),
                Path("/bin"), Path("/usr/bin"), Path("/private/etc/ssl"),
                Path("/private/etc/resolv.conf"), Path("/private/etc/hosts")]
    if source:
        readable.append(Path(source))
    rules = " ".join(f"(subpath {_literal(path)})" for path in readable)
    if type(relay_port) is not int or not 1 <= relay_port <= 65535:
        raise ValueError("invalid relay port")
    return ('(version 1)\n(deny default)\n(import "system.sb")\n'
            '(allow mach-bootstrap)\n(allow syscall*)\n(allow process*)\n'
            '(allow signal (target children))\n'
            '(allow file-read-metadata)\n'
            f'(allow file-read* file-map-executable {rules})\n'
            f'(allow file-write* (subpath {_literal(visible)}))\n'
            '(system-network)\n'
            '(allow network-outbound (literal "/private/var/run/mDNSResponder"))\n'
            f'(allow network-outbound (remote tcp "*:{relay_port}") (remote udp "*:53") (remote tcp "*:53"))\n')


def environment(visible, provider=None):
    visible = Path(visible).resolve()
    # Do not inherit API keys, provider overrides, personal hooks or Python paths.
    env = {"HOME": str(Path.home()), "USER": os.environ.get("USER", ""),
            "PATH": f"{visible / 'shim'}:/usr/bin:/bin", "LANG": "en_US.UTF-8",
            "CLAUDE_CONFIG_DIR": str(visible / "config"),
            "TMPDIR": str(visible / "tmp"), "CLAUDE_CODE_TMPDIR": str(visible / "tmp"),
            "XDG_STATE_HOME": str(visible / "state"), "XDG_CACHE_HOME": str(visible / "cache"),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
            "KITHARNESS_PROJECT_ROOT": str(visible / "workspace/.claude"),
            "KITHARNESS_GLOBAL_ROOT": str(visible / "global-config"), "KITHARNESS_DISABLE_GLOBAL": "1",
            "KITHARNESS_NOTIFY": "", "KITHARNESS_CLAUDE_BIN": str(visible / "shim/claude"),
            "KITHARNESS_PYTHON": str(Path(sys.executable).resolve()),
            "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    if provider:
        env.update(provider.env)
    return export_env(env)


def setup(repo, output, *, cc_switch_dir=None):
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        raise ValueError("this pilot requires macOS sandbox-exec")
    binary = Path(shutil.which("claude") or "/nonexistent").resolve(strict=True)
    cc_switch_dir = Path(cc_switch_dir or Path.home() / ".cc-switch").resolve()
    provider = live_auth.load_cc_switch(cc_switch_dir)
    output = Path(output).resolve()
    task_eval.prepare(repo, Path(repo) / "evals/tasks/suite.json", output, model="sonnet")
    output.chmod(0o700)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    selected = []
    for condition in CONDITIONS:
        attempt = next(item for item in manifest["attempts"] if item["condition"] == condition and item["task"] == TASK)
        visible = (output / attempt["workspace"]).parent
        for name in ("config", "tmp", "shim"):
            (visible / name).mkdir()
        # Both old hard-coded `claude` and new configurable spawns are blocked.
        stub = visible / "shim/claude"
        stub.write_text('#!/bin/sh\nif [ "$KIT_PILOT_GUARD_PROBE" = "1" ]; then\n'
                        '  exit 77\nfi\nprintf "blocked descendant CLI\\n" >> "$CLAUDE_CONFIG_DIR/blocked-spawns"\nexit 77\n', encoding="utf-8")
        stub.chmod(0o755)
        python = visible / "shim/python3"
        python.write_text(f'#!/bin/sh\nexec {shlex.quote(str(Path(sys.executable).resolve()))} "$@"\n', encoding="utf-8")
        python.chmod(0o755)
        adapter_digest = live_auth.install_adapter(visible)
        write_json(visible / "settings.json", {"enabledPlugins": {}, "autoUpdatesChannel": "stable"})
        source = output / "sources" / condition if condition != "no_learning" else None
        (visible / "pilot.sb").write_text(pilot_profile(visible, binary, source, relay_port=provider.port), encoding="utf-8")
        selected.append({**attempt, "visible": str(visible.relative_to(output)), "auth_adapter_sha256": adapter_digest})
    data = {"version": 1, "kind": "host_protocol_pilot", "requested_model_alias": "sonnet",
            "binary": str(binary), "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "cli_version": subprocess.check_output([str(binary), "--version"], text=True, encoding="utf-8").strip(),
            "sources": manifest["sources"], "selected": selected, "models_launched": 0}
    data.update(auth_source="cc-switch", cc_switch_dir=str(cc_switch_dir), relay=provider.public())
    write_json(output / "pilot.json", data)
    return data


def run_process(argv, *, env, cwd, timeout=30, input_text=None):
    """Kill the process group on timeout/interruption; never retry a model launch."""
    process = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", start_new_session=True)
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        return {"returncode": process.returncode, "stdout": stdout, "stderr": stderr, "timed_out": False}
    except BaseException as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        if not isinstance(exc, subprocess.TimeoutExpired):
            raise
        return {"returncode": process.returncode, "stdout": stdout, "stderr": stderr, "timed_out": True}


def command(binary, visible, source=None):
    argv = ["/usr/bin/sandbox-exec", "-f", str(visible / "pilot.sb"), str(binary),
            "--print", "--restricted", "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disable-slash-commands", "--setting-sources", "", "--settings", str(visible / "settings.json"),
            "--model", "sonnet", "--permission-mode", "dontAsk", "--no-session-persistence",
            "--output-format", "stream-json", "--verbose", "--include-hook-events", "--no-chrome",
            "--max-budget-usd", "0.25", "--system-prompt", SYSTEM]
    if source:
        argv.extend(["--plugin-dir", str(source)])
    return argv


def check_host(output, cc_switch_dir=None, *, conditions=CONDITIONS):
    """Same policy and environment as the pilot, but NO --print/model calls."""
    output = Path(output).resolve()
    plan = json.loads((output / "pilot.json").read_text(encoding="utf-8"))
    if plan.get("auth_source") != "cc-switch":
        raise ValueError("old authentication plan; prepare a new relay pilot")
    configured = Path(plan["cc_switch_dir"]).resolve()
    if cc_switch_dir is not None and Path(cc_switch_dir).resolve() != configured:
        raise ValueError("authentication source differs from plan")
    provider = live_auth.load_cc_switch(configured)
    if provider.public() != plan["relay"]:
        raise ValueError("selected relay changed; prepare a new pilot")
    checks = []
    for attempt in plan["selected"]:
        if attempt["condition"] not in conditions:
            continue
        visible = output / attempt["visible"]
        helper = visible / "shim/live_auth.py"
        expected = attempt.get("auth_adapter_sha256")
        if not expected or hashlib.sha256(helper.read_bytes()).hexdigest() != expected:
            raise ValueError("authentication adapter missing or changed; prepare a new pilot")
        env = environment(visible)
        base = ["/usr/bin/sandbox-exec", "-f", str(visible / "pilot.sb")]
        result = run_process([*base, plan["binary"], "--restricted", "--setting-sources", "", "auth", "status", "--json"],
                             env=environment(visible, provider), cwd=visible / "workspace")
        auth = _object(result["stdout"])
        directory = auth.get("configDirectory")
        passed = (result["returncode"] == 0 and auth.get("loggedIn") is True
                  and auth.get("authMethod") in ("oauth_token", "api_key", "auth_token")
                  and isinstance(directory, str) and Path(directory).resolve() == visible / "config")
        verified = run_process([*base, str(Path(sys.executable).resolve()), "-I", "-B", str(helper), "--verify-relay"],
                               env=env, cwd=visible / "workspace",
                               input_text=json.dumps({"env": provider.env, "cli_version": plan["cli_version"]}))
        profile_auth = _object(verified["stdout"])
        profile_passed = verified["returncode"] == 0 and profile_auth.get("authenticated") is True
        # Probe the actual run's grading directory under the exact CLI policy.
        probe = visible / "probe.py"
        probe.write_text(_HELPER, encoding="utf-8")
        (visible / "input.txt").write_text("public-fixture", encoding="utf-8")
        hidden = output / "grading"
        (hidden / "grading.json").write_text('{"fixture":"not-for-model"}', encoding="utf-8")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(2)
            args = [str(Path(sys.executable).resolve()), "-I", "-B", "-S", str(probe),
                    str(visible), str(hidden), str(listener.getsockname()[1])]
            baseline = run_process(args, env=env, cwd=visible / "workspace")
            (hidden / "outside-write").unlink(missing_ok=True)
            sandboxed = run_process([*base, *args], env=env, cwd=visible / "workspace")
        isolation = classify_probe(_object(baseline["stdout"]), _object(sandboxed["stdout"]))
        guard = run_process([*base, str(visible / "shim/claude"), "--version"],
                            env={**env, "KIT_PILOT_GUARD_PROBE": "1"}, cwd=visible / "workspace")
        spawn_blocked = guard["returncode"] == 77
        checks.append({"condition": attempt["condition"], "authenticated": profile_passed,
                       "local_auth_status_passed": passed, "relay_authentication": profile_auth,
                       "returncode": result["returncode"], "timed_out": result["timed_out"],
                       "credentials_copied": False, "isolation": isolation,
                       "isolation_baseline": _object(baseline["stdout"]),
                       "isolation_sandboxed": _object(sandboxed["stdout"]),
                       "descendant_cli_blocked": spawn_blocked,
                       "profile_sha256": hashlib.sha256((visible / "pilot.sb").read_bytes()).hexdigest()})
    report = {"models_launched": 0, "checks": checks,
              "passed": bool(checks) and all(item["authenticated"] and item["local_auth_status_passed"]
                            and item["isolation"] == "passed" and item["descendant_cli_blocked"] for item in checks),
              "relay": provider.public(), "billing_mode": "api_relay",
              "live_authentication_verified": False,
              "relay_authentication_verified": bool(checks) and all(item["authenticated"] for item in checks),
              "scope": "protected relay catalog GET with negative control, native CLI status and local boundaries; inference not tested"}
    write_json(output / "host-check.json", report)
    return report


def receipt(stdout, task):
    """Allowlist telemetry; never persist raw authentication or assistant thinking."""
    frames = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    if not all(isinstance(frame, dict) for frame in frames):
        raise ValueError("CLI frames must be objects")
    initial = next((f for f in frames if f.get("type") == "system" and f.get("subtype") == "init"), {})
    final = next((f for f in reversed(frames) if f.get("type") == "result"), {})
    model_ids = sorted({f["message"]["model"] for f in frames
                        if f.get("type") == "assistant" and isinstance(f.get("message", {}).get("model"), str)
                        and not f["message"]["model"].startswith("<")})
    tools_disabled = "tools" in initial and initial["tools"] == [] and initial.get("mcp_servers") == []
    answer = None
    try:
        answer = task_eval._parse(final.get("result", ""))
    except (ValueError, TypeError):
        pass
    return {"init": {key: initial.get(key) for key in ("model", "claude_code_version", "tools", "mcp_servers", "plugins")},
            "observed_model_ids": model_ids,
            "hook_events": [{key: f.get(key) for key in ("subtype", "hook_event", "hook_name", "outcome", "exit_code")}
                            for f in frames if f.get("type") == "system" and str(f.get("subtype", "")).startswith("hook_")],
            "tools_disabled_verified": tools_disabled,
            "result_type": final.get("subtype"), "is_error": final.get("is_error"),
            "error_result_redacted": redact.redact(str(final.get("result", "")))[:4000] if final.get("is_error") else None,
            "errors_redacted": redact.data(final.get("errors")),
            "duration_ms": final.get("duration_ms"), "usage": final.get("usage"),
            "model_usage": final.get("modelUsage"), "estimated_cost_usd": final.get("total_cost_usd"),
            "answer": answer, "grade": task_eval.grade(task, answer),
            "protocol_passed": tools_disabled and bool(model_ids) and all("sonnet" in model.lower() for model in model_ids)
                               and not final.get("is_error", True)
                               and task_eval.grade(task, answer)["valid"]}


def execute(output, ledger, cc_switch_dir=None, *, conditions=CONDITIONS):
    if not conditions or len(set(conditions)) != len(conditions) or set(conditions) - set(CONDITIONS):
        raise ValueError("choose distinct pilot conditions")
    output = Path(output).resolve()
    if (output / "pilot-report.json").exists():
        raise FileExistsError("pilot already attempted; receipts must not be overwritten")
    plan = json.loads((output / "pilot.json").read_text(encoding="utf-8"))
    if plan.get("auth_source") == "cc-switch":
        raise ValueError("relay model execution requires a new explicit API budget phase; subscription grant cannot be reused")
    _, manifest, suite, manifest_digest = task_eval._load_run(output)
    if plan["sources"] != manifest["sources"]:
        raise ValueError("pilot source identity differs from frozen manifest")
    if hashlib.sha256(Path(plan["binary"]).read_bytes()).hexdigest() != plan["binary_sha256"]:
        raise ValueError("CLI binary changed; run a new no-model preflight")
    for name, source in plan["sources"].items():
        if source and task_eval.tree_hash(output / "sources" / name) != source["snapshot_sha256"]:
            raise ValueError("pinned plugin source changed")
    host = check_host(output, cc_switch_dir, conditions=conditions)
    if not host["passed"]:
        raise ValueError("host preflight failed; no CLI model session launched")
    task = next(t for t in suite["tasks"] if t["id"] == TASK)
    prompt = task_eval.prompt_for(task) + "\n当前无工具。请将 answer.json 的 JSON 正文直接作为最终回答，不写文件，不加 Markdown。"
    report = {"version": 1, "kind": "host_protocol_pilot", "learning_effect_evidence": False,
              "requested_model_alias": "sonnet", "cli_version": plan["cli_version"], "sources": plan["sources"],
              "manifest_sha256": manifest_digest, "suite_sha256": manifest["suite_sha256"], "host_check": host,
              "warmup_sessions": 0, "curation_sessions": 0, "tools_enabled": False,
              "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "attempts": []}
    write_json(output / "pilot-report.json", report)
    expected_models = None
    for attempt in plan["selected"]:
        if attempt["condition"] not in conditions:
            continue
        visible = output / attempt["visible"]
        source = output / "sources" / attempt["condition"] if attempt["condition"] != "no_learning" else None
        number = reserve(ledger, attempt["condition"], run_id=output.name)
        started = time.monotonic()
        result = run_process(command(plan["binary"], visible, source), env=environment(visible),
                             cwd=visible / "workspace", timeout=180, input_text=prompt)
        item = {"condition": attempt["condition"], "session_reservation": number,
                "returncode": result["returncode"], "timed_out": result["timed_out"],
                "wall_seconds": round(time.monotonic() - started, 3),
                "stdout_sha256": hashlib.sha256(result["stdout"].encode()).hexdigest(),
                "stderr_bytes": len(result["stderr"].encode()),
                "stderr_redacted": redact.redact(result["stderr"])[:4000],
                "blocked_descendant_attempts": len((visible / "config/blocked-spawns").read_text(encoding="utf-8").splitlines())
                if (visible / "config/blocked-spawns").exists() else 0}
        try:
            item.update(receipt(result["stdout"], task))
        except (ValueError, TypeError, KeyError):
            item["protocol_passed"] = False
            item["reason"] = "invalid CLI stream"
        if expected_models is None:
            expected_models = item.get("observed_model_ids")
        item["model_identity_matches"] = bool(expected_models) and item.get("observed_model_ids") == expected_models
        report["attempts"].append(item)
        write_json(output / "pilot-report.json", report)
        if result["returncode"] or not item["protocol_passed"] or not item["model_identity_matches"]:
            break
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "check", "execute"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--cc-switch-dir", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--conditions", choices=CONDITIONS, nargs="+", default=CONDITIONS)
    args = parser.parse_args()
    if args.action == "setup":
        result = setup(args.repo, args.output, cc_switch_dir=args.cc_switch_dir)
        print(json.dumps({"kind": result["kind"], "models_launched": 0, "selected": len(result["selected"])}))
    elif args.action == "check":
        result = check_host(args.output, args.cc_switch_dir)
        print(json.dumps(result))
        return 0 if result["passed"] else 1
    else:
        if args.ledger is None:
            parser.error("execute requires an existing approved --ledger")
        result = execute(args.output, args.ledger, args.cc_switch_dir, conditions=args.conditions)
        print(json.dumps({"attempts": len(result["attempts"]),
                          "protocol_passes": sum(a["protocol_passed"] for a in result["attempts"])}))


if __name__ == "__main__":
    raise SystemExit(main())

"""Single dispatcher: the one entry point for all the plugin's hook events, routing by event name to the matching `on_*`.

architecture §execution lives only at the hook global layer: `hooks.json` points only at this file, every
pipeline execution step enters through this one door and is dispatched by `hook_event_name`; unknown /
missing event names are safely ignored (fail-safe — a new host event must not crash this plugin). E6
platform contract pins the details:
- `Stop` = once per turn (S6) → on Stop, +1 each to both layers' request counters (the MNG-rate
  denominator), then run CAP triggering; `SubagentStop` (reflector done, S4) is not routed, not counted.
- `PreToolUse`: `Skill` → MNG numerator instrumentation (identity in `tool_input`, S5); `Read` into a
  managed symbol directory → view counter (not use), except reflector
  reads (library comparison, not use); reflector `agent_type` (carried in the S3 payload) on
  `Write`/`Edit` → deny (plugin top-level backstop — a plugin agent does not honor its own hooks, S1).
- Triggering reflection starts in a detached background job (host-detach: do not block the host Stop);
  the spawn CLI entry connects to the promoter.

Main-session tools also advance the project consolidation counter. Stop judges both
cadences; consolidation takes priority without resetting a deferred reflection.
"""
import hashlib
import json
import re
import subprocess
import sys
import uuid

from kitharness._compat import env_get

MIN_PYTHON = (3, 11)  # tomllib (lib/redact.py) entered the stdlib here; README badge and CI matrix pin the same floor


def _below_floor(version):
    return (f"KitHarness: needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+, got "
            f"{version[0]}.{version[1]} at {sys.executable}. Every hook is off for this session; "
            f"point the hooks.json commands at a newer interpreter.")


# this guard runs before the package imports on purpose: the chain below is module level end to end
# (dispatch -> promoter -> redact -> tomllib), so on an older interpreter the process dies at import
# and the fail-safe in dispatch() never gets the chance to catch it -- the host shows a generic hook
# error and nothing inside the plugin names the cause. Exit 0: a host hook must not fail the session.
if sys.version_info[:2] < MIN_PYTHON:
    print(_below_floor(sys.version_info), file=sys.stderr)
    raise SystemExit(0)

from kitharness import config  # noqa: E402
from kitharness.hook import (  # noqa: E402
    on_session_end,
    on_session_start,
    on_skill_call,
    on_stop,
    on_user_prompt,
    promoter,
)
from kitharness.lib import counters, layer, redact  # noqa: E402

_SANITIZE = re.compile(r"[^A-Za-z0-9_-]")
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_CHILD_ALLOWED_TOOLS = frozenset((
    "Read",
    "Grep",
    "Glob",
    "mcp__plugin_kitharness_stage_skill__stage_skill",
))


def _roots(roots):
    return roots or {lyr: layer.default_root(lyr) for lyr in layer.LAYERS}


def _run_id(result):
    raw = str(result.get("session_id") or "")
    sid = _SANITIZE.sub("", raw)
    if not sid:
        sid = hashlib.sha256(raw.encode()).hexdigest()[:8] if raw else "run"
    return f"{sid}-{result.get('count', 0)}-{uuid.uuid4().hex}"


def _curate_run_id(event, count):
    raw = str(event.get("session_id") or "")
    sid = _SANITIZE.sub("", raw)
    if not sid:
        sid = hashlib.sha256(raw.encode()).hexdigest()[:8] if raw else "run"
    return f"{sid}-c{count}-{uuid.uuid4().hex}"


def _is_reflector(event):
    at = str(event.get("agent_type") or "")
    return at == config.REFLECTOR_AGENT or at.endswith(":reflector")


def _detached_launch(transcript_path, session_id, run_id, roots):
    try:
        subprocess.Popen(  # host-detach: fire-and-forget so the Stop hook returns immediately
            [sys.executable, "-m", "kitharness.hook.spawn",
             str(transcript_path), str(session_id), run_id,
             str(roots[layer.PROJECT]), str(roots[layer.GLOBAL])],
            start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return {"error": f"detached_launch failed: {type(exc).__name__}: {exc}"}
    return None


def _reflect(event, result, roots, launch=None):
    transcript_path = event.get("transcript_path")
    if not transcript_path:
        return
    (launch or _detached_launch)(transcript_path, result.get("session_id", ""), _run_id(result), roots)


def _consolidate_launch(run_id, roots):
    subprocess.Popen(  # host-detach: same fire-and-forget as reflection; the curator reads the library, not the transcript
        [sys.executable, "-m", "kitharness.hook.spawn", "--curate", run_id,
         str(roots[layer.PROJECT]), str(roots[layer.GLOBAL])],
        start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _maybe_consolidate(event, roots, launch):
    launched = False
    try:
        with counters.consolidation_tick(config.CONSOLIDATE_EVERY_N, roots.get(layer.PROJECT)) as tick:
            if tick is None:
                return {"triggered": False}
            run_id = _curate_run_id(event, tick["count"])
            outcome = launch(run_id, roots)
            if isinstance(outcome, dict) and outcome.get("error"):
                raise RuntimeError(outcome["error"])
            launched = True
        return {"triggered": True, "run_id": run_id, "tool_calls": tick["count"],
                "watermark": tick["watermark"]}
    except Exception as exc:
        return {"triggered": launched, "error": redact.redact(f"{type(exc).__name__}: {exc}")}


def dispatch(event, *, roots=None, reflect=None, consolidate=None):
    if not isinstance(event, dict):
        return {"ignored": True, "reason": "non-object hook input"}
    name = event.get("hook_event_name")
    roots = _roots(roots)
    proot = roots.get(layer.PROJECT)
    fire = reflect or _reflect
    curate = consolidate or _consolidate_launch
    try:
        if name == "SessionStart":
            return {"handled": name, "result": on_session_start.on_session_start(event, roots=roots)}
        if name == "UserPromptSubmit":
            return {"handled": name, "result": on_user_prompt.on_user_prompt(event, roots=roots)}
        if name == "Stop":
            if env_get(config.CHILD_SESSION_ENV):
                return {"handled": name, "result": {"triggered": False, "reason": "recursion_guard"}}
            if not config.DISABLE_GLOBAL:
                counters.bump_request(layer.GLOBAL, roots.get(layer.GLOBAL))  # MNG denominator (per turn)
            counters.bump_request(layer.PROJECT, proot)
            drain_error = None
            try:
                promoter.drain(config.INTERACTIVE_RUN_ID, roots=roots)  # /learn and other in-session proposals; no-op when empty
            except Exception as exc:  # the queue stays for the next Stop; this turn still counts and reflects
                drain_error = f"{type(exc).__name__}: {exc}"
            consolidation = _maybe_consolidate(event, roots, curate)
            result = on_stop.on_stop(event, root=proot, defer=consolidation["triggered"])
            result["consolidation"] = consolidation
            if drain_error:
                result = {**result, "drain_error": drain_error}
            if result.get("triggered"):
                fire(event, result, roots)
            return {"handled": name, "result": result}
        if name == "SessionEnd":
            result = on_session_end.on_session_end(event, root=proot)
            if result.get("triggered"):
                fire(event, result, roots)
            return {"handled": name, "result": result}
        if name == "PreToolUse":
            tool = event.get("tool_name")
            child = bool(env_get(config.CHILD_SESSION_ENV))
            if child and tool not in _CHILD_ALLOWED_TOOLS:
                return {"deny": True, "reason": "KitHarness child may only read and stage intents"}
            if tool in _WRITE_TOOLS and _is_reflector(event):
                return {"deny": True, "reason": "reflector may only stage intents, not write files"}
            if not child:  # direction H: every main-session tool call advances the activity numerator
                sid = event.get("session_id")
                if isinstance(sid, str) and sid:
                    try:
                        counters.bump_session(sid, proot)
                        counters.bump_activity(proot)
                    except ValueError:
                        pass  # unsafe session id: skip counting, never crash the host hook
            if tool == "Skill":
                return {"handled": name, "result": on_skill_call.on_skill_call(event, roots=roots)}
            if tool == "Read":
                if child or _is_reflector(event):
                    return {"handled": name, "result": {"counted": False, "reason": "reflector_read"}}
                return {"handled": name, "result": on_skill_call.on_skill_read(event, roots=roots)}
            return {"ignored": True, "reason": "untracked PreToolUse"}
    except Exception as exc:  # fail-safe: a buggy handler must never crash the host hook
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"ignored": True, "reason": f"unrouted event: {name!r}"}


def _emit(verdict):
    if verdict.get("deny"):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": verdict.get("reason", "denied"),
        }}))
        return
    result = verdict.get("result") or {}
    if verdict.get("handled") in ("SessionStart", "UserPromptSubmit") and result.get("context"):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": verdict["handled"],
            "additionalContext": result["context"],
        }}))
        return
    handled = verdict.get("handled")
    if handled:
        print(json.dumps({"handled": handled, "result": result}), file=sys.stderr)
    elif verdict.get("error"):
        print(json.dumps({"error": verdict["error"]}), file=sys.stderr)  # a crashed handler is not silent


def main():
    # the host writes UTF-8 JSON, whatever the locale codec (cp1252 on Windows) is; output is ASCII JSON
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # malformed hook input → ignore, never crash the host
    _emit(dispatch(event))
    return 0


if __name__ == "__main__":
    sys.exit(main())

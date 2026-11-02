"""REF launch vehicle: deterministically assemble the reflector input → detached spawn a child session → connect to promoter.drain.

architecture line 29 / [reflector-subagent] / [cap]: at trigger time CAP gives "redacted episode window +
trigger cadence"; this step assembles the three pieces (window + the existing skill description index
(compare-first dedupe source, skips archived) + the single-source format_spec) and feeds them via
stdin to the cross-process reflector — nothing is persisted; the bundle lives only in the pipe. spawn sets CHILD_SESSION_ENV (recursion guard) +
injects run_id/root via env (stage_skill uses these to append back to the queue), then drains the intent
queue to disk after the child session ends. Authoring and landing are fully split across processes from
here: the reflector only appends intents, the promoter exclusively validates and lands.

ponytail: run() is the body of the "detached background job" (synchronous spawn→wait→drain); the "do not block the host Stop" detach is started in the background at the hook top level by the Phase 7 dispatch calling run(). spawn_fn is injectable (system tests use a fake reflector script in place of the real claude). Precise handling of the transcript upper-bound race (cap.md open) is still tolerated at v0.
"""
import json
import logging
import os
import sys
import tarfile
from pathlib import Path

from autoharness import config
from autoharness.hook import capture, jobs, promoter, recovery
from autoharness.lib import (
    atomic,
    counters,
    intent_queue,
    layer,
    redact,
    sidecar,
    skill_store,
    validate,
)

log = logging.getLogger(__name__)


def description_index(roots=None, *, agent_only=False):
    roots = roots or {}
    lines = []
    for lyr in config.active_layers():
        root = roots.get(lyr)
        skills = layer.skills_dir(lyr, root)
        if not skills.exists():
            continue
        for path in sorted(skills.glob(f"*/{skill_store.SKILL_FILE}")):
            symbol = path.parent.name
            if agent_only and not sidecar.is_agent_created(lyr, symbol, root):
                continue  # curator only ever sees its own skills; native/user stay out of the pool
            # anyone's skill can sit here; a latin-1 SKILL.md must not take the whole reflection down
            fm = validate._frontmatter(path.read_text(encoding="utf-8", errors="replace")) or {}
            name = fm.get("name") or symbol
            desc = fm.get("description") or "(no description)"
            lines.append(f"- {name} [{lyr}]: {desc}")
    return "\n".join(lines) if lines else "(no live skills yet)"


def build_bundle(window, index, spec, digest=""):
    preamble = (
        "# Prior context digest (older exchanges, tool outputs omitted — background only,"
        " never an evidence source)\n\n" + digest + "\n\n"
    ) if digest else ""
    return (
        preamble
        + "# Episode window (redacted)\n\n" + window
        + "\n\n# Existing skills (compare-first: dedupe / patch / where)\n\n" + index
        + "\n\n# Authoring + format spec (write to satisfy this)\n\n" + spec + "\n"
    )


def build_curator_bundle(index, spec):
    # The curator consolidates the whole agent-authored library, not one episode — so no window.
    return (
        "# Agent-authored skills (consolidate: merge narrow siblings into class-level umbrellas)\n\n"
        + index
        + "\n\n# Authoring + format spec (merged skills must satisfy this)\n\n" + spec + "\n"
    )


# Fork-carrier reflect instruction (direction G): arrives as the -p prompt (a fresh user turn on the
# forked conversation) — never as --agent/--system-prompt, which would invalidate the parent prefix.
# Carries the F1 reconcile duty: a new rule must supersede contradicting old statements.
FORK_INSTRUCTION = (
    "KitHarness reflection pass (forked session: the conversation above is your evidence source).\n"
    "Compare-first against the skill index below: prefer patching an existing skill over creating a\n"
    "new one; distill only class-level reusable lessons, never session narratives. When you distill\n"
    "a new rule or preference, search the managed skill trees for overlapping or contradicting\n"
    "statements and stage updates so the new supersedes the old. Propose every change exclusively\n"
    "via the stage_skill tool — never write files directly. Evidence must quote this session\n"
    "verbatim. If nothing is worth keeping, stage nothing.\n"
)


def build_fork_command(*, session_id, claude_bin):
    return [claude_bin, "-p", "--resume", str(session_id), "--fork-session",
            "--dangerously-skip-permissions", *_budget_args()]


def build_fork_prompt(index, spec):
    return (FORK_INSTRUCTION
            + "\n# Existing skills (compare-first: dedupe / patch / where)\n\n" + index
            + "\n\n# Authoring + format spec (write to satisfy this)\n\n" + spec + "\n")


def build_command(*, agent, claude_bin):
    # Reflection is an unattended background job — nobody is there to approve tool calls, so skip the
    # permission prompt. The security boundary is held by the agent's tools allowlist
    # (Read/Grep/Glob/stage_skill) + the top-level PreToolUse write backstop, not by the prompt.
    # (live e2e: a reflector's real stage_skill call gets blocked by the permission gate and can only
    # "narrate"; only with this flag does it land.)
    return [claude_bin, "-p", "--agent", agent, "--dangerously-skip-permissions", *_budget_args()]


def _budget_args():
    return ["--output-format", "json", "--max-budget-usd", str(config.JOB_MAX_BUDGET_USD)]


def child_env(run_id, root, *, base_env=None):
    env = dict(os.environ if base_env is None else base_env)
    env[config.CHILD_SESSION_ENV] = "1"
    env[config.RUN_ID_ENV] = run_id
    env[config.PROJECT_ROOT_ENV] = str(root)
    return env


def _spawn_error(proc, argv):
    """Return bounded, redacted child diagnostics safe for the run account."""
    stderr = redact.redact(str(proc.stderr or "")).strip()[-2000:]
    return {
        "argv0": redact.redact(str(argv[0])) if argv else None,
        "returncode": proc.returncode,
        "stderr_tail": stderr,
    }


def _detached_spawn(argv, env, bundle):
    """Run the reflector child to completion; report a crash on stderr instead of discarding it."""
    proc = jobs.execute(argv, env, bundle)
    if proc.returncode != 0:
        error = _spawn_error(proc, argv)
        print(f"background child {error['argv0']} exited {proc.returncode}: "
              f"{error['stderr_tail']}", file=sys.stderr)
    return proc


def _record_job(run_id, roots, proc, argv, *, deferred=None):
    """Merge bounded diagnostics and numeric usage into the existing run account."""
    if proc is None and deferred is None:
        return
    state = layer.state_dir(layer.PROJECT, roots.get(layer.PROJECT))
    runs = state / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    account = runs / f"{run_id}.json"
    record = json.loads(account.read_text(encoding="utf-8")) if account.exists() else {"run_id": run_id}
    record["job"] = {"status": deferred or ("success" if proc.returncode == 0 else "failed"),
                     "timeout_s": config.JOB_TIMEOUT_S, "max_budget_usd": config.JOB_MAX_BUDGET_USD,
                     "elapsed_ms": getattr(proc, "elapsed_ms", None), "usage": jobs.usage(proc)}
    if proc is not None and proc.returncode != 0:
        record["spawn_error"] = _spawn_error(proc, argv)
    atomic.write_text(account, json.dumps(record, ensure_ascii=False, indent=2))


def _execute(run_id, roots, argv, payload, repo_name, spawn_fn, *, snapshot=False):
    proot = roots.get(layer.PROJECT)
    with jobs.slots(roots) as acquired:
        if not acquired:
            jobs.cancel(run_id, proot)
            _record_job(run_id, roots, None, argv, deferred="concurrency_limit")
            raise jobs.Deferred("concurrency limit; transcript offset retained")
        if snapshot:
            try:
                _snapshot_skills(run_id, roots)
            except OSError:
                log.warning("could not snapshot library before curation")
            except Exception:
                log.exception("unexpected snapshot error; curator running without safety net")
        with intent_queue.live_lock(run_id, proot):
            proc = (spawn_fn or _detached_spawn)(argv, child_env(run_id, proot), payload)
            response = jobs.response(proc)
            deferred = ("timed_out" if getattr(proc, "timed_out", False) else
                        "launch_failed" if getattr(proc, "launch_failed", False) else
                        "budget_exhausted" if response.get("subtype") == "error_max_budget_usd" else
                        "child_failed" if getattr(proc, "returncode", 0) != 0 or response.get("is_error") is True else None)
            if deferred:
                jobs.cancel(run_id, proot)
                _record_job(run_id, roots, proc, argv, deferred=deferred)
                raise jobs.Deferred(f"{deferred}; partial proposals quarantined")
            try:
                verdicts = promoter.drain(run_id, roots=roots, repo_name=repo_name)
            finally:
                _record_job(run_id, roots, proc, argv)
        return verdicts


def run(window_text, run_id, *, roots, repo_name=None, agent=None, claude_bin=None,
        spec_path=None, digest="", session_id=None, carrier=None, spawn_fn=None):
    roots = roots or {}
    spec = (spec_path or config.FORMAT_SPEC).read_text(encoding="utf-8")

    carrier = carrier or config.REFLECTOR_CARRIER
    if carrier == "fork" and session_id:  # no session to fork -> bundle chain (fail-safe)
        argv = build_fork_command(session_id=session_id, claude_bin=claude_bin or config.CLAUDE_BIN)
        payload = build_fork_prompt(description_index(roots), spec)  # -p reads the prompt from stdin
    else:
        argv = build_command(agent=agent or config.REFLECTOR_AGENT,
                             claude_bin=claude_bin or config.CLAUDE_BIN)
        payload = build_bundle(window_text, description_index(roots), spec, digest=digest)

    # the live lock marks the run as having a spawn parent: between here and the own drain below,
    # a foreign drain (the main session's Stop) leaves this queue alone instead of landing the
    # child's intents mid-flight or racing its appends
    return _execute(run_id, roots, argv, payload, repo_name, spawn_fn)


def _snapshot_skills(run_id, roots):
    """Pre-run library snapshot (direction E, mirrors Hermes): covers the one risk atomic landing
    and reversible archiving cannot — a whole curator run writing the library wrong. Recovery is a
    manual unpack; rotation keeps SNAPSHOT_KEEP per layer."""
    snapdir = layer.state_dir(layer.PROJECT, roots.get(layer.PROJECT)) / "snapshots"
    snapdir.mkdir(parents=True, exist_ok=True)
    for lyr in config.active_layers():
        skills = layer.skills_dir(lyr, roots.get(lyr))
        if not skills.exists():
            continue
        with tarfile.open(snapdir / f"{run_id}-{lyr}.tar.gz", "w:gz") as tar:
            tar.add(skills, arcname="skills")
        kept = sorted(snapdir.glob(f"*-{lyr}.tar.gz"), key=lambda p: p.stat().st_mtime)
        for old in kept[: max(0, len(kept) - config.SNAPSHOT_KEEP)]:
            old.unlink()


def run_curator(run_id, *, roots, repo_name=None, agent=None, claude_bin=None,
                spec_path=None, spawn_fn=None):
    roots = roots or {}
    spec = (spec_path or config.FORMAT_SPEC).read_text(encoding="utf-8")
    bundle = build_curator_bundle(description_index(roots, agent_only=True), spec)

    argv = build_command(agent=agent or config.CURATOR_AGENT,
                         claude_bin=claude_bin or config.CLAUDE_BIN)
    return _execute(run_id, roots, argv, bundle, repo_name, spawn_fn, snapshot=True)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--curate":
        run_id, proot, groot = argv[1:]
        owner = recovery.track(run_id, Path(proot))
        try:
            result = run_curator(run_id, roots={layer.PROJECT: Path(proot), layer.GLOBAL: Path(groot)})
        except jobs.Deferred:
            return []
        recovery.complete(owner)
        return result
    transcript_path, session_id, run_id, proot, groot = argv
    roots = {layer.PROJECT: Path(proot), layer.GLOBAL: Path(groot)}
    owner = recovery.track(run_id, roots[layer.PROJECT])
    offset = counters.session_offset(session_id, roots[layer.PROJECT])
    window_text, new_offset = capture.window(transcript_path, offset)
    try:
        result = run(window_text, run_id, roots=roots, session_id=session_id,
                     digest=capture.digest(transcript_path, offset))
    except jobs.Deferred:
        return []
    counters.write_session_offset(session_id, new_offset, roots[layer.PROJECT])
    recovery.complete(owner)
    return result


if __name__ == "__main__":
    main()

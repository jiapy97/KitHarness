"""Pinned real-model study with one shared budget, explicit maintenance and frozen libraries.

Preparation/checks never invoke a model. Only an existing user-approved API grant
permits run. Synthetic smoke results must never be substituted for these receipts.
"""
import argparse
import hashlib
import json
import shlex
import shutil
import sys
import time
from pathlib import Path

from autoharness.lib import live_accounting as accounting
from autoharness.lib import live_auth, live_pilot, redact, task_eval

MODEL = accounting.MODEL
STAGE_TOOL = "mcp__plugin_autoharness_stage_skill__stage_skill"
READ_TOOLS = ("Read", "Grep", "Glob")
ALLOWANCE = {"pilot": 0.1, "warmup": 0.1, "reflector": 0.75, "curator": 0.75, "evaluation": 0.15}
SOLVER_SYSTEM = "Solve the supplied synthetic workflow task. You may read relevant learned skills. Return only the requested JSON object. Do not modify files."
WARMUP_SYSTEM = "Read the supplied workflow lesson. Return a short JSON object with keys lesson and pitfall. Do not create or modify any files."


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    accounting.write(path, value)


def _source(root, condition):
    return root / "sources" / condition if condition != "no_learning" else None


def _visible(root, condition, kind="training"):
    return root / kind / condition


def install_context(visible, binary, source, port):
    visible.mkdir(parents=True, exist_ok=True)
    for name in ("workspace/.claude/skills", "global-config/skills", "shim", "sessions"):
        (visible / name).mkdir(parents=True, exist_ok=True)
    python = visible / "shim/python3"
    python.write_text('#!/bin/sh\nexec ' + shlex.quote(str(Path(sys.executable).resolve())) + ' "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    blocked = visible / "shim/claude"
    blocked.write_text('#!/bin/sh\nprintf "blocked descendant CLI\\n" >> "$CLAUDE_CONFIG_DIR/blocked-spawns"\nexit 77\n', encoding="utf-8")
    blocked.chmod(0o755)
    (visible / "worker.py").write_bytes(Path(__file__).with_name("live_study_worker.py").read_bytes())
    (visible / "pilot.sb").write_text(live_pilot.pilot_profile(visible, binary, source, relay_port=port), encoding="utf-8")
    save(visible / "settings.json", {"enabledPlugins": {}, "autoUpdatesChannel": "stable"})


def prepare(repo, output, *, cc_switch_dir=None, credential_mode="configured", deepseek_env=None):
    output = Path(output).resolve()
    provider = live_auth.load_deepseek_project(deepseek_env) if deepseek_env else live_auth.load_cc_switch(cc_switch_dir)
    model = provider.model or MODEL
    binary = Path(shutil.which("claude") or "/unavailable").resolve(strict=True)
    manifest = task_eval.prepare(repo, Path(repo) / "evals/tasks/suite.json", output, model=model)
    output.chmod(0o700)
    for kind in ("training", "pilot"):
        for condition in task_eval.CONDITIONS:
            install_context(_visible(output, condition, kind), binary, _source(output, condition), provider.port)
    (output / "receipts").mkdir()
    (output / "frozen").mkdir()
    plan = {"version": 1, "kind": "controlled_live_three_arm", "model": model,
            "sources": manifest["sources"], "binary": str(binary), "binary_sha256": digest(binary.read_bytes()),
            "cc_switch_dir": str(Path(cc_switch_dir or Path.home() / ".cc-switch").resolve()), "relay": provider.public(),
            "roles": {"warmup": 18, "reflector": 2, "curator": 2, "evaluation": 18, "pilot": 3},
            "maintenance": "one explicit batch reflection then curation per learning arm; original role bodies and production MCP/promoter",
            "model_override": "all roles use the same explicit model, rather than agent frontmatter defaults",
            "allowances_usd": ALLOWANCE, "planned_cli_allocations_usd": 7.8,
            "sampling": "one repetition; not a statistical significance claim"}
    if credential_mode not in ("configured", "api_key"):
        raise ValueError("invalid native CLI credential mode")
    plan["native_cli_credential_mode"] = credential_mode
    if deepseek_env:
        plan["deepseek_env_file"] = str(Path(deepseek_env).resolve())
        plan["cost_basis"] = "official DeepSeek Flash peak USD rates plus independently observed account balance change"
    save(output / "study.json", plan)
    save(output / "progress.json", {"pilot": [], "warmup": [], "maintenance": [], "evaluation": [], "frozen": {}})
    return plan


def context_env(visible, call_id, provider=None, *, source=None, child=False, credential_mode="configured"):
    session = visible / "sessions" / call_id
    for name in ("config", "tmp", "state", "cache"):
        (session / name).mkdir(parents=True, exist_ok=True)
    env = live_pilot.environment(visible, provider)
    if provider and credential_mode == "api_key":
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env["ANTHROPIC_API_KEY"] = provider.env[provider.credential_key]
    env.update(CLAUDE_CONFIG_DIR=str(session / "config"), TMPDIR=str(session / "tmp"),
               CLAUDE_CODE_TMPDIR=str(session / "tmp"), XDG_STATE_HOME=str(session / "state"),
               XDG_CACHE_HOME=str(session / "cache"), CLAUDE_CODE_MAX_OUTPUT_TOKENS="2048",
               AUTOHARNESS_CONSOLIDATE_EVERY_N="0", AUTOHARNESS_REFLECT_EVERY_N="1000000",
               AUTOHARNESS_RUN_ID=call_id)
    if source:
        env["PYTHONPATH"] = str(source / "src")
    if child:
        env["AUTOHARNESS_CHILD_SESSION"] = "1"
    return env


def worker(visible, pinned_source, operation, **payload):
    env = context_env(visible, "controller", source=pinned_source, child=True)
    result = live_pilot.run_process(["/usr/bin/sandbox-exec", "-f", str(visible / "pilot.sb"),
                                    str(Path(sys.executable).resolve()), str(visible / "worker.py")],
                                   env=env, cwd=visible / "workspace", timeout=30,
                                   input_text=json.dumps({"operation": operation, **payload}))
    if result["returncode"]:
        raise ValueError("pinned production helper failed: " + redact.redact(result["stderr"])[-1500:])
    return json.loads(result["stdout"])


def mcp_config(visible, source, call_id):
    env = {"PYTHONPATH": str(source / "src"), "AUTOHARNESS_PROJECT_ROOT": str(visible / "workspace/.claude"),
           "AUTOHARNESS_GLOBAL_ROOT": str(visible / "global-config"), "AUTOHARNESS_DISABLE_GLOBAL": "1",
           "AUTOHARNESS_CHILD_SESSION": "1", "AUTOHARNESS_RUN_ID": call_id,
           "ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": "", "ANTHROPIC_BASE_URL": ""}
    value = {"mcpServers": {"plugin_autoharness_stage_skill": {
        "command": str(Path(sys.executable).resolve()), "args": ["-m", "autoharness.stage_skill.server"], "env": env}}}
    path = visible / "sessions" / call_id / "mcp.json"
    save(path, value)
    return path


def command(plan, visible, source, stage, call_id, system):
    argv = live_pilot.command(plan["binary"], visible, source)
    argv[argv.index("--model") + 1] = plan.get("model", MODEL)
    argv[argv.index("--max-budget-usd") + 1] = str(ALLOWANCE[stage])
    # Keep the native client's default prompt/identity; custom role rules append.
    argv[argv.index("--system-prompt")] = "--append-system-prompt"
    argv[argv.index("--append-system-prompt") + 1] = system
    maintenance = stage in ("reflector", "curator")
    tools = list(READ_TOOLS) if maintenance or stage == "evaluation" else []
    argv[argv.index("--tools") + 1] = ",".join(tools)
    allowed = tools + ([STAGE_TOOL] if maintenance else [])
    if allowed:
        argv.extend(["--allowedTools", ",".join(allowed)])
    if maintenance:
        argv[argv.index("--mcp-config") + 1] = str(mcp_config(visible, source, call_id))
    argv.extend(["--max-turns", "12" if tools else "2"])
    return argv, set(allowed)


def parse_stream(stdout):
    frames = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    if not all(isinstance(frame, dict) for frame in frames):
        raise ValueError("invalid native CLI stream")
    initial = next((f for f in frames if f.get("type") == "system" and f.get("subtype") == "init"), {})
    final = next((f for f in reversed(frames) if f.get("type") == "result"), {})
    models, tools, reads = set(), [], []
    for frame in frames:
        if frame.get("type") != "assistant":
            continue
        message = frame.get("message", {})
        model = message.get("model")
        if isinstance(model, str) and not model.startswith("<"):
            models.add(model)
        for block in message.get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tools.append(block.get("name"))
                if block.get("name") == "Read":
                    reads.append(block.get("input", {}).get("file_path"))
    return frames, initial, final, sorted(models), tools, reads


def safe_frames(frames, provider):
    cleaned = []
    for frame in frames:
        frame = json.loads(json.dumps(frame))
        message = frame.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), list):
            message["content"] = [block for block in message["content"] if not isinstance(block, dict)
                                  or block.get("type") not in ("thinking", "redacted_thinking")]
        cleaned.append(redact.data(json.loads(provider.redact(json.dumps(frame, ensure_ascii=True)))))
    return cleaned


def call(root, plan, grant, provider, *, call_id, stage, condition, visible, prompt, system):
    path = root / "receipts" / (call_id + ".json")
    if path.exists():
        raise FileExistsError("model receipt already exists")
    if len(prompt.encode("utf-8")) > 200_000:
        raise ValueError("model input exceeds study context cap")
    source = _source(root, condition)
    env = context_env(visible, call_id, provider, source=source, child=stage in ("reflector", "curator"),
                      credential_mode=plan.get("native_cli_credential_mode", "configured"))
    argv, allowed = command(plan, visible, source, stage, call_id, system)
    current = accounting.relay_state(provider)
    budget_call_id = root.name + ":" + call_id
    reservation = accounting.reserve(grant, call_id=budget_call_id, stage=stage, condition=condition,
                                     allowance_usd=ALLOWANCE[stage], prompt_sha256=digest(prompt.encode()),
                                     provider_identity=provider.public(), current=current)
    started = time.monotonic()
    result = live_pilot.run_process(argv, env=env, cwd=visible / "workspace",
                                    timeout=600 if stage in ("reflector", "curator") else 180, input_text=prompt)
    try:
        frames, initial, final, models, tools, reads = parse_stream(result["stdout"])
    except (ValueError, TypeError, KeyError):
        frames, initial, final, models, tools, reads = [], {}, {}, [], [], []
    maintenance = stage in ("reflector", "curator")
    advertised = set(initial.get("tools", []))
    connected = initial.get("mcp_servers", [])
    tools_valid = advertised == allowed and set(tools) <= allowed
    mcp_valid = (any(item.get("name") == "plugin_autoharness_stage_skill" and item.get("status") == "connected"
                     for item in connected) if maintenance else connected == [])
    ok = (result["returncode"] == 0 and final.get("subtype") == "success" and final.get("is_error") is False
          and models == [plan["model"]] and tools_valid and mcp_valid)
    raw_answer = final.get("result")
    if isinstance(raw_answer, str) and provider.redact(raw_answer) != raw_answer:
        ok = False
    accounted_cost = (accounting.deepseek_peak_estimate(final.get("usage")) if provider.source == "deepseek_project"
                      else final.get("total_cost_usd"))
    receipt = {"call_id": call_id, "stage": stage, "condition": condition, "reservation": reservation["number"],
               "model": plan["model"], "observed_model_ids": models, "success": ok, "returncode": result["returncode"],
               "timed_out": result["timed_out"], "wall_seconds": round(time.monotonic() - started, 3),
               "duration_ms": final.get("duration_ms"), "estimated_usd": accounted_cost,
               "native_cli_estimated_usd": final.get("total_cost_usd"),
               "cost_basis": "DeepSeek published peak rates" if provider.source == "deepseek_project" else "native CLI estimate",
               "usage": final.get("usage"), "model_usage": final.get("modelUsage"),
               "advertised_tools": sorted(advertised), "used_tools": tools, "read_paths": reads,
               "mcp_servers": connected, "tools_verified": tools_valid, "mcp_verified": mcp_valid,
               "result": final.get("result"), "is_error": final.get("is_error"), "subtype": final.get("subtype"),
               "stderr_redacted": redact.redact(provider.redact(result["stderr"]))[-3000:],
               "stdout_sha256": digest(result["stdout"].encode()), "prompt_sha256": digest(prompt.encode()),
               "system_sha256": digest(system.encode()),
               "hook_events": [{key: f.get(key) for key in ("subtype", "hook_event", "hook_name", "outcome", "exit_code")}
                               for f in frames if f.get("type") == "system" and str(f.get("subtype", "")).startswith("hook_")]}
    receipt = redact.data(json.loads(provider.redact(json.dumps(receipt, ensure_ascii=True))))
    save(path, receipt)
    safe = safe_frames(frames, provider)
    (root / "receipts" / (call_id + ".jsonl")).write_text("".join(json.dumps(frame, ensure_ascii=True) + "\n" for frame in safe), encoding="utf-8")
    reconciled = accounting.finish(grant, call_id=budget_call_id, estimated_usd=accounted_cost,
                                   current=accounting.relay_state(provider), successful=ok, receipt_sha256=digest(path.read_bytes()),
                                   estimate_basis="published_deepseek_peak_rates" if provider.source == "deepseek_project" else "native_cli")
    print(json.dumps({"call": call_id, "success": ok, "estimated_usd": accounted_cost,
                      "relay_normalized_units": accounting.summary(grant)["relay_normalized_units"]}), flush=True)
    if not ok or reconciled["halt_reason"]:
        raise ValueError("model call or budget guard stopped study; see retained receipt")
    # Grade synthetic data before generic redaction can rewrite literal fixture keys.
    return {**receipt, "result": raw_answer}


def _load(root):
    root = Path(root).resolve()
    plan = json.loads((root / "study.json").read_text(encoding="utf-8"))
    _, manifest, suite, _ = task_eval._load_run(root)
    if plan["model"] not in accounting.SUPPORTED_MODELS or manifest["model"] != plan["model"] or digest(Path(plan["binary"]).read_bytes()) != plan["binary_sha256"]:
        raise ValueError("model or CLI identity changed")
    for condition, identity in plan["sources"].items():
        if identity and task_eval.tree_hash(root / "sources" / condition) != identity["snapshot_sha256"]:
            raise ValueError("pinned plugin source changed")
    provider = (live_auth.load_deepseek_project(plan["deepseek_env_file"]) if plan.get("deepseek_env_file")
                else live_auth.load_cc_switch(plan["cc_switch_dir"]))
    if provider.public() != plan["relay"]:
        raise ValueError("relay configuration changed")
    progress = json.loads((root / "progress.json").read_text(encoding="utf-8"))
    return root, plan, manifest, suite, provider, progress


def pilot(root, grant):
    root, plan, _, suite, provider, progress = _load(root)
    task = next(t for t in suite["tasks"] if t["id"] == "utc-boundaries")
    prompt = task_eval.prompt_for(task) + "\n无工具。直接输出 answer.json 的 JSON 正文，不使用 Markdown。"
    for condition in task_eval.CONDITIONS:
        if condition in progress["pilot"]:
            continue
        result = call(root, plan, grant, provider, call_id="pilot-" + condition, stage="pilot", condition=condition,
                      visible=_visible(root, condition, "pilot"), prompt=prompt, system=SOLVER_SYSTEM)
        grade = task_eval.grade(task, task_eval._parse(result["result"]))
        save(root / "receipts" / ("pilot-" + condition + "-grade.json"), grade)
        progress["pilot"].append(condition)
        save(root / "progress.json", progress)
    return progress


def library_hash(project, global_root, *, content_only=False):
    state = hashlib.sha256()
    for layer, base in (("project", Path(project)), ("global", Path(global_root))):
        for path in sorted(base.rglob("*")):
            if path.is_symlink():
                raise ValueError("linked library file")
            if not path.is_file():
                continue
            relative = path.relative_to(base)
            if content_only and (not relative.parts or relative.parts[0] != "skills" or path.name == ".sidecar.json"):
                continue
            state.update((layer + "/" + str(relative)).encode() + b"\0" + path.read_bytes() + b"\0")
    return state.hexdigest()


def run(root, grant):
    root, plan, manifest, suite, provider, progress = _load(root)
    if len(progress["pilot"]) != 3:
        raise ValueError("complete the three-arm model pilot first")
    for index, task in enumerate(suite["tasks"]):
        rotated = task_eval.CONDITIONS[index % 3:] + task_eval.CONDITIONS[:index % 3]
        for condition in rotated:
            call_id = "warmup-" + condition + "-" + task["id"]
            if call_id in progress["warmup"]:
                continue
            prompt = "阅读这条工作流经验，用 JSON 总结一条规则和一个易错点。不要创建技能文件。\n" + task["warmup"]
            call(root, plan, grant, provider, call_id=call_id, stage="warmup", condition=condition,
                 visible=_visible(root, condition), prompt=prompt, system=WARMUP_SYSTEM)
            progress["warmup"].append(call_id)
            save(root / "progress.json", progress)
    for condition in ("original", "kitharness"):
        visible, source = _visible(root, condition), _source(root, condition)
        for role in ("reflector", "curator"):
            call_id = role + "-" + condition
            if call_id in progress["maintenance"]:
                continue
            episodes = []
            for task in suite["tasks"]:
                result = json.loads((root / "receipts" / ("warmup-" + condition + "-" + task["id"] + ".json")).read_text(encoding="utf-8"))
                episodes.extend([{"type": "user", "message": {"role": "user", "content": task["warmup"]}},
                                 {"type": "assistant", "message": {"role": "assistant", "content": result["result"]}}])
            bundle = worker(visible, source, "bundle", role=role, run_id=call_id, source=str(source),
                            window="\n".join(json.dumps(e, ensure_ascii=False) for e in episodes))
            call(root, plan, grant, provider, call_id=call_id, stage=role, condition=condition, visible=visible,
                 prompt=bundle["prompt"], system=bundle["system"])
            verdicts = worker(visible, source, "drain", run_id=call_id)
            save(root / "receipts" / (call_id + "-promotion.json"), verdicts)
            inventory = worker(visible, source, "inventory")
            save(root / "receipts" / (call_id + "-inventory.json"), inventory)
            progress["maintenance"].append(call_id)
            save(root / "progress.json", progress)
    for condition in task_eval.CONDITIONS:
        if condition in progress["frozen"]:
            continue
        visible = _visible(root, condition)
        frozen = root / "frozen" / condition
        frozen.mkdir()
        shutil.copytree(visible / "workspace/.claude", frozen / "project")
        shutil.copytree(visible / "global-config", frozen / "global")
        progress["frozen"][condition] = {"library_sha256": library_hash(frozen / "project", frozen / "global"),
                                          "content_sha256": library_hash(frozen / "project", frozen / "global", content_only=True)}
        save(root / "progress.json", progress)
    for attempt in manifest["attempts"]:
        if attempt["id"] in progress["evaluation"]:
            continue
        condition = attempt["condition"]
        frozen = root / "frozen" / condition
        identity = progress["frozen"][condition]
        if library_hash(frozen / "project", frozen / "global") != identity["library_sha256"]:
            raise ValueError("frozen library drift")
        visible = (root / attempt["workspace"]).parent
        install_context(visible, plan["binary"], _source(root, condition), provider.port)
        shutil.copytree(frozen / "project", visible / "workspace/.claude", dirs_exist_ok=True)
        shutil.copytree(frozen / "global", visible / "global-config", dirs_exist_ok=True)
        if library_hash(visible / "workspace/.claude", visible / "global-config") != identity["library_sha256"]:
            raise ValueError("library copy does not match frozen identity")
        task = next(t for t in suite["tasks"] if t["id"] == attempt["task"])
        prompt = task_eval.prompt_for(task) + "\n只读工具可查阅相关技能。直接输出 answer.json 的 JSON 正文，不写文件，不使用 Markdown。"
        result = call(root, plan, grant, provider, call_id="evaluation-" + attempt["id"], stage="evaluation", condition=condition,
                      visible=visible, prompt=prompt, system=SOLVER_SYSTEM)
        if library_hash(visible / "workspace/.claude", visible / "global-config", content_only=True) != identity["content_sha256"]:
            raise ValueError("evaluation modified learned skill contents")
        # Retain invalid JSON as an invalid artifact, so it scores zero without
        # losing its cost receipt or aborting the remaining paired attempts.
        (visible / "workspace/answer.json").write_text(result["result"] or "", encoding="utf-8")
        usage = result.get("usage") or {}
        task_eval.record(root, attempt["id"], metadata={"model": plan["model"], "library_sha256": identity["library_sha256"],
                         "duration_ms": result["duration_ms"], "total_cost_usd": result["estimated_usd"],
                         **{key: usage.get(key) for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}})
        progress["evaluation"].append(attempt["id"])
        save(root / "progress.json", progress)
    report = task_eval.report(root)
    report.update(kind="controlled_live_three_arm", host_protocol={"model_verified": True, "same_model_all_roles": plan["model"],
                  "batch_maintenance": True, "warmup_sessions": 18, "reflector_sessions": 2, "curator_sessions": 2,
                  "frozen_libraries": progress["frozen"], "no_feedback_between_evaluation_tasks": True},
                  all_phase_accounting=accounting.summary(grant))
    save(root / "live-report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "pilot", "run", "publish"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--grant", type=Path)
    parser.add_argument("--cc-switch-dir", type=Path)
    parser.add_argument("--deepseek-env", type=Path)
    parser.add_argument("--publish-dir", type=Path)
    parser.add_argument("--credential-mode", choices=("configured", "api_key"), default="configured")
    args = parser.parse_args()
    if args.action == "prepare":
        plan = prepare(args.repo, args.output, cc_switch_dir=args.cc_switch_dir, credential_mode=args.credential_mode,
                       deepseek_env=args.deepseek_env)
        print(json.dumps({"kind": plan["kind"], "planned_sessions": plan["roles"], "models_called": 0}))
    elif args.action == "publish":
        from autoharness.lib.live_study_report import publish
        if args.grant is None or args.publish_dir is None:
            parser.error("publish requires --grant and a new --publish-dir")
        result = publish(args.output, args.grant, args.publish_dir)
        print(json.dumps({"published": str(args.publish_dir), "model": result["model"], "conditions": result["conditions"]}))
    else:
        if args.grant is None:
            parser.error("a user-approved --grant is required")
        result = pilot(args.output, args.grant) if args.action == "pilot" else run(args.output, args.grant)
        print(json.dumps({"completed": args.action, "report_written": args.action == "run", "progress_items": len(result)}))


if __name__ == "__main__":
    main()

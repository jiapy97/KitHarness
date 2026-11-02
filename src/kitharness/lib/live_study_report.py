"""Verify and export completed host evidence, never execute models."""
import io
import json
import tarfile
from pathlib import Path, PurePosixPath

from kitharness.lib import live_accounting as accounting
from kitharness.lib import live_study as study


def contexts(frames):
    """Measure actual stdout additionalContext, not the stderr diagnostic intent."""
    sizes = []
    for frame in frames:
        if frame.get("type") != "system" or frame.get("subtype") != "hook_response":
            continue
        try:
            output = json.loads(frame.get("stdout", ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(output, dict):
            continue
        context = output.get("hookSpecificOutput", {}).get("additionalContext")
        if isinstance(context, str):
            sizes.append({"event": frame.get("hook_event"), "bytes": len(context.encode("utf-8"))})
    return sizes


def checked_files(root, paths, secrets):
    contents, manifest = [], []
    for path in sorted(set(paths)):
        if path.is_symlink() or not path.is_file():
            raise ValueError("unsafe evidence file")
        raw = path.read_bytes()
        if any(key in raw for key in secrets):
            raise ValueError("credential detected in export; publication stopped")
        name = str(path.relative_to(root))
        contents.append((name, raw))
        manifest.append({"path": name, "sha256": study.digest(raw), "bytes": len(raw)})
    return contents, manifest


def verify_complete(root):
    root, plan, manifest, _, provider, progress = study._load(root)
    if (len(progress["pilot"]) != 3 or len(progress["warmup"]) != 18
            or len(progress["maintenance"]) != 4 or len(progress["evaluation"]) != 18):
        raise ValueError("host study is incomplete")
    plugins = {condition: json.loads((root / "sources" / condition / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
               for condition, identity in plan["sources"].items() if identity}
    plugin_names = {plugin["name"] for plugin in plugins.values()}
    receipts = []
    for path in sorted((root / "receipts").glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(row, dict) or "call_id" not in row:
            continue
        if not row.get("success") or row["observed_model_ids"] != [plan["model"]]:
            raise ValueError("unsuccessful or inconsistent model receipt")
        frames = [json.loads(line) for line in path.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines() if line]
        initial = next((f for f in frames if f.get("type") == "system" and f.get("subtype") == "init"), {})
        installed = [plugin for plugin in initial.get("plugins", []) if plugin.get("name") in plugin_names]
        if row["condition"] == "no_learning":
            if installed:
                raise ValueError("baseline plugin contamination")
        else:
            source = root / "sources" / row["condition"]
            plugin = plugins[row["condition"]]
            if (len(installed) != 1 or installed[0].get("name") != plugin["name"]
                    or Path(installed[0]["path"]).resolve() != source):
                raise ValueError("actual host plugin differs from pinned source")
        row["plugin_context_events"] = contexts(frames)
        row["plugin_context_bytes"] = sum(item["bytes"] for item in row["plugin_context_events"])
        receipts.append(row)
    if len(receipts) != 43:
        raise ValueError("expected 43 successful model sessions")
    for stage, count in plan["roles"].items():
        if sum(row["stage"] == stage for row in receipts) != count:
            raise ValueError("missing planned stage receipts")
    for attempt in manifest["attempts"]:
        condition = attempt["condition"]
        frozen = root / "frozen" / condition
        identity = progress["frozen"][condition]
        if study.library_hash(frozen / "project", frozen / "global") != identity["library_sha256"]:
            raise ValueError("frozen snapshot drift")
        visible = (root / attempt["workspace"]).parent
        if study.library_hash(visible / "workspace/.claude", visible / "global-config", content_only=True) != identity["content_sha256"]:
            raise ValueError("learned rules changed during evaluation")
    return root, plan, provider, progress, receipts


def publish(root, grant, output):
    root, plan, provider, progress, receipts = verify_complete(root)
    output = Path(output)
    if output.exists():
        raise FileExistsError("published evidence already exists")
    report = json.loads((root / "live-report.json").read_text(encoding="utf-8"))
    isolation = json.loads((root / "isolation-proof.json").read_text(encoding="utf-8"))
    if not isolation.get("passed") or len(isolation.get("checks", [])) != 3:
        raise ValueError("actual evaluation profile proof missing")
    cost = accounting.summary(grant)
    if cost["halt_reason"]:
        raise ValueError("budget is halted or unreconciled")
    native = [r["native_cli_estimated_usd"] for r in receipts if accounting.number(r.get("native_cli_estimated_usd"))]
    cost.update(successful_study_sessions=43, previous_failed_sonnet_sessions=cost["cli_sessions"] - 43,
                native_cli_estimated_usd_this_study=sum(native), native_cli_cost_coverage=len(native),
                token_price_estimate_usd_this_study=sum(r["estimated_usd"] for r in receipts),
                estimate_basis="DeepSeek published peak rates, without assuming off-peak discounts",
                pricing_source="https://api-docs.deepseek.com/quick_start/pricing/",
                account_balance_observation_note="balance changes include shared-account concurrent use and endpoint rounding; not a per-request invoice")
    cost["cli_estimated_usd"] = (cost["cli_estimated_usd"] or 0) + sum(native)
    cost["cli_estimate_coverage_sessions"] = len(native) + cost["previous_failed_sonnet_sessions"]
    summaries = {}
    for condition in study.task_eval.CONDITIONS:
        solves = [r for r in receipts if r["condition"] == condition and r["stage"] == "evaluation"]
        inventory = ({"skill_count": 0} if condition == "no_learning" else
                     json.loads((root / "receipts" / ("curator-" + condition + "-inventory.json")).read_text(encoding="utf-8")))
        maintenance = {}
        if condition != "no_learning":
            for role in ("reflector", "curator"):
                verdicts = json.loads((root / "receipts" / (role + "-" + condition + "-promotion.json")).read_text(encoding="utf-8"))["verdicts"]
                maintenance[role] = {"accepted": sum(v.get("ok") is True for v in verdicts),
                                     "rejected": sum(v.get("ok") is not True for v in verdicts),
                                     "archival_intents_accepted": sum(v.get("ok") is True and v.get("action") == "delete" for v in verdicts)}
        summaries[condition] = {"tasks_passed": report["conditions"][condition]["successes"], "tasks": 6,
                                "cases_correct": sum(sum(row["case_correct"].values()) for row in report["attempts"] if row["condition"] == condition),
                                "cases": 24, "final_active_skills": inventory["skill_count"],
                                "mean_plugin_context_bytes": sum(r["plugin_context_bytes"] for r in solves) / 6,
                                "mean_wall_seconds": sum(r["wall_seconds"] for r in solves) / 6,
                                "skill_file_reads": sum("/skills/" in (path or "") for r in solves for path in r["read_paths"]),
                                "solver_peak_estimate_usd": sum(r["estimated_usd"] for r in solves), "maintenance": maintenance}
    original = summaries["original"]["mean_plugin_context_bytes"]
    kit = summaries["kitharness"]["mean_plugin_context_bytes"]
    summary = {"version": 1, "kind": "controlled_live_three_arm", "date": "2026-11-02", "model": plan["model"],
               "provider": provider.public(), "conditions": summaries, "costs": cost,
               "observed_context_byte_reduction_vs_original": (original - kit) / original if original else None,
               "accuracy_improvement_demonstrated": any(v["kit_only_success"] > v["baseline_only_success"] for v in report["paired"].values()),
               "limitations": ["one repetition, six synthetic structured workflow tasks; no significance claim",
                               "same warmup lessons; one explicit batched reflection and curation per learning arm, not a native cadence comparison",
                               "different generated libraries confound retrieval/context comparisons",
                               "plugin context bytes include last-run summaries; native system prompt, skill body reads and other conversation tokens are separate",
                               "requested and reported model IDs match; provider model weights were not independently audited",
                               "curation archives are separate from lifecycle retirement; no forced retirement experiment",
                               "account balance and token-based peak estimate are different cost observations"]}
    report.update(summary=summary, cost_basis=cost["estimate_basis"],
                  protocol_verification="43 actual host receipts, source/plugin/model identities and frozen skill contents checked",
                  telemetry_scope="conditions contain solver telemetry; costs/all_phase_accounting cover pilot, warmup and maintenance separately")
    report["all_phase_accounting"] = cost
    selected = []
    for directory in (root / "receipts", root / "controller-at-run-completion"):
        selected.extend(path for path in directory.rglob("*") if path.is_file())
    selected.extend(root / name for name in ("manifest.json", "manifest.sha256", "warmup.json", "progress.json", "isolation-proof.json"))
    for condition in study.task_eval.CONDITIONS:
        selected.extend(path for path in (root / "frozen" / condition).rglob("*") if path.is_file())
    for attempt in study.task_eval._load_run(root)[1]["attempts"]:
        selected.append(root / attempt["workspace"] / "answer.json")
    secrets = [provider.env[k].encode("utf-8") for k in study.live_auth.CREDENTIAL_KEYS if provider.env.get(k)]
    contents, file_manifest = checked_files(root, selected, secrets)
    output.mkdir(parents=True)
    study.save(output / "report.json", report)
    study.save(output / "summary.json", summary)
    study.save(output / "isolation.json", isolation)
    study.save(output / "calls.json", [{key: r.get(key) for key in (
        "call_id", "stage", "condition", "model", "observed_model_ids", "success", "wall_seconds", "usage",
        "estimated_usd", "native_cli_estimated_usd", "cost_basis", "advertised_tools", "used_tools", "plugin_context_bytes", "hook_events")}
        for r in receipts])
    archive = output / "evidence.tar.gz"
    with tarfile.open(archive, "w:gz") as target:
        for name, raw in contents:
            entry = tarfile.TarInfo(name)
            entry.size = len(raw)
            entry.mode = 0o600
            target.addfile(entry, io.BytesIO(raw))
    study.save(output / "evidence-manifest.json", {"files": file_manifest, "archive_sha256": study.digest(archive.read_bytes()),
               "includes_thinking_blocks": False, "includes_credential_sources_or_native_cli_configs": False,
               "controller_source_snapshot": "captured after model run completion, before report-only fixes; not an execution signature"})
    if cost.get("grant_status") != "completed":
        accounting.close(grant, evidence_sha256=study.digest(archive.read_bytes()))
    cost["grant_status"] = "completed"
    study.save(output / "summary.json", summary)
    study.save(output / "report.json", report)
    return summary


def verify_archive(directory, suite_path):
    """Re-grade saved JSON answers without extracting/executing code or reading keys."""
    directory = Path(directory)
    listing = json.loads((directory / "evidence-manifest.json").read_bytes())
    archive = directory / "evidence.tar.gz"
    if study.digest(archive.read_bytes()) != listing["archive_sha256"]:
        raise ValueError("evidence archive hash mismatch")
    expected = {row["path"]: row for row in listing["files"]}
    if len(expected) != len(listing["files"]):
        raise ValueError("duplicate manifest file")
    files, total = {}, 0
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            path = PurePosixPath(member.name)
            if (not member.isfile() or member.name not in expected or member.name in files
                    or path.is_absolute() or ".." in path.parts or "\\" in member.name):
                raise ValueError("unsafe or unexpected evidence member")
            total += member.size
            if total > 64_000_000 or member.size != expected[member.name]["bytes"]:
                raise ValueError("evidence size limit or manifest mismatch")
            with source.extractfile(member) as handle:
                raw = handle.read(member.size + 1)
            if study.digest(raw) != expected[member.name]["sha256"]:
                raise ValueError("evidence file hash mismatch")
            files[member.name] = raw
    if set(files) != set(expected):
        raise ValueError("missing evidence files")
    manifest = study.task_eval._parse(files["manifest.json"])
    if study.digest(files["manifest.json"]) != files["manifest.sha256"].decode("ascii"):
        raise ValueError("experiment manifest hash mismatch")
    suite = study.task_eval.load_suite(suite_path)
    if study.digest(study.task_eval._json_bytes(suite)) != manifest["suite_sha256"]:
        raise ValueError("grading suite differs from frozen study")
    summaries = {condition: {"tasks": 0, "passed": 0, "cases": 0, "correct": 0} for condition in study.task_eval.CONDITIONS}
    for attempt in manifest["attempts"]:
        raw = files.get(attempt["workspace"] + "/answer.json")
        if raw is None:
            raise ValueError("missing saved answer")
        task = next(task for task in suite["tasks"] if task["id"] == attempt["task"])
        try:
            answer = study.task_eval._parse(raw)
        except (ValueError, TypeError):
            answer = None
        grade = study.task_eval.grade(task, answer)
        summary = summaries[attempt["condition"]]
        summary["tasks"] += 1
        summary["passed"] += grade["success"]
        summary["cases"] += len(task["cases"])
        summary["correct"] += sum(grade["case_correct"].values())
    return {"archive_verified": True, "files_verified": len(files), "answer_files": len(manifest["attempts"]),
            "conditions": summaries, "scope": "saved answer grading and evidence hashes; not an independent model rerun", "model_requests": 0}

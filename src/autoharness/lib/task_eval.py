"""Prepare pinned three-arm experiments and grade JSON data, never execute model code.

This module has no model runner. Live answers and telemetry must be collected by
an explicitly budgeted host experiment. Fixture smoke results are labeled as such.
"""
import argparse
import hashlib
import io
import json
import math
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from autoharness.lib import atomic, lock

CONDITIONS = ("no_learning", "original", "kitharness")
ORIGINAL = "f2de058720317025c4c18369b7227235141a218b"
KIT = "b232f74c5c7e6f0a02dcda688584cfb6dbf19c52"
MAX_JSON_BYTES = 1_000_000
MAX_SOURCE_BYTES = 64_000_000
TELEMETRY_FIELDS = ("duration_ms", "input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens", "total_cost_usd")
_ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("non-finite JSON number")


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _read(path, limit=MAX_JSON_BYTES):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("expected a regular, non-linked file")
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("file exceeds evaluation size limit")
    return raw


def _json(path):
    return _parse(_read(path))


def _parse(raw):
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float)


def load_suite(path):
    return _check_suite(_json(path))


def _check_suite(suite):
    if (not isinstance(suite, dict) or type(suite.get("version")) is not int or suite.get("version") != 1
            or not _valid_id(suite.get("id")) or not isinstance(suite.get("tasks"), list)
            or not 1 <= len(suite["tasks"]) <= 100):
        raise ValueError("invalid task suite")
    seen = set()
    for task in suite["tasks"]:
        if (not isinstance(task, dict) or not _valid_id(task.get("id")) or task["id"] in seen
                or not isinstance(task.get("instruction"), str) or not task["instruction"].strip()
                or not isinstance(task.get("warmup"), str) or not task["warmup"].strip()
                or not isinstance(task.get("cases"), list) or not 1 <= len(task["cases"]) <= 100):
            raise ValueError("invalid or duplicate task")
        seen.add(task["id"])
        case_ids = set()
        for case in task["cases"]:
            if (not isinstance(case, dict) or not _valid_id(case.get("id")) or case["id"] in case_ids
                    or "input" not in case or "expected" not in case):
                raise ValueError("invalid or duplicate case")
            case_ids.add(case["id"])
    return suite


def _git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("pinned source unavailable in local git repository")
    return result.stdout


def tree_hash(directory):
    """Hash file names, executable bits and bytes; reject links and special files."""
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("snapshot directory is linked")
    digest = hashlib.sha256()
    total = 0
    for path in sorted(directory.rglob("*")):
        mode = path.lstat().st_mode
        if path.is_symlink() or path.resolve().is_relative_to(directory.resolve()) is False:
            raise ValueError("snapshot contains a link")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError("snapshot contains a special file")
        raw = _read(path, MAX_SOURCE_BYTES)
        total += len(raw)
        if total > MAX_SOURCE_BYTES:
            raise ValueError("source snapshot exceeds size limit")
        digest.update(str(path.relative_to(directory)).encode("utf-8") + b"\0")
        digest.update(str(mode & 0o111).encode() + b"\0")
        digest.update(raw + b"\0")
    return digest.hexdigest()


def export_source(repo, ref, destination):
    commit = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode("ascii").strip()
    raw = _git(repo, "archive", "--format=tar", commit)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("source archive exceeds size limit")
    destination.mkdir(parents=True)
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or "\\" in member.name:
                raise ValueError("unsafe archive path")
            target = destination.joinpath(*name.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                total += member.size
                if total > MAX_SOURCE_BYTES:
                    raise ValueError("source archive exceeds size limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as handle:
                    target.write_bytes(handle.read())
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
            else:
                raise ValueError("source archive contains non-regular content")
    return {"commit": commit, "snapshot_sha256": tree_hash(destination)}


def public_task(task):
    return {"id": task["id"], "instruction": task["instruction"],
            "cases": [{"id": case["id"], "input": case["input"]} for case in task["cases"]]}


def prompt_for(task):
    return ("完成以下结构化工作流任务。只提交 JSON 数据文件 answer.json，格式为 "
            '{"answers":{"case-id":该用例输出}}。不得增加未知 case-id。'
            "整数和布尔值不可互换，数组顺序有意义。不要提交或运行实现代码。\n\n"
            + _json_bytes(public_task(task)).decode("utf-8"))


def prepare(repo, suite_path, output, *, model, repeats=1, seed=7, original_ref=ORIGINAL,
            kit_ref=KIT, kind="planned_live"):
    if kind not in ("planned_live", "fixture_smoke") or not isinstance(model, str) or not model.strip():
        raise ValueError("explicit model and valid experiment kind required")
    if type(repeats) is not int or not 1 <= repeats <= 20 or type(seed) is not int:
        raise ValueError("repeats must be 1..20 and seed must be an integer")
    suite = load_suite(suite_path)
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("experiment output already exists; never overwrite a run")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".kitharness-eval-", dir=output.parent))
    try:
        sources = {"no_learning": None,
                   "original": export_source(repo, original_ref, staging / "sources/original"),
                   "kitharness": export_source(repo, kit_ref, staging / "sources/kitharness")}
        frozen = _json_bytes(suite)
        (staging / "grading").mkdir()
        (staging / "grading/suite.json").write_bytes(frozen)
        warmup = [{"id": task["id"], "prompt": task["warmup"]} for task in suite["tasks"]]
        (staging / "warmup.json").write_bytes(_json_bytes(warmup))
        attempts = []
        rng = random.Random(seed)
        for repetition in range(repeats):
            ordered = list(suite["tasks"])
            rng.shuffle(ordered)
            for index, task in enumerate(ordered):
                rotation = (index + repetition) % len(CONDITIONS)
                order = CONDITIONS[rotation:] + CONDITIONS[:rotation]
                for condition in order:
                    attempt_id = f"{condition}-r{repetition:02d}-{task['id']}"
                    relative = f"attempts/{attempt_id}/workspace"
                    workspace = staging / relative
                    workspace.mkdir(parents=True)
                    (workspace / ".claude/skills").mkdir(parents=True)
                    global_root = f"attempts/{attempt_id}/global-config"
                    (staging / global_root / "skills").mkdir(parents=True)
                    prompt = prompt_for(task).encode("utf-8")
                    (workspace / "PROMPT.txt").write_bytes(prompt)
                    task_bytes = _json_bytes(public_task(task))
                    (workspace / "task.json").write_bytes(task_bytes)
                    attempts.append({"id": attempt_id, "condition": condition, "task": task["id"],
                                     "repetition": repetition, "workspace": relative,
                                     "project_root": f"{relative}/.claude", "global_root": global_root,
                                     "prompt_sha256": _sha(prompt), "task_sha256": _sha(task_bytes)})
        manifest = {"version": 1, "kind": kind, "model": model, "suite_id": suite["id"],
                    "suite_sha256": _sha(frozen), "warmup_sha256": _sha((staging / "warmup.json").read_bytes()),
                    "seed": seed, "seed_scope": "scheduling only, not a model sampling seed", "repeats": repeats,
                    "sources": sources, "attempts": attempts,
                    "protocol": {"warmup": "same episodes for all arms; not executed by this tool",
                                 "evaluation": "fresh solver session per task; frozen condition library; no test feedback until all answers collected",
                                 "host_driver": "step 11; no model invocation in this tool",
                                 "telemetry": "externally reported, not independently verified"}}
        raw = _json_bytes(manifest)
        (staging / "manifest.json").write_bytes(raw)
        (staging / "manifest.sha256").write_text(_sha(raw), encoding="ascii")
        (staging / "results").mkdir()
        os.rename(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return manifest


def _load_run(root):
    root = Path(root).resolve()
    raw = _read(root / "manifest.json")
    if _sha(raw) != _read(root / "manifest.sha256").decode("ascii"):
        raise ValueError("manifest integrity check failed")
    manifest = _parse(raw)
    if manifest.get("version") != 1 or manifest.get("kind") not in ("planned_live", "fixture_smoke"):
        raise ValueError("invalid experiment manifest")
    suite_path = _path(root, "grading/suite.json")
    suite_bytes = _read(suite_path)
    if _sha(suite_bytes) != manifest["suite_sha256"]:
        raise ValueError("suite integrity check failed")
    suite = _check_suite(_parse(suite_bytes))
    if _sha(_read(root / "warmup.json")) != manifest["warmup_sha256"]:
        raise ValueError("warmup integrity check failed")
    return root, manifest, suite, _sha(raw)


def _path(root, relative):
    root = Path(root).resolve()
    candidate = root / relative
    if not candidate.resolve().is_relative_to(root) or candidate.is_symlink():
        raise ValueError("evaluation path escaped its run directory")
    return candidate


def _equal(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_equal(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_equal(a, b) for a, b in zip(actual, expected, strict=True))
    return actual == expected


def grade(task, answer):
    ids = {case["id"] for case in task["cases"]}
    if (not isinstance(answer, dict) or set(answer) != {"answers"} or not isinstance(answer["answers"], dict)
            or set(answer["answers"]) - ids):
        return {"valid": False, "case_correct": {key: False for key in sorted(ids)}, "case_score": 0, "success": False}
    cases = {case["id"]: case["id"] in answer["answers"] and _equal(answer["answers"][case["id"]], case["expected"])
             for case in task["cases"]}
    return {"valid": True, "case_correct": cases, "case_score": sum(cases.values()) / len(cases), "success": all(cases.values())}


def _telemetry(metadata, model):
    metadata = metadata or {}
    if not isinstance(metadata, dict) or metadata.get("model") not in (None, model):
        raise ValueError("telemetry model must match the experiment model")
    result = {"model": metadata.get("model"), "source": "external-unverified", "library_sha256": metadata.get("library_sha256")}
    if result["library_sha256"] is not None and not _HASH.fullmatch(result["library_sha256"]):
        raise ValueError("invalid library fingerprint")
    for field in TELEMETRY_FIELDS:
        value = metadata.get(field)
        if value is not None:
            if type(value) not in (int, float) or not 0 <= value <= 1e15 or not math.isfinite(value):
                raise ValueError("telemetry numbers must be finite and nonnegative")
            if field.endswith("tokens") and type(value) is not int:
                raise ValueError("token counters must be integers")
        result[field] = value
    return result


def record(root, attempt_id, *, status="completed", metadata=None):
    root, manifest, _, digest = _load_run(root)
    if status not in ("completed", "timeout", "error"):
        raise ValueError("unknown attempt status")
    attempt = next((item for item in manifest["attempts"] if item["id"] == attempt_id), None)
    if attempt is None:
        raise ValueError("unknown attempt")
    telemetry = _telemetry(metadata, manifest["model"])
    answer = _path(root, attempt["workspace"]) / "answer.json"
    try:
        answer_hash = _sha(_read(answer)) if status == "completed" else None
    except (OSError, ValueError):
        answer_hash = None
    receipt = {"version": 1, "manifest_sha256": digest, "attempt_id": attempt_id, "status": status,
               "answer_sha256": answer_hash, "telemetry": telemetry}
    path = _path(root, f"results/{attempt_id}.json")
    with lock.file_lock(path.with_suffix(".lock")):
        if path.exists():
            raise FileExistsError("attempt already recorded; use a new run for retries")
        atomic.write_bytes(path, _json_bytes(receipt))
    return receipt


def report(root):
    root, manifest, suite, digest = _load_run(root)
    tasks = {task["id"]: task for task in suite["tasks"]}
    source_ok = {"no_learning": True}
    for condition in CONDITIONS[1:]:
        try:
            source_ok[condition] = tree_hash(_path(root, f"sources/{condition}")) == manifest["sources"][condition]["snapshot_sha256"]
        except (OSError, ValueError):
            source_ok[condition] = False
    rows = []
    for attempt in manifest["attempts"]:
        task = tasks[attempt["task"]]
        result = {"valid": False, "case_correct": {case["id"]: False for case in task["cases"]},
                  "case_score": 0, "success": False}
        telemetry = _telemetry({}, manifest["model"])
        status = "missing"
        recorded = False
        try:
            receipt_path = _path(root, f"results/{attempt['id']}.json")
            if receipt_path.exists():
                recorded = True
                receipt = _json(receipt_path)
                if receipt.get("manifest_sha256") != digest or receipt.get("attempt_id") != attempt["id"]:
                    raise ValueError("receipt identity mismatch")
                status = receipt["status"]
                if status not in ("completed", "timeout", "error"):
                    raise ValueError("invalid receipt status")
                telemetry = _telemetry(receipt["telemetry"], manifest["model"])
                if status == "completed":
                    try:
                        answer = _path(root, attempt["workspace"]) / "answer.json"
                        if receipt["answer_sha256"] is None:
                            status = "missing_artifact"
                        else:
                            raw_answer = _read(answer)
                            if _sha(raw_answer) != receipt["answer_sha256"]:
                                status = "artifact_drift"
                            else:
                                result = grade(task, _parse(raw_answer))
                                if not result["valid"]:
                                    status = "invalid_artifact"
                    except (OSError, ValueError, TypeError, RecursionError):
                        status = "invalid_artifact"
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            status = "invalid_receipt"
        workspace = _path(root, attempt["workspace"])
        try:
            inputs_ok = (_sha(_read(workspace / "PROMPT.txt")) == attempt["prompt_sha256"]
                         and _sha(_read(workspace / "task.json")) == attempt["task_sha256"])
        except (OSError, ValueError):
            inputs_ok = False
        if not source_ok[attempt["condition"]] or not inputs_ok:
            status = "source_drift" if not source_ok[attempt["condition"]] else "input_drift"
        if attempt["condition"] == "no_learning":
            try:
                skill_roots = [_path(root, attempt[key] + "/skills") for key in ("project_root", "global_root")]
                if any(any(directory.iterdir()) for directory in skill_roots):
                    status = "baseline_contamination"
            except (OSError, ValueError):
                status = "baseline_contamination"
        if status != "completed":
            result = {**result, "case_score": 0, "success": False,
                      "case_correct": {key: False for key in result["case_correct"]}}
        rows.append({"attempt_id": attempt["id"], "condition": attempt["condition"], "task": attempt["task"],
                     "repetition": attempt["repetition"], "recorded": recorded, "status": status, **result, "telemetry": telemetry})
    summaries = {}
    for condition in CONDITIONS:
        group = [row for row in rows if row["condition"] == condition]
        library_hashes = {row["telemetry"]["library_sha256"] for row in group if row["telemetry"]["library_sha256"] is not None}
        if len(library_hashes) > 1:
            for row in group:
                if row["recorded"]:
                    row.update(status="library_drift", success=False, case_score=0,
                               case_correct={key: False for key in row["case_correct"]})
        metrics = {}
        for field in TELEMETRY_FIELDS:
            known = [row["telemetry"][field] for row in group if row["telemetry"][field] is not None]
            metrics[field] = {"known_count": len(known), "planned_count": len(group),
                              "known_total": sum(known) if known else None,
                              "known_mean": sum(known) / len(known) if known else None}
        summaries[condition] = {"planned": len(group), "recorded": sum(row["recorded"] for row in group),
                                "successes": sum(row["success"] for row in group),
                                "success_rate": sum(row["success"] for row in group) / len(group),
                                "mean_case_score": sum(row["case_score"] for row in group) / len(group),
                                "statuses": {status: sum(row["status"] == status for row in group) for status in sorted({row["status"] for row in group})},
                                "provenance": {"observed_model_count": sum(row["telemetry"]["model"] is not None for row in group),
                                               "library_fingerprint_count": sum(row["telemetry"]["library_sha256"] is not None for row in group),
                                               "library_consistent": len(library_hashes) == 1 if library_hashes else None},
                                "telemetry": metrics}
    paired = {}
    comparable = {"completed", "timeout", "error", "invalid_artifact", "missing_artifact"}
    by_key = {(row["condition"], row["repetition"], row["task"]): row for row in rows}
    for baseline in CONDITIONS[:2]:
        pairs = [(by_key[("kitharness", r, task)], by_key[(baseline, r, task)])
                 for r in range(manifest["repeats"]) for task in tasks]
        valid = [(kit, base) for kit, base in pairs if kit["status"] in comparable and base["status"] in comparable]
        paired[baseline] = {"planned_pairs": len(pairs), "comparable_pairs": len(valid),
                            "kit_only_success": sum(kit["success"] and not base["success"] for kit, base in valid),
                            "baseline_only_success": sum(base["success"] and not kit["success"] for kit, base in valid),
                            "ties": sum(kit["success"] == base["success"] for kit, base in valid)}
    return {"version": 1, "kind": manifest["kind"], "model": manifest["model"], "manifest_sha256": digest,
            "suite_sha256": manifest["suite_sha256"], "sources": manifest["sources"], "source_verified": source_ok,
            "scope": "JSON artifact grading; fixture results are not model effectiveness measurements",
            "telemetry_scope": "solver attempts only; warmup, reflection and curation overhead are not included",
            "protocol_verification": "host execution, warmup completion, isolation and library freezing require step 11 evidence",
            "conditions": summaries, "paired": paired, "attempts": rows}


def smoke(repo, suite_path, *, original_ref=ORIGINAL, kit_ref=KIT):
    """Deliberate perfect/partial/timeout fixtures, identical across conditions."""
    with tempfile.TemporaryDirectory(prefix="kitharness-eval-smoke-") as temporary:
        root = Path(temporary) / "run"
        manifest = prepare(repo, suite_path, root, model="offline-fixture", kind="fixture_smoke",
                           original_ref=original_ref, kit_ref=kit_ref)
        suite = load_suite(root / "grading/suite.json")
        tasks = {task["id"]: task for task in suite["tasks"]}
        for attempt in manifest["attempts"]:
            task = tasks[attempt["task"]]
            if task["id"] == suite["tasks"][-1]["id"]:
                record(root, attempt["id"], status="timeout")
                continue
            answer = {case["id"]: case["expected"] for case in task["cases"]}
            if task["id"] == suite["tasks"][-2]["id"]:
                answer[task["cases"][0]["id"]] = None
            _path(root, attempt["workspace"]).joinpath("answer.json").write_bytes(_json_bytes({"answers": answer}))
            record(root, attempt["id"])
        return report(root)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--suite", type=Path, default=Path(__file__).resolve().parents[3] / "evals/tasks/suite.json")
    commands = parser.add_subparsers(dest="command", required=True)
    preparing = commands.add_parser("prepare")
    preparing.add_argument("--output", type=Path, required=True)
    preparing.add_argument("--model", required=True)
    preparing.add_argument("--repeats", type=int, default=1)
    preparing.add_argument("--seed", type=int, default=7)
    preparing.add_argument("--original-ref", default=ORIGINAL)
    preparing.add_argument("--kit-ref", default=KIT)
    recording = commands.add_parser("record")
    recording.add_argument("run", type=Path)
    recording.add_argument("attempt")
    recording.add_argument("--status", choices=("completed", "timeout", "error"), default="completed")
    recording.add_argument("--metadata", type=Path)
    reporting = commands.add_parser("report")
    reporting.add_argument("run", type=Path)
    for command in (reporting, commands.add_parser("smoke")):
        command.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.repo, args.suite, args.output, model=args.model, repeats=args.repeats, seed=args.seed,
                         original_ref=args.original_ref, kit_ref=args.kit_ref)
        result = {"kind": result["kind"], "attempts": len(result["attempts"]), "models_called": 0}
    elif args.command == "record":
        result = record(args.run, args.attempt, status=args.status, metadata=_json(args.metadata) if args.metadata else None)
    else:
        result = report(args.run) if args.command == "report" else smoke(args.repo, args.suite)
    raw = _json_bytes(result)
    if getattr(args, "output", None) is not None and args.command != "prepare":
        target = args.output
        if target.exists() or target.is_symlink():
            raise FileExistsError("report output already exists")
        atomic.write_bytes(target, raw)
    else:
        print(raw.decode("utf-8"), end="")


if __name__ == "__main__":
    main()

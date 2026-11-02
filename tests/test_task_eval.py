"""Task experiment preparation and grading never invoke models or execute answers."""
import copy
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from kitharness.lib import task_eval as evaluation

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "evals/tasks/suite.json"


@pytest.fixture
def run(tmp_path):
    path = tmp_path / "run"
    manifest = evaluation.prepare(ROOT, SUITE, path, model="test-model")
    return path, manifest


def task_for(attempt):
    return next(task for task in evaluation.load_suite(SUITE)["tasks"] if task["id"] == attempt["task"])


def answer_for(task):
    return {"answers": {case["id"]: case["expected"] for case in task["cases"]}}


def submit(run, attempt=None, answer=None, *, metadata=None, status="completed"):
    root, manifest = run
    attempt = attempt or manifest["attempts"][0]
    if answer is None:
        answer = answer_for(task_for(attempt))
    (root / attempt["workspace"] / "answer.json").write_text(json.dumps(answer), encoding="utf-8")
    evaluation.record(root, attempt["id"], status=status, metadata=metadata)
    return attempt


def row_for(report, attempt):
    return next(row for row in report["attempts"] if row["attempt_id"] == attempt["id"])


def test_three_arms_have_identical_inputs_and_private_grading_separation(run):
    root, manifest = run
    assert len(manifest["attempts"]) == 18
    assert manifest["sources"]["no_learning"] is None
    assert manifest["sources"]["original"]["commit"] == evaluation.ORIGINAL
    assert manifest["sources"]["kitharness"]["commit"] == evaluation.KIT
    for task in evaluation.load_suite(SUITE)["tasks"]:
        attempts = [attempt for attempt in manifest["attempts"] if attempt["task"] == task["id"]]
        assert len(attempts) == 3
        assert len({attempt["prompt_sha256"] for attempt in attempts}) == 1
        prompts = {(root / attempt["workspace"] / "PROMPT.txt").read_bytes() for attempt in attempts}
        assert len(prompts) == 1
        for attempt in attempts:
            public = json.loads((root / attempt["workspace"] / "task.json").read_text())
            assert all(set(case) == {"id", "input"} for case in public["cases"])
            assert not (root / attempt["workspace"] / "grading").exists()
            assert (root / attempt["project_root"] / "skills").is_dir()
            assert (root / attempt["global_root"] / "skills").is_dir()


def test_preparing_twice_is_reproducible_and_never_overwrites_existing_run(run, tmp_path):
    root, manifest = run
    other = tmp_path / "other"
    assert evaluation.prepare(ROOT, SUITE, other, model="test-model") == manifest
    assert (root / "manifest.json").read_bytes() == (other / "manifest.json").read_bytes()
    sentinel = root / "keep"
    sentinel.write_text("preserve")
    with pytest.raises(FileExistsError):
        evaluation.prepare(ROOT, SUITE, root, model="different")
    assert sentinel.read_text() == "preserve"


def test_repeats_and_schedule_are_common_and_not_a_model_seed(tmp_path):
    root = tmp_path / "repeat"
    manifest = evaluation.prepare(ROOT, SUITE, root, model="model", repeats=2, seed=10)
    assert len(manifest["attempts"]) == 36
    for repeat in range(2):
        for condition in evaluation.CONDITIONS:
            assert len([a for a in manifest["attempts"] if a["repetition"] == repeat and a["condition"] == condition]) == 6
    assert "not a model" in manifest["seed_scope"]
    first_conditions = [manifest["attempts"][index]["condition"] for index in range(0, 18, 3)]
    assert set(first_conditions) == set(evaluation.CONDITIONS)


@pytest.mark.parametrize("index", range(6))
def test_gold_artifacts_pass_the_same_grader_for_every_task(index):
    task = evaluation.load_suite(SUITE)["tasks"][index]
    assert evaluation.grade(task, answer_for(task))["success"]


@pytest.mark.parametrize("bad_answer", [None, [], {}, {"answers": []}, {"answers": {"unknown-case": 1}},
                                       {"answers": {}, "explanation": "not part of the contract"}])
def test_malformed_output_cannot_get_partial_credit(bad_answer):
    task = evaluation.load_suite(SUITE)["tasks"][0]
    assert evaluation.grade(task, bad_answer)["case_score"] == 0


def test_partial_credit_and_integer_boolean_type_distinction():
    task = evaluation.load_suite(SUITE)["tasks"][0]
    answer = answer_for(task)
    answer["answers"].pop(task["cases"][0]["id"])
    assert evaluation.grade(task, answer)["case_score"] == .75
    merge = evaluation.load_suite(SUITE)["tasks"][4]
    answer = copy.deepcopy(answer_for(merge))
    answer["answers"]["safe-merge"]["archive"] = 1
    assert not evaluation.grade(merge, answer)["success"]


def test_missing_results_stay_in_denominator_and_unknown_cost_is_not_zero(run):
    root, _ = run
    result = evaluation.report(root)
    for condition in evaluation.CONDITIONS:
        summary = result["conditions"][condition]
        assert summary["planned"] == 6 and summary["recorded"] == 0
        assert summary["success_rate"] == 0 and summary["statuses"] == {"missing": 6}
        assert summary["telemetry"]["total_cost_usd"]["known_total"] is None
        assert summary["telemetry"]["duration_ms"]["known_count"] == 0
    assert result["paired"]["original"]["comparable_pairs"] == 0


@pytest.mark.parametrize("status", ["completed", "timeout", "error"])
def test_attempt_status_and_usage_are_counted_without_dropping_failures(run, status):
    root, _ = run
    metrics = {"model": "test-model", "duration_ms": 42, "input_tokens": 10, "output_tokens": 3, "total_cost_usd": .01}
    attempt = submit(run, status=status, metadata=metrics)
    result = evaluation.report(root)
    row = row_for(result, attempt)
    assert row["status"] == status and row["success"] == (status == "completed")
    aggregate = result["conditions"][attempt["condition"]]
    assert aggregate["planned"] == 6 and aggregate["recorded"] == 1
    assert aggregate["telemetry"]["total_cost_usd"] == {"known_count": 1, "planned_count": 6, "known_total": .01, "known_mean": .01}


def test_duplicate_record_is_rejected_and_answer_changes_invalidate_the_result(run):
    root, _ = run
    attempt = submit(run)
    with pytest.raises(FileExistsError):
        evaluation.record(root, attempt["id"])
    (root / attempt["workspace"] / "answer.json").write_text('{"answers":{}}')
    row = row_for(evaluation.report(root), attempt)
    assert row["status"] == "artifact_drift" and not row["success"]


@pytest.mark.parametrize("content", ['{"answers":{},"answers":{}}', '{"answers":NaN}', '{"answers":1e400}',
                                    '```json\n{}\n```', "x" * 1_000_001])
def test_invalid_json_duplicate_keys_and_oversize_artifacts_fail(run, content):
    root, manifest = run
    attempt = manifest["attempts"][0]
    (root / attempt["workspace"] / "answer.json").write_text(content)
    evaluation.record(root, attempt["id"])
    row = row_for(evaluation.report(root), attempt)
    assert row["status"] in ("invalid_artifact", "missing_artifact")
    assert not row["success"]


def test_answer_is_never_executed_and_raw_answer_does_not_enter_report(run, tmp_path, monkeypatch):
    root, _ = run
    marker = tmp_path / "executed"
    payload = f"__import__('pathlib').Path({str(marker)!r}).touch()"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("grading launched a process"))
    attempt = submit(run, answer={"answers": {"exponential": payload}})
    result = evaluation.report(root)
    assert not row_for(result, attempt)["success"]
    assert not marker.exists() and payload not in json.dumps(result)


@pytest.mark.parametrize("target", ["prompt", "task", "source"])
def test_drift_in_one_arm_is_visible_and_cannot_be_called_model_success(run, target):
    root, manifest = run
    attempt = next(a for a in manifest["attempts"] if a["condition"] == "kitharness")
    submit(run, attempt)
    if target == "source":
        (root / "sources/kitharness/README.md").write_text("changed")
    else:
        filename = "PROMPT.txt" if target == "prompt" else "task.json"
        (root / attempt["workspace"] / filename).write_text("changed")
    result = evaluation.report(root)
    row = row_for(result, attempt)
    assert row["status"] == ("source_drift" if target == "source" else "input_drift")
    assert not row["success"]
    assert result["conditions"]["original"]["recorded"] == 0


@pytest.mark.parametrize("target", ["manifest.json", "grading/suite.json", "warmup.json"])
def test_modified_experiment_definition_stops_reporting(run, target):
    root, _ = run
    path = root / target
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="integrity"):
        evaluation.report(root)


def test_grading_cannot_use_unverified_suite_bytes_from_a_transient_read(run, monkeypatch):
    root, _ = run
    original = evaluation._read
    tampered = evaluation.load_suite(SUITE)
    tampered["tasks"][0]["cases"][0]["expected"] = "unverified-answer"
    first = True

    def transient(path, *args, **kwargs):
        nonlocal first
        if Path(path) == root / "grading/suite.json" and first:
            first = False
            return json.dumps(tampered).encode("utf-8")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(evaluation, "_read", transient)
    with pytest.raises(ValueError, match="suite integrity"):
        evaluation.report(root)


@pytest.mark.parametrize("metadata", [{"model": "wrong-model"}, {"duration_ms": -1}, {"input_tokens": True},
                                      {"input_tokens": 1.5}, {"total_cost_usd": float("nan")}, {"library_sha256": "bad"}])
def test_incomparable_or_invalid_metadata_is_rejected_before_recording(run, metadata):
    root, manifest = run
    attempt = manifest["attempts"][0]
    with pytest.raises(ValueError):
        evaluation.record(root, attempt["id"], metadata=metadata)
    assert not (root / f"results/{attempt['id']}.json").exists()


def test_missing_output_and_linked_output_do_not_read_external_data(run, tmp_path):
    root, manifest = run
    first, second = manifest["attempts"][:2]
    evaluation.record(root, first["id"])
    external = tmp_path / "private"
    external.write_text("private-value-do-not-read")
    (root / second["workspace"] / "answer.json").symlink_to(external)
    evaluation.record(root, second["id"])
    result = evaluation.report(root)
    assert row_for(result, first)["status"] == "missing_artifact"
    assert row_for(result, second)["status"] == "missing_artifact"
    assert "private-value" not in json.dumps(result)


@pytest.mark.parametrize("member_type", ["traversal", "symlink"])
def test_source_export_rejects_unsafe_archive_entries(tmp_path, monkeypatch, member_type):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        member = tarfile.TarInfo("../escaped" if member_type == "traversal" else "linked")
        if member_type == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "/outside"
        archive.addfile(member)
    monkeypatch.setattr(evaluation, "_git", lambda repo, *args: b"a" * 40 if args[0] == "rev-parse" else stream.getvalue())
    with pytest.raises(ValueError, match="unsafe|non-regular"):
        evaluation.export_source(ROOT, "ref", tmp_path / "source")
    assert not (tmp_path / "escaped").exists()


def test_paired_comparison_does_not_treat_missing_arm_as_a_valid_loss(run):
    root, manifest = run
    kit = next(a for a in manifest["attempts"] if a["condition"] == "kitharness")
    submit(run, kit)
    assert evaluation.report(root)["paired"]["original"]["comparable_pairs"] == 0
    original = next(a for a in manifest["attempts"] if a["task"] == kit["task"] and a["condition"] == "original")
    submit(run, original, status="timeout")
    paired = evaluation.report(root)["paired"]["original"]
    assert paired["comparable_pairs"] == 1 and paired["kit_only_success"] == 1


def test_changing_library_fingerprints_cannot_pass_as_frozen_library_evaluation(run):
    root, manifest = run
    attempts = [item for item in manifest["attempts"] if item["condition"] == "kitharness"][:2]
    for index, attempt in enumerate(attempts):
        submit(run, attempt, metadata={"model": "test-model", "library_sha256": str(index) * 64})
    result = evaluation.report(root)
    assert all(row_for(result, attempt)["status"] == "library_drift" for attempt in attempts)
    assert not result["conditions"]["kitharness"]["provenance"]["library_consistent"]


def test_no_learning_condition_with_skills_is_flagged_as_contaminated(run):
    root, manifest = run
    attempt = next(item for item in manifest["attempts"] if item["condition"] == "no_learning")
    submit(run, attempt)
    (root / attempt["project_root"] / "skills/memory.txt").write_text("unexpected retained memory")
    row = row_for(evaluation.report(root), attempt)
    assert row["status"] == "baseline_contamination" and not row["success"]


@pytest.mark.parametrize("invalid", ["duplicate-task", "duplicate-case", "unsafe-id", "wrong-id-type", "boolean-version"])
def test_invalid_suites_are_rejected_before_output_creation(tmp_path, invalid):
    suite = evaluation.load_suite(SUITE)
    if invalid == "duplicate-task":
        suite["tasks"].append(copy.deepcopy(suite["tasks"][0]))
    elif invalid == "duplicate-case":
        suite["tasks"][0]["cases"].append(copy.deepcopy(suite["tasks"][0]["cases"][0]))
    elif invalid == "unsafe-id":
        suite["tasks"][0]["id"] = "../outside"
    elif invalid == "wrong-id-type":
        suite["tasks"][0]["id"] = []
    else:
        suite["version"] = True
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(suite))
    with pytest.raises(ValueError):
        evaluation.prepare(ROOT, path, tmp_path / "must-not-exist", model="model")
    assert not (tmp_path / "must-not-exist").exists()


def test_smoke_report_is_fixture_only_reproducible_and_has_real_failure_paths():
    actual = evaluation.smoke(ROOT, SUITE)
    saved = json.loads((ROOT / "evals/tasks/smoke-report.json").read_text(encoding="utf-8"))
    assert actual["kind"] == saved["kind"] == "fixture_smoke"
    assert actual["conditions"] == saved["conditions"]
    assert actual["paired"] == saved["paired"]
    assert all(item["successes"] == 4 and item["statuses"]["timeout"] == 1 for item in actual["conditions"].values())
    assert all(item["telemetry"]["total_cost_usd"]["known_total"] is None for item in actual["conditions"].values())


def test_cli_prepare_reports_no_model_calls(tmp_path):
    result = subprocess.run([sys.executable, "-m", "kitharness.lib.task_eval", "--repo", str(ROOT), "--suite", str(SUITE),
                             "prepare", "--output", str(tmp_path / "cli"), "--model", "not-invoked"],
                            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                            capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"attempts": 18, "kind": "planned_live", "models_called": 0}

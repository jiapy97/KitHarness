import json
from pathlib import Path

import pytest

from kitharness.lib.live_study_report import checked_files, contexts, verify_archive


def test_only_actual_stdout_context_is_counted_not_stderr_intentions():
    frames = [{"type": "system", "subtype": "hook_response", "hook_event": "UserPromptSubmit",
               "stdout": json.dumps({"hookSpecificOutput": {"additionalContext": "规则"}}),
               "stderr": json.dumps({"result": {"context": "much-longer-intended-context"}})},
              {"type": "system", "subtype": "hook_response", "hook_event": "SessionStart",
               "stdout": "", "stderr": "unapplied context"}]
    assert contexts(frames) == [{"event": "UserPromptSubmit", "bytes": len("规则".encode())}]


def test_invalid_hook_stdout_does_not_invent_injection_measurements():
    frames = [{"type": "system", "subtype": "hook_response", "stdout": "not-json"},
              {"type": "system", "subtype": "hook_response", "stdout": "[]"},
              {"type": "assistant", "subtype": "hook_response", "stdout": "context"}]
    assert contexts(frames) == []


def test_actual_credential_in_evidence_stops_export_without_echo(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text('{"error":"echo private-key-value"}', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        checked_files(tmp_path, [path], [b"private-key-value"])
    assert "private-key-value" not in str(error.value)


def test_saved_real_evidence_regrades_without_model_or_credential_access():
    repo = Path(__file__).resolve().parents[1]
    result = verify_archive(repo / "evals/live/real-study", repo / "evals/tasks/suite.json")
    assert result["model_requests"] == 0 and result["answer_files"] == 18
    assert result["archive_verified"]
    assert all(row["passed"] == row["tasks"] == 6 and row["correct"] == row["cases"] == 24
               for row in result["conditions"].values())


def test_tampered_archive_is_rejected_before_any_extract_or_grading(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    (tmp_path / "evidence-manifest.json").write_bytes((repo / "evals/live/real-study/evidence-manifest.json").read_bytes())
    (tmp_path / "evidence.tar.gz").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="archive hash"):
        verify_archive(tmp_path, repo / "evals/tasks/suite.json")

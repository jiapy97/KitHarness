"""Credentials must not escape via nested values, provenance, or staged content."""
import json
import time

import pytest

from autoharness.lib import intent_queue, ledger, redact
from autoharness.stage_skill import server

SECRET = "abcd1234efgh5678ijkl"
BODY = "---\nname: dates\ndescription: Use when formatting dates.\n---\nUse ISO dates.\n"


@pytest.mark.parametrize("escaped", [False, True])
@pytest.mark.parametrize("assignment", [f"client_secret={SECRET}", f'client_secret":"{SECRET}'])
def test_nested_assignments_leave_no_secret(escaped, assignment):
    text = json.dumps({"token": assignment})
    if escaped:
        text = json.dumps({"transcript": text})
    result = redact.redact(text)
    assert SECRET not in result
    assert redact.redact(result) == result
    assert "api_key_assignment" in redact.secret_hits(text)


@pytest.mark.parametrize("text", ["http://user:" * 20000, "xoxb-" * 40000, "a" * 200000])
def test_adversarial_prefix_runs_finish_in_bounded_time(text):
    start = time.perf_counter()
    redact.redact(text)
    assert time.perf_counter() - start < 2.0


def test_long_url_password_is_redacted_without_a_length_cutoff():
    password = "z" * 20000
    result = redact.redact(f"postgres://user:{password}@db.internal/app")
    assert password not in result
    assert result.endswith("db.internal/app")


def test_ledger_redacts_nested_metadata_without_mutating_caller(tmp_path):
    entry = {"action": "patch", "reason": f"client_secret={SECRET}",
             "extra": [{"detail": f"api_key={SECRET}"}]}
    ledger.append("project", "dates", entry, tmp_path)
    assert SECRET not in ledger.path("project", "dates", tmp_path).read_text()
    assert SECRET in entry["reason"]


def test_queue_redacts_reason_and_evidence(tmp_path):
    intent_queue.append("run", {"action": "patch", "reason": f"api_key={SECRET}",
                                "evidence": f"password={SECRET}"}, tmp_path)
    text = (tmp_path / "kitharness/intents/run.jsonl").read_text()
    assert SECRET not in text


@pytest.mark.parametrize("in_subfile", [False, True])
def test_stage_rejects_secret_content_before_persistence(tmp_path, in_subfile):
    params = {"action": "create", "name": "dates", "body": BODY,
              "reason": "learned", "evidence": "example"}
    if in_subfile:
        params["body"] += "Read references/notes.md.\n"
        params["files"] = {"references/notes.md": f"client_secret={SECRET}"}
    else:
        params["body"] += f"client_secret={SECRET}\n"
    result = server.stage(params, run_id="run", root=tmp_path)
    assert not result["ok"]
    assert "secret" in [family for family, _ in result["errors"]]
    assert not intent_queue.pending("run", tmp_path)


def test_stage_response_and_queue_both_redact_provenance(tmp_path):
    result = server.stage({"action": "create", "name": "dates", "body": BODY,
                           "reason": f"api_key={SECRET}", "evidence": f"password={SECRET}"},
                          run_id="run", root=tmp_path)
    assert result["ok"]
    assert SECRET not in json.dumps(result)
    assert SECRET not in json.dumps(intent_queue.read("run", tmp_path))

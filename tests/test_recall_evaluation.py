"""The published synthetic comparison must remain reproducible and honestly scoped."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_fixed_report_is_reproducible_without_models_or_personal_libraries():
    spec = importlib.util.spec_from_file_location("evaluate_recall", ROOT / "tools" / "evaluate_recall.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    actual = module.evaluate()
    saved = json.loads((ROOT / "evals/recall/report.json").read_text(encoding="utf-8"))
    assert actual == saved
    assert actual["corpus_count"] == 20 and actual["query_count"] == 18
    assert "not host tokens" in actual["token_estimator"]


def test_equal_budget_comparison_keeps_failures_visible_and_respects_limits():
    report = json.loads((ROOT / "evals/recall/report.json").read_text(encoding="utf-8"))
    task = report["systems"]["task_bm25"]
    baseline = report["systems"]["budgeted_catalog"]
    assert task["profile"]["max_bytes"] == baseline["profile"]["max_bytes"]
    assert task["profile"]["top_k"] == baseline["profile"]["top_k"]
    assert task["summary"]["macro_recall_positive"] > baseline["summary"]["macro_recall_positive"]
    for system in (task, baseline):
        assert all(row["bytes"] <= system["profile"]["max_bytes"] for row in system["per_query"])
    failure = next(row for row in task["per_query"] if row["id"] == "paraphrase-limit")
    assert failure["recall"] == 0 and failure["selected"] == []

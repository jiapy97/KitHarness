"""Reproduce the fixed-query metadata retrieval comparison, without model calls."""
import argparse
import hashlib
import json
from pathlib import Path

from autoharness.lib import recall

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "evals" / "recall" / "fixture.json"


def evaluate(path=FIXTURE):
    raw = Path(path).read_bytes()
    fixture = json.loads(raw)
    documents, queries = fixture["documents"], fixture["queries"]
    profiles = {
        "original_full_catalog": {"mode": "catalog", "max_bytes": 1_000_000, "top_k": len(documents)},
        "budgeted_catalog": {"mode": "catalog", "max_bytes": 1200, "top_k": 5},
        "task_bm25": {"mode": "task", "max_bytes": 1200, "top_k": 5},
    }
    systems = {}
    for name, profile in profiles.items():
        rows = []
        for query in queries:
            result = recall.plan(documents, query["query"], preview_chars=60, **profile)
            selected = [f"{item['level']}:{item['name']}" for item in result["ranked"] if item["reason"] == "selected"]
            relevant = set(query["relevant"])
            hit_count = len(set(selected) & relevant)
            rank = next((i for i, item in enumerate(selected, 1) if item in relevant), None)
            rows.append({"id": query["id"], "selected": selected,
                         "recall": hit_count / len(relevant) if relevant else None,
                         "reciprocal_rank": 1 / rank if rank else 0,
                         "precision": hit_count / len(selected) if selected else (0 if relevant else 1),
                         "bytes": result["usage"]["bytes"], "token_estimate": result["usage"]["token_estimate"]})
        positive = [row for row in rows if row["recall"] is not None]
        negative = [row for row in rows if row["recall"] is None]
        summary = {"macro_recall_positive": sum(row["recall"] for row in positive) / len(positive),
                   "mrr_positive": sum(row["reciprocal_rank"] for row in positive) / len(positive),
                   "mean_precision_all": sum(row["precision"] for row in rows) / len(rows),
                   "unrelated_injection_rate": sum(bool(row["selected"]) for row in negative) / len(negative),
                   "mean_bytes": sum(row["bytes"] for row in rows) / len(rows),
                   "mean_token_estimate": sum(row["token_estimate"] for row in rows) / len(rows)}
        systems[name] = {"profile": profile, "summary": {key: round(value, 6) for key, value in summary.items()},
                         "per_query": rows}
    return {"version": 1, "fixture_sha256": hashlib.sha256(raw).hexdigest(), "algorithm": recall.ALGORITHM,
            "corpus_count": len(documents), "query_count": len(queries),
            "positive_queries": sum(bool(item["relevant"]) for item in queries),
            "token_estimator": "utf8-bytes-div-4; not host tokens or billing",
            "scope": "Synthetic metadata relevance labels; no model task success or live hook validation.",
            "systems": systems}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.fixture)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(json.dumps({"output": str(args.output), "summary": {
            name: value["summary"] for name, value in report["systems"].items()}}, ensure_ascii=False, indent=2))
    else:
        print(text, end="")


if __name__ == "__main__":
    main()

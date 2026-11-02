"""Task-aware recall before the user prompt is processed; never changes skill state."""

from kitharness import config
from kitharness._compat import env_get
from kitharness.lib import recall


def on_user_prompt(event, *, roots=None):
    if env_get(config.CHILD_SESSION_ENV):
        return {"context": None, "reason": "recursion_guard"}
    if config.INDEX_SUSPENDED or config.RECALL_MODE != "task":
        return {"context": None, "reason": "recall_disabled"}
    if config.RECALL_MAX_BYTES == 0:
        return {"context": None, "reason": "zero_budget"}
    query = event.get("prompt")
    if not isinstance(query, str) or not query.strip():
        return {"context": None, "reason": "no_task"}
    result = recall.retrieve(roots or {}, query, cwd=event.get("cwd"))
    return {"context": result["context"], "recall": {
        "algorithm": result["algorithm"], "candidate_count": result["candidate_count"],
        "skipped_count": result["skipped_count"], "usage": result["usage"],
        "selected": [item for item in result["ranked"] if item["reason"] == "selected"],
    }}

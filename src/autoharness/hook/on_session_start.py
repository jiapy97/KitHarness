"""MNG lazy recompute: at SessionStart, compute over the accumulated ledger now → archive inactive symbols, running before this session's recall.

mng.md: a non-resident host has no background sweep, so eviction rides SessionStart — read the sidecar
(use/view counters + anchor) + the layer request counters (denominator) accumulated watermark, run the
lifecycle decision, and move the to-archive list out of the live tree one by one (archiving = moving the
directory out of recall, reversible). The decision reads only accumulated quantities (not what this
session happened to see) → any repo's SessionStart reaches the same conclusion. Once per session, no
throttling. Manages only self-produced symbols (native / user skills stay outside the pool, preserving
zero intrusion).

ponytail: GC of orphan session counts (residue from crashed sessions) needs a session-liveness signal to sweep safely (a naive sweep would wrongly delete a concurrent session's live count), so it is deferred until that signal exists — the clear_session primitive is ready (Phase 4), policy left open in cap.md/mng.md.
"""
import json
import os
import uuid

from autoharness import config
from autoharness.hook import recovery
from autoharness.lib import (
    counters,
    layer,
    lifecycle,
    recall,
    sidecar,
    skill_store,
    versions,
)


def recall_index(roots, cwd=None, *, max_bytes=None):
    """Explicit catalog API; normal task mode injects matches at UserPromptSubmit."""
    if config.INDEX_SUSPENDED:
        return None
    return recall.retrieve(roots, mode="catalog", cwd=cwd, max_bytes=max_bytes)["context"]


def last_run_summary(roots):
    """One-line anti-silence digest of the previous drain (validate-store §verdict visibility):
    read once, then consume — the account file under runs/ keeps the durable record."""
    p = layer.state_dir(layer.PROJECT, roots.get(layer.PROJECT)) / "last_run.json"
    if not p.exists():
        return None
    # Each claimant owns a distinct destination: another drain may publish a new
    # last_run.json before this consumer has finished reading its claimed record.
    consumed = p.with_name(f"{p.name}.{uuid.uuid4().hex}.consuming")
    try:
        p.rename(consumed)
    except OSError:
        return None
    try:
        last = json.loads(consumed.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    finally:
        try:
            consumed.unlink()
        except OSError:
            pass
    # name the run so the numbers stay traceable when a Stop drained a dead sibling's queue
    line = (f"KitHarness last run ({last.get('run_id', 'unknown')}): landed {last.get('landed', 0)}, "
            f"rejected {last.get('rejected', 0)}")
    if last.get("families"):
        line += f" ({', '.join(last['families'])})"
    if last.get("absorbed"):
        line += f"; merged {last['absorbed']} into umbrellas"
    if last.get("uncategorized"):
        line += f"; {last['uncategorized']} landed with no category (grouped under general)"
    return line


def _members(lyr, root):
    skills = layer.skills_dir(lyr, root)
    if not skills.exists():
        return []
    members = []
    for path in skills.glob(f"*/{skill_store.SKILL_FILE}"):
        name = path.parent.name
        if not sidecar.is_agent_created(lyr, name, root):
            continue
        s = sidecar.read(lyr, name, root)
        members.append({"name": name, "use": s.get("use", 0), "view": s.get("view", 0),
                        "anchor": s.get("anchor", 0)})
    return members


def on_session_start(event=None, *, roots=None):
    if os.environ.get(config.CHILD_SESSION_ENV):
        # a reflector/curator child: it must not consume the user's last-run line, run the archive pass
        # beside the parent, or get a second index on top of its bundle
        return {"archived": {}, "context": None, "reason": "recursion_guard"}
    roots = roots or {}
    versions.recover_all(roots)
    recovery.recover(roots=roots)
    archived = {}
    for lyr in config.active_layers():
        root = roots.get(lyr)
        names = lifecycle.evaluate(
            _members(lyr, root), counters.request_count(lyr, root),
            maturity=config.MATURITY_THRESHOLD[lyr], capacity=config.CAPACITY[lyr],
            review_suspended=config.GRADUATION_REVIEW_SUSPENDED,
        )
        for name in names:
            skill_store.archive(lyr, name, root)
        archived[lyr] = names
    summary = last_run_summary(roots)
    parts = [summary] if summary and len(summary.encode("utf-8")) <= config.RECALL_MAX_BYTES else []
    if config.RECALL_MODE == "catalog":
        remaining = max(0, config.RECALL_MAX_BYTES - len("\n\n".join(parts).encode("utf-8")) - (2 if parts else 0))
        if remaining:
            parts.append(recall_index(roots, (event or {}).get("cwd"), max_bytes=remaining))
    context = "\n\n".join(p for p in parts if p) or None
    return {"archived": archived, "context": context}

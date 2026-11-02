"""Read-only lexical retrieval, bounded context rendering, and numeric explanations.

No prompt persistence, model calls, embeddings or use/view increments. Token counts
are explicitly a UTF-8 byte/4 proxy; only the byte limit is a hard text bound.
"""
import argparse
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

from autoharness import config
from autoharness.lib import layer, redact, sidecar, skill_store, validate

ALGORITHM = "field-bm25-v1"
MAX_QUERY_CHARS = 8000
MAX_QUERY_TERMS = 256
MAX_METADATA_BYTES = 64_000
MAX_FIELD_CHARS = 8000
FIELD_WEIGHTS = {"name": 3.0, "category": 1.0, "description": 2.0}
_PIECES = re.compile(r"[a-z]+(?:\+\+|#)?|[0-9]+|[\u3400-\u9fff]+")
_STOP = frozenset("a an and are as at be by can do for from how i in is it me of on or please that the this to use when with you your redacted".split())
_ZH_STOP = frozenset(("用于", "适用", "用来", "使用", "需要", "当需", "时使", "时候"))
INDEX_HEADER = (
    "Self-accumulated skills (KitHarness-managed). When the task at hand matches a "
    "description below, you MUST consider loading that skill before proceeding."
)
TASK_HEADER = "Skills related to this request (KitHarness-managed; lexical matches):"


def tokens(text):
    normalized = unicodedata.normalize("NFKC", text).casefold()
    result = []
    for match in _PIECES.finditer(normalized):
        piece = match.group()
        if "\u3400" <= piece[0] <= "\u9fff":
            parts = [piece] if len(piece) == 1 else [piece[i:i + 2] for i in range(len(piece) - 1)]
            result.extend(part for part in parts if part not in _ZH_STOP)
        elif piece not in _STOP:
            result.append(piece)
    return result


def _flat(text):
    return " ".join(str(text).split())


def description_preview(text, limit):
    flat = _flat(text)
    return flat if len(flat) <= limit else flat[:limit - 3] + "..."


def read_hint(roots, cwd):
    if not cwd:
        return None
    skills = layer.skills_dir(layer.PROJECT, roots.get(layer.PROJECT)).resolve()
    if Path(cwd).resolve().is_relative_to(skills.parent.parent):
        return None
    return redact.redact(_flat(
        f"[project] skills live outside this checkout, so the Skill tool cannot load them: "
        f"Read {skills}/<name>/SKILL.md instead."))


def load_documents(roots):
    """Read only agent frontmatter; a damaged/linked skill cannot hide other results."""
    documents, skipped = [], 0
    for lyr in config.active_layers():
        skills = layer.skills_dir(lyr, roots.get(lyr))
        if skills.is_symlink():
            continue
        for path in sorted(skills.glob(f"*/{skill_store.SKILL_FILE}")):
            name = path.parent.name
            try:
                layer._check_name(name)
                if _flat(name) != name or redact.secret_hits(name):
                    skipped += 1
                    continue
                meta = path.parent / sidecar.FILENAME
                if (path.parent.is_symlink() or path.is_symlink() or meta.is_symlink()
                        or path.parent.resolve().parent != skills.resolve()):
                    skipped += 1
                    continue
                if not meta.exists():
                    continue
                with meta.open("rb") as handle:
                    raw_meta = handle.read(MAX_METADATA_BYTES + 1)
                if len(raw_meta) > MAX_METADATA_BYTES:
                    skipped += 1
                    continue
                owner = json.loads(raw_meta)
                if not isinstance(owner, dict) or owner.get("created_by") != "agent":
                    continue
                with path.open("rb") as handle:
                    frontmatter = validate._frontmatter(handle.read(MAX_METADATA_BYTES).decode("utf-8", errors="replace"))
                if not frontmatter:
                    skipped += 1
                    continue
                documents.append({"name": name, "level": lyr,
                                  "description": redact.redact(frontmatter.get("description") or "(no description)")[:MAX_FIELD_CHARS],
                                  "category": redact.redact(_flat(frontmatter.get("category") or "general"))[:64]})
            except (OSError, ValueError, TypeError, RecursionError):
                skipped += 1
    return documents, skipped


def rank(documents, query):
    """Field-weighted BM25, k1=1.2, b=.75; ties prefer project then name."""
    all_terms = tokens(redact.redact(query[:MAX_QUERY_CHARS]))
    query_terms = sorted(list(dict.fromkeys(all_terms))[:MAX_QUERY_TERMS])
    fields = [{field: Counter(tokens(str(doc.get(field, ""))[:MAX_FIELD_CHARS])) for field in FIELD_WEIGHTS}
              for doc in documents]
    df = Counter(term for item in fields for term in set().union(*(set(value) for value in item.values())))
    lengths = [{field: sum(item[field].values()) for field in FIELD_WEIGHTS} for item in fields]
    averages = {field: sum(item[field] for item in lengths) / len(fields) if fields else 0
                for field in FIELD_WEIGHTS}
    result = []
    for doc, counts, sizes in zip(documents, fields, lengths, strict=True):
        contributions = []
        score = 0.0
        for term in query_terms:
            matched_fields, contribution = [], 0.0
            for field, weight in FIELD_WEIGHTS.items():
                frequency = counts[field][term]
                if not frequency:
                    continue
                matched_fields.append(field)
                length = sizes[field]
                idf = math.log1p((len(documents) - df[term] + .5) / (df[term] + .5))
                contribution += weight * idf * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * length / averages[field]))
            if matched_fields:
                score += contribution
                contributions.append({"term": term, "fields": matched_fields, "contribution": round(contribution, 6)})
        result.append({"document": doc, "score": score,
                       "matches": sorted(contributions, key=lambda item: (-item["contribution"], item["term"]))[:12]})
    return sorted(result, key=lambda item: (-item["score"], item["document"]["level"] != layer.PROJECT,
                                          item["document"]["name"]))


def _render(documents, mode, preview_chars, hint):
    if not documents:
        return None
    header = TASK_HEADER if mode == "task" else INDEX_HEADER
    lines = [header, ""]
    if mode == "catalog":
        categories = sorted({doc["category"] for doc in documents})
        for category in categories:
            lines.append(f"## {category}")
            for doc in sorted((item for item in documents if item["category"] == category), key=lambda item: (item["name"], item["level"])):
                lines.append(f"- {doc['name']} [{doc['level']}]: {description_preview(doc['description'], preview_chars)}")
    else:
        lines.extend(f"- {doc['name']} [{doc['level']}]: {description_preview(doc['description'], preview_chars)}" for doc in documents)
    if hint and any(doc["level"] == layer.PROJECT for doc in documents):
        lines.extend(("", hint))
    return "\n".join(lines)


def plan(documents, query="", *, mode="task", max_bytes=None, top_k=None, preview_chars=None, hint=None):
    """Pure selection API shared by hooks, CLI and the fixed-query evaluation."""
    if mode not in ("task", "catalog"):
        raise ValueError("unknown recall mode")
    max_bytes = config.RECALL_MAX_BYTES if max_bytes is None else max_bytes
    top_k = config.RECALL_TOP_K if top_k is None else top_k
    preview_chars = config.INDEX_DESC_MAX_CHARS if preview_chars is None else preview_chars
    if (type(max_bytes) is not int or max_bytes < 0 or type(top_k) is not int or top_k < 1
            or type(preview_chars) is not int or preview_chars < 4):
        raise ValueError("invalid recall limits")
    ranked = (rank(documents, query) if mode == "task" else
              [{"document": doc, "score": 0.0, "matches": []} for doc in sorted(documents, key=lambda doc: (doc["category"], doc["name"], doc["level"]))])
    selected, explanation = [], []
    for item in ranked:
        doc = item["document"]
        reason = "selected"
        if mode == "task" and item["score"] <= 0:
            reason = "no_match"
        elif len(selected) >= top_k:
            reason = "top_k"
        else:
            candidate = _render([*selected, doc], mode, preview_chars, hint)
            if len(candidate.encode("utf-8")) > max_bytes:
                reason = "budget"
            else:
                selected.append(doc)
        explanation.append({"name": doc["name"], "level": doc["level"], "category": doc["category"],
                            "score": round(item["score"], 6), "matches": item["matches"], "reason": reason})
    context = _render(selected, mode, preview_chars, hint)
    size = len((context or "").encode("utf-8"))
    return {"algorithm": ALGORITHM if mode == "task" else "category-name-v1", "mode": mode,
            "scoring": {"field_weights": dict(FIELD_WEIGHTS), "k1": 1.2, "b": .75,
                        "ties": "project before global, then name", "explanation_terms": 12} if mode == "task" else None,
            "context": context, "candidate_count": len(documents), "selected_count": len(selected),
            "usage": {"bytes": size, "max_bytes": max_bytes, "token_estimate": math.ceil(size / 4),
                      "token_estimator": "utf8-bytes-div-4"}, "ranked": explanation}


def retrieve(roots, query="", *, mode="task", cwd=None, max_bytes=None):
    documents, skipped = load_documents(roots)
    result = plan(documents, query, mode=mode, hint=read_hint(roots, cwd), max_bytes=max_bytes)
    result["skipped_count"] = skipped
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--query")
    source.add_argument("--stdin", action="store_true", help="read task text from stdin, without persisting it")
    source.add_argument("--catalog", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--global-root", type=Path)
    args = parser.parse_args()
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    query = sys.stdin.read(MAX_QUERY_CHARS) if args.stdin else args.query or ""
    result = retrieve({layer.PROJECT: args.root, layer.GLOBAL: args.global_root}, query,
                      mode="catalog" if args.catalog else "task")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

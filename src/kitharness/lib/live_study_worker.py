"""Fixed controller helper imported with a pinned plugin's PYTHONPATH.

No model execution. Only production bundle/promoter/library operations are used.
"""
import importlib
import json
import os
import sys
from pathlib import Path


def main():
    package = os.environ["KITHARNESS_SOURCE_PACKAGE"]
    config = importlib.import_module(package + ".config")
    promoter = importlib.import_module(package + ".hook.promoter")
    spawn = importlib.import_module(package + ".hook.spawn")
    layer = importlib.import_module(package + ".lib.layer")
    sidecar = importlib.import_module(package + ".lib.sidecar")
    validate = importlib.import_module(package + ".lib.validate")

    args = json.loads(sys.stdin.buffer.read(1_000_001))
    roots = {layer.PROJECT: Path(os.environ["KITHARNESS_PROJECT_ROOT"]),
             layer.GLOBAL: Path(os.environ["KITHARNESS_GLOBAL_ROOT"])}
    operation = args["operation"]
    if operation == "bundle":
        role = args["role"]
        if role not in ("reflector", "curator"):
            raise ValueError("invalid maintenance role")
        index = spawn.description_index(roots, agent_only=role == "curator")
        spec = config.FORMAT_SPEC.read_text(encoding="utf-8")
        if role == "curator":
            spawn._snapshot_skills(args["run_id"], roots)
            prompt = spawn.build_curator_bundle(index, spec)
        else:
            prompt = spawn.build_bundle(args["window"], index, spec)
        agent = Path(args["source"]) / "agents" / (role + ".md")
        text = agent.read_text(encoding="utf-8")
        system = text.split("---", 2)[2].strip()
        result = {"prompt": prompt, "system": system, "index": index,
                  "carrier": "pinned agent body and production bundle; explicit batch maintenance"}
    elif operation == "drain":
        result = {"verdicts": promoter.drain(args["run_id"], roots=roots, repo_name="live-evaluation")}
    elif operation == "inventory":
        skills = []
        for level in config.active_layers():
            directory = layer.skills_dir(level, roots[level])
            for path in sorted(directory.glob("*/SKILL.md")):
                meta = validate._frontmatter(path.read_text(encoding="utf-8")) or {}
                counter = sidecar.read(level, path.parent.name, roots[level])
                skills.append({"name": path.parent.name, "level": level, "description": meta.get("description"),
                               "use": counter.get("use", 0), "view": counter.get("view", 0), "patch": counter.get("patch", 0),
                               "bytes": path.stat().st_size})
        result = {"skills": skills, "skill_count": len(skills)}
    else:
        raise ValueError("invalid fixed operation")
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def test_install_entry_matches_the_two_manifest_names():
    plugin = _load(".claude-plugin/plugin.json")
    market = _load(".claude-plugin/marketplace.json")
    assert plugin["name"] == "autoharness"
    assert plugin["license"] == "MIT"
    assert market["name"] == "kitharness"
    assert [item["name"] for item in market["plugins"]] == ["autoharness"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert '<h1 align="center">KitHarness</h1>' in readme
    assert "/plugin install autoharness@kitharness" in readme
    assert "marketplace add tigerless-labs/autoharness" not in readme
    assert "https://github.com/tigerless-labs/autoharness" in readme


def test_upstream_license_and_measured_limits_stay_visible():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 ryan" in license_text
    project = (ROOT / "docs/project.md").read_text(encoding="utf-8")
    demo = (ROOT / "docs/demo.md").read_text(encoding="utf-8")
    assert "24/24" in project and "没有证明正确率提升" in project
    assert "tools/evaluate_recall.py" in demo
    assert "tools/evaluate_tasks.py smoke" in demo
    assert "tools/check_live_evidence.py" in demo

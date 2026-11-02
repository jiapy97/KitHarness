"""Validate the saved experiment archive and re-grade answers, without model calls."""
import argparse
import json
from pathlib import Path

from kitharness.lib.live_study_report import verify_archive

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--suite", type=Path, default=Path(__file__).resolve().parents[1] / "evals/tasks/suite.json")
    args = parser.parse_args()
    print(json.dumps(verify_archive(args.directory, args.suite)))

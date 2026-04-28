#!/usr/bin/env python3
"""Run the daily paper workflow.

This Python version generates the digest from the local queue. Zotero push is
kept as a future Python-only extension; no PowerShell bridge is used.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def run(args: argparse.Namespace) -> None:
    workspace = Path(args.workspace_root).resolve()
    digest_script = workspace / "automation" / "paper_digest.py"
    if not digest_script.exists():
        raise FileNotFoundError(f"Missing digest script: {digest_script}")
    subprocess.run([sys.executable, str(digest_script), "--workspace-root", str(workspace)], check=True)
    daily_pick_path = workspace / "data" / "paper-daily-picks.json"
    if not daily_pick_path.exists():
        raise FileNotFoundError(f"Missing daily pick file: {daily_pick_path}")
    print(f"Daily workflow completed. Picks: {daily_pick_path}")


def parse_args() -> argparse.Namespace:
    root_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Run daily paper digest workflow.")
    parser.add_argument("--workspace-root", default=str(root_default))
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())

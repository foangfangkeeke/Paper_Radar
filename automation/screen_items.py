#!/usr/bin/env python3
"""Screen merged WoS items with MiniMax.

This runner only parses CLI arguments. Core logic lives in minimax_screening_tool.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from minimax_screening_tool import screen_items_file


def parse_args() -> argparse.Namespace:
    root_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Screen merged WoS items with MiniMax and update paper queues.")
    parser.add_argument("--workspace", default=str(root_default))
    parser.add_argument("--input", default="data/wos_minimax_items.json")
    parser.add_argument("--min-push-score", type=int, default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rebuild-push", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    workspace = Path(args.workspace).resolve()

    def log(message: str) -> None:
        print(f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")

    queue = screen_items_file(
        workspace=workspace,
        input_path=args.input,
        min_push_score=args.min_push_score,
        limit=args.limit,
        dry_run=bool(args.dry_run),
        rebuild_push=bool(args.rebuild_push),
        log=log,
    )
    log(f"Done | pushQueueSize={len(queue)} | output={workspace / 'data' / 'paper_push_queue.json'}")


if __name__ == "__main__":
    main()

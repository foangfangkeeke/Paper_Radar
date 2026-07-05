#!/usr/bin/env python3
"""Find paper_items that have abstracts and are not yet in base_queue.

Usage:
  python skills/screen/find_candidates.py              # print JSON to stdout
  python skills/screen/find_candidates.py --count      # print count only
  python skills/screen/find_candidates.py --limit 10   # top 10 only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parents[2]
ITEMS_PATH = ROOT / "data" / "paper_items.json"
BASE_QUEUE_PATH = ROOT / "data" / "paper_base_queue.json"


def load_json(path: Path) -> list:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    p = argparse.ArgumentParser(
        description="Find paper_items needing preliminary screen")
    p.add_argument("--count", action="store_true",
                   help="Print count only")
    p.add_argument("--limit", type=int, default=0,
                   help="Limit to top N candidates")
    p.add_argument("--json", action="store_true",
                   help="Output as JSON array")
    p.add_argument("--items", default="",
                   help="Path to paper_items.json (overrides default)")
    p.add_argument("--base-queue", default="",
                   help="Path to paper_base_queue.json (overrides default)")
    args = p.parse_args()

    items_path = Path(args.items) if args.items else ITEMS_PATH
    base_queue_path = Path(args.base_queue) if args.base_queue else BASE_QUEUE_PATH

    items = load_json(items_path)
    base_queue = load_json(base_queue_path)

    # Build exclusion set from base_queue: Key + normalized title
    excluded_keys = set()
    excluded_titles = set()
    for entry in base_queue:
        key = entry.get("Key", "")
        if key:
            excluded_keys.add(key.lower())
        title = (entry.get("Title") or "").strip().lower()
        if title:
            excluded_titles.add(title)

    candidates = []
    for item in items:
        key = (item.get("Key") or "").lower()
        title = (item.get("TI") or "").strip().lower()
        abstract = (item.get("AB") or "").strip()

        # Must have abstract
        if not abstract:
            continue
        # Must not already be in base_queue
        if key and key in excluded_keys:
            continue
        if title and title in excluded_titles:
            continue

        candidates.append({
            "Key": item.get("Key", ""),
            "Title": item.get("TI", ""),
            "Journal": item.get("SO", ""),
            "Abstract": abstract,
        })

    if args.count:
        print(f"{len(candidates)} candidates / {len(items)} total items")
        return

    output = candidates[:args.limit] if args.limit else candidates

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        for i, c in enumerate(output):
            print(f"[{i+1}] [{c['Journal']}] {c['Title'][:100]}")
            print(f"    Key: {c['Key']}")
            print(f"    Abstract: {c['Abstract'][:200]}...")
            print()


if __name__ == "__main__":
    main()

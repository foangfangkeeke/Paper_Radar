#!/usr/bin/env python3
"""List papers in push_queue, optionally filtering to unpushed only.

Usage:
  python skills/screen/rank_push_queue.py --unpushed --json --limit 10 --offset 0   # first 10 unpushed
  python skills/screen/rank_push_queue.py --unpushed --json --limit 10 --offset 10  # next 10
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills"))
PUSH_QUEUE_PATH = ROOT / "data" / "paper_push_queue.json"

from cdp_client import extract_doi


def load_json(path: Path):
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    p = argparse.ArgumentParser(
        description="List papers in push_queue")
    p.add_argument("--unpushed", action="store_true",
                   help="Only show unpushed papers")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    p.add_argument("--limit", type=int, default=10,
                   help="Limit to top N papers (default: 10)")
    p.add_argument("--offset", type=int, default=0,
                   help="Skip first N papers (for pagination)")
    p.add_argument("--push-queue", default="",
                   help="Path to paper_push_queue.json (overrides default)")
    args = p.parse_args()

    queue_path = Path(args.push_queue) if args.push_queue else PUSH_QUEUE_PATH
    papers = load_json(queue_path)

    if args.unpushed:
        papers = [p for p in papers if not p.get("PushedAt")]

    if args.offset:
        papers = papers[args.offset:]

    if args.limit:
        papers = papers[:args.limit]

    if args.json:
        out = []
        for paper in papers:
            out.append({
                "Key": paper.get("Key"),
                "Title": paper.get("Title"),
                "Journal": paper.get("Journal"),
                "Score": paper.get("Score"),
                "MatchedDirections": paper.get("MatchedDirections"),
                "Tags": paper.get("Tags"),
                "Comment": paper.get("Comment"),
                "PushedAt": paper.get("PushedAt"),
            })
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    for i, paper in enumerate(papers):
        title = paper.get("Title", "?")[:100]
        journal = paper.get("Journal", "?")
        doi = extract_doi(paper.get("Key", "")) or ""
        pushed = paper.get("PushedAt", "")
        mark = " [PUSHED]" if pushed else ""
        print(f"[{i+1}] Score={paper.get('Score', '?')} [{journal}] {title}{mark}")
        print(f"    DOI: {doi}")
        print()


if __name__ == "__main__":
    main()

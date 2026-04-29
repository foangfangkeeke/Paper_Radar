#!/usr/bin/env python3
"""
Create a push digest from data/paper_push_queue.json.

This is the first lightweight push executor. It does not send email/WeChat yet;
it writes a Markdown digest without mutating queue items.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve(workspace: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else workspace / path


def is_pending(item: dict[str, Any]) -> bool:
    return True


def score_of(item: dict[str, Any]) -> int:
    try:
        return int(item.get("Score", item.get("score", item.get("RecommendationScore", 0))) or 0)
    except Exception:
        return 0


def item_title(item: dict[str, Any]) -> str:
    return clean_text(item.get("TI") or item.get("Title") or "Untitled")


def item_journal(item: dict[str, Any]) -> str:
    return clean_text(item.get("SO") or item.get("Journal") or "Unknown journal")


def item_abstract(item: dict[str, Any]) -> str:
    return clean_text(item.get("AB") or item.get("Abstract") or "")


def item_comment(item: dict[str, Any]) -> str:
    return clean_text(item.get("comment") or item.get("Comment") or item.get("LlmScreen", {}).get("Comment") or "")


def item_tags(item: dict[str, Any]) -> dict[str, Any]:
    tags = item.get("Tags")
    if isinstance(tags, dict):
        return tags
    tags = item.get("tags")
    if isinstance(tags, dict):
        return tags
    llm = item.get("LlmScreen")
    if isinstance(llm, dict) and isinstance(llm.get("Tags"), dict):
        return llm["Tags"]
    return {}


def trim(text: str, max_chars: int) -> str:
    text = clean_text(text)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def markdown_for_items(items: list[dict[str, Any]], title: str, abstract_chars: int) -> str:
    now = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    lines = [f"# {title}", "", f"Generated at: {now}", "", f"Total: {len(items)}", ""]
    for idx, item in enumerate(items, start=1):
        tags = item_tags(item)
        tag_parts: list[str] = []
        for key, value in tags.items():
            if isinstance(value, list):
                text = ", ".join(clean_text(v) for v in value if clean_text(v))
            else:
                text = clean_text(value)
            if text:
                tag_parts.append(f"**{key}**: {text}")
        lines.extend(
            [
                f"## {idx}. {item_title(item)}",
                "",
                f"- **Journal**: {item_journal(item)}",
                f"- **Score**: {score_of(item)}",
                f"- **Key**: `{clean_text(item.get('key') or item.get('Key'))}`",
            ]
        )
        if item_comment(item):
            lines.append(f"- **Comment**: {item_comment(item)}")
        if tag_parts:
            lines.append(f"- **Tags**: {'; '.join(tag_parts)}")
        if item_abstract(item):
            lines.extend(["", trim(item_abstract(item), abstract_chars)])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def push_items(
    workspace: Path,
    push_queue_path: Path,
    out_md_path: Path,
    count: int,
    dry_run: bool,
    keep_pending: bool,
    abstract_chars: int,
) -> None:
    queue = read_json(push_queue_path, [])
    if not isinstance(queue, list):
        raise RuntimeError(f"Push queue must be a list: {push_queue_path}")

    pending = [item for item in queue if isinstance(item, dict) and is_pending(item)]
    pending.sort(key=lambda item: (score_of(item), str(item.get("screened_at") or item.get("queued_at") or "")), reverse=True)
    selected = pending[: max(0, count)] if count > 0 else pending

    out_md_path.parent.mkdir(parents=True, exist_ok=True)
    digest = markdown_for_items(selected, title="Paper Radar Push Digest", abstract_chars=abstract_chars)
    out_md_path.write_text(digest, encoding="utf-8")

    print(f"pending={len(pending)} selected={len(selected)} dryRun={dry_run} keepPending={keep_pending}")
    print(f"digest={out_md_path}")

    if not dry_run and not keep_pending:
        print("queueUnchanged=true")


def main() -> None:
    root_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Generate a Markdown paper push digest from paper_push_queue.json.")
    parser.add_argument("--workspace", default=str(root_default))
    parser.add_argument("--push-queue", default="data/paper_push_queue.json")
    parser.add_argument("--out-md", default="")
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--abstract-chars", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true", help="Write digest but do not mark selected papers as pushed.")
    parser.add_argument("--keep-pending", action="store_true", help="Write digest but keep selected papers pending.")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    out_md = args.out_md or f"reports/push_{dt.date.today().isoformat()}.md"
    push_items(
        workspace=workspace,
        push_queue_path=resolve(workspace, args.push_queue),
        out_md_path=resolve(workspace, out_md),
        count=int(args.count),
        dry_run=bool(args.dry_run),
        keep_pending=bool(args.keep_pending),
        abstract_chars=int(args.abstract_chars),
    )


if __name__ == "__main__":
    main()

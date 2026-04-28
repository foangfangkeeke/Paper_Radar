#!/usr/bin/env python3
"""Generate a daily paper digest from the local candidate queue."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def keyword_label(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("name") or entry.get("key") or entry.get("query") or "")
    return ""


def configured_directions(config: dict[str, Any]) -> list[str]:
    return [keyword_label(item) for item in config.get("keywords", []) if keyword_label(item)]


def date_key(value: Any) -> str:
    return str(value or "")


def clean_value(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalize_tags(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    tags: dict[str, Any] = {}
    for key, raw in value.items():
        name = clean_value(key)
        if not name:
            continue
        if isinstance(raw, list):
            items = [clean_value(item) for item in raw if clean_value(item)]
            if items:
                tags[name] = items
        else:
            text = clean_value(raw)
            if text:
                tags[name] = text
    return tags


def paper_tag_summary(paper: dict[str, Any]) -> dict[str, Any]:
    screen = paper.get("LlmScreen") if isinstance(paper.get("LlmScreen"), dict) else {}
    tags = normalize_tags(screen.get("Tags") if isinstance(screen, dict) else {})
    # Reason is kept only as a fallback for old queue files written before schema v3.
    comment = clean_value(screen.get("Comment") or screen.get("Reason") or "")
    return {"Tags": tags, "Comment": comment}


def render_tag_lines(tags: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for field, value in tags.items():
        rendered = "; ".join(value) if isinstance(value, list) else clean_value(value)
        if rendered:
            lines.append(f"{field}: {rendered}")
    return lines


def run(args: argparse.Namespace) -> None:
    workspace = Path(args.workspace_root).resolve()
    config = read_json(workspace / "automation" / "paper-watch.config.json", {})
    queue_path = workspace / "data" / "paper-candidate-queue.json"
    screening = config.get("llmScreening", {})
    history_path = resolve_path(workspace, screening.get("pushHistoryPath", "data/paper-push-history.json"))
    reports_dir = workspace / "reports"
    data_dir = workspace / "data"
    reports_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    queue = read_json(queue_path, [])
    if not isinstance(queue, list):
        queue = []
    history_data = read_json(history_path, {"pushed": []})
    history = history_data.get("pushed", []) if isinstance(history_data, dict) else []
    if not isinstance(history, list):
        history = []
    pushed_keys = {str(item.get("Key") or item.get("key") or "") for item in history if isinstance(item, dict)}
    pushed_keys.discard("")

    daily_pick_count = int(config.get("dailyPickCount", 1))
    directions = configured_directions(config)

    daily_picks: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    for direction_name in directions:
        candidates = [
            paper
            for paper in queue
            if direction_name in (paper.get("MatchedDirections") or [])
            and str(paper.get("Key", "")) not in pushed_keys
            and str(paper.get("Key", "")) not in selected_keys
        ]
        candidates.sort(key=lambda paper: (int(paper.get("RecommendationScore", 0)), date_key(paper.get("DateObject"))), reverse=True)
        for paper in candidates[:daily_pick_count]:
            tag_summary = paper_tag_summary(paper)
            daily_picks.append(
                {
                    "Direction": direction_name,
                    "Paper": paper,
                    "Score": int(paper.get("RecommendationScore", 0)),
                    "ScoreBreakdown": paper.get("ScoreBreakdown"),
                    "TagSummary": tag_summary,
                }
            )
            selected_keys.add(str(paper.get("Key", "")))

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = reports_dir / f"paper_digest_{timestamp}.md"
    lines = [
        "# Paper Digest",
        "",
        f"Generated at: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Workspace: {workspace}",
        f"Queue size: {len(queue)}",
        f"Already pushed: {len(pushed_keys)}",
        "",
        "## Daily Focus Picks",
    ]
    if not daily_picks:
        lines.append("No unpushed paper is available in the local queue.")
    else:
        for idx, pick in enumerate(daily_picks, start=1):
            paper = pick["Paper"]
            tags = pick["TagSummary"]
            lines.extend(
                [
                    f"### Pick {idx}: {pick['Direction']}",
                    f"Title: {paper.get('Title', '')}",
                    f"Source: {paper.get('Source', '')}",
                    f"Venue: {paper.get('Journal', '')}",
                    f"Date: {paper.get('Date', '')}",
                    f"Recommendation score: {pick['Score']}",
                ]
            )
            breakdown = paper.get("ScoreBreakdown") or {}
            if breakdown:
                lines.append(
                    "Score formula: "
                    f"llm {breakdown.get('LlmScore', 0)} + "
                    f"journal {breakdown.get('JournalQuality', 0)}"
                )
            if paper.get("Url"):
                lines.append(f"Link: {paper['Url']}")
            if paper.get("CodeUrl"):
                lines.append(f"Code: {paper['CodeUrl']}")
            lines.extend(["", "[Tags]"])
            lines.extend(render_tag_lines(tags.get("Tags", {})))
            lines.extend(["", "Comment:", tags.get("Comment", ""), ""])
            history.append(
                {
                    "Key": paper.get("Key"),
                    "Title": paper.get("Title"),
                    "DOI": paper.get("DOI"),
                    "Direction": pick["Direction"],
                    "PushedAt": dt.datetime.now().astimezone().isoformat(),
                    "ReportPath": str(report_path),
                }
            )

    remaining = len([paper for paper in queue if str(paper.get("Key", "")) not in pushed_keys and str(paper.get("Key", "")) not in selected_keys])
    lines.extend(["", "## Queue Status", f"Remaining unpushed papers: {remaining}", f"Queue file: {queue_path}", f"Push history file: {history_path}"])
    write_json(data_dir / "paper-daily-picks.json", {"generatedAt": dt.datetime.now().astimezone().isoformat(), "dailyPicks": daily_picks, "queueSize": len(queue), "pushedSize": len(history)})
    write_json(history_path, {"generatedAt": dt.datetime.now().astimezone().isoformat(), "pushed": history})
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Digest generated from local queue: {report_path}")


def parse_args() -> argparse.Namespace:
    root_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Generate daily paper digest from local queue.")
    parser.add_argument("--workspace-root", default=str(root_default))
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())

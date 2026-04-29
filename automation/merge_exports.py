#!/usr/bin/env python3
"""
Merge and deduplicate Web of Science plain-text exports for MiniMax screening.

Default input folders, relative to project root:
  reports/inbox/     if it exists, otherwise reports/ for backward compatibility
  data/source_exports/

Archive behavior:
  By default, successfully parsed txt files under reports/ are moved to
  reports/archive/YYYY-MM-DD/ after the merged JSON and stats JSON are written.
  Archived files are never scanned again.

Main output:
  data/wos_minimax_items.json

Each output item contains:
  Key, TI, SO, AB
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
from pathlib import Path
from typing import Any


ARCHIVE_FOLDER_NAME = "archive"


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_title(value: str) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_wos_plain_text(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    current_tag = ""

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")

        if line == "ER":
            if current:
                records.append(current)
            current = {}
            current_tag = ""
            continue

        if not line or line.startswith("FN ") or line.startswith("VR ") or line == "EF":
            continue

        tag = line[:2].strip()
        if len(line) >= 3 and tag:
            value = line[3:].strip() if len(line) > 3 else ""
            current_tag = tag
            if tag in current and value:
                current[tag] = clean_text(current[tag] + " " + value)
            else:
                current[tag] = clean_text(value)
            continue

        if current_tag and line.startswith(" "):
            current[current_tag] = clean_text(current.get(current_tag, "") + " " + line.strip())

    if current:
        records.append(current)

    return records


def record_to_minimax_item(raw: dict[str, str]) -> dict[str, str] | None:
    title = clean_text(raw.get("TI"))
    journal = clean_text(raw.get("SO") or raw.get("JI") or raw.get("J9"))
    abstract = clean_text(raw.get("AB"))

    if not title or not journal:
        return None

    doi = clean_text(raw.get("DI"))
    accession = clean_text(raw.get("UT"))
    if doi:
        key = "doi:" + doi.lower()
    elif accession:
        key = "wos:" + accession.lower()
    else:
        key = "title:" + normalize_title(title)

    return {"Key": key, "TI": title, "SO": journal, "AB": abstract}


def is_inside_archive(path: Path) -> bool:
    return ARCHIVE_FOLDER_NAME.lower() in {part.lower() for part in path.parts}


def find_export_files(input_dirs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for folder in input_dirs:
        if not folder.exists():
            continue
        if folder.is_file() and folder.suffix.lower() == ".txt" and not is_inside_archive(folder):
            files.append(folder.resolve())
            continue
        if folder.is_dir():
            for path in folder.rglob("*.txt"):
                if path.is_file() and not is_inside_archive(path):
                    files.append(path.resolve())
    return sorted(set(files))


def merge_wos_exports(input_dirs: list[Path]) -> tuple[list[dict[str, str]], dict[str, Any], list[Path]]:
    files = find_export_files(input_dirs)
    merged: dict[str, dict[str, str]] = {}
    imported = 0
    skipped = 0
    parsed_files: list[Path] = []

    for path in files:
        try:
            raws = parse_wos_plain_text(path)
        except Exception as exc:
            skipped += 1
            print(f"WARNING: failed to parse {path}: {exc}")
            continue

        parsed_files.append(path)
        imported += len(raws)
        for raw in raws:
            item = record_to_minimax_item(raw)
            if item is None:
                skipped += 1
                continue

            key = item["Key"]
            existing = merged.get(key)
            if existing is None:
                merged[key] = item
            elif not existing.get("AB") and item.get("AB"):
                merged[key] = item

    items = sorted(merged.values(), key=lambda x: (str(x.get("SO") or ""), str(x.get("TI") or "")))
    stats = {
        "files_found": len(files),
        "files_parsed": len(parsed_files),
        "imported_records": imported,
        "deduped_records": len(items),
        "duplicates_removed": max(0, imported - len(items)),
        "skipped_records_or_files": skipped,
    }
    return items, stats, parsed_files


def unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}__{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def should_archive_file(path: Path, reports_dir: Path, archive_scope: str) -> bool:
    if archive_scope == "none":
        return False
    if is_inside_archive(path):
        return False
    try:
        path.resolve().relative_to(reports_dir.resolve())
        return archive_scope in {"reports", "all"}
    except ValueError:
        return archive_scope == "all"


def archive_files(files: list[Path], reports_dir: Path, archive_dir: Path, archive_scope: str) -> dict[str, Any]:
    moved = 0
    failed = 0
    today_folder = archive_dir / dt.date.today().isoformat()
    today_folder.mkdir(parents=True, exist_ok=True)

    for path in files:
        if not should_archive_file(path, reports_dir, archive_scope):
            continue
        if not path.exists():
            continue
        dest = unique_destination(today_folder / path.name)
        try:
            shutil.move(str(path), str(dest))
            moved += 1
        except Exception as exc:
            failed += 1
            print(f"WARNING: failed to archive {path} -> {dest}: {exc}")

    return {"archived_files": moved, "archive_failed": failed, "archive_dir": str(today_folder)}


def default_input_dirs(workspace: Path) -> list[Path]:
    reports = workspace / "reports"
    inbox = reports / "inbox"
    # If reports/inbox exists, use it as the active intake folder. Otherwise use
    # reports/ once for backward compatibility with your current folder layout.
    active_reports = inbox if inbox.exists() else reports
    return [active_reports, workspace / "data" / "source_exports"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge and deduplicate WoS plain-text exports for MiniMax.")
    parser.add_argument(
        "--workspace",
        default=str(Path(__file__).resolve().parents[1]),
        help="Project root. Default: parent folder of automation/.",
    )
    parser.add_argument("--input", action="append", default=[], help="Input folder or txt file. Can be repeated.")
    parser.add_argument("--out-json", default="data/wos_minimax_items.json", help="Output JSON path, relative to workspace unless absolute.")
    parser.add_argument("--no-archive", action="store_true", help="Do not move processed txt files after a successful merge.")
    parser.add_argument(
        "--archive-scope",
        choices=["reports", "all", "none"],
        default="reports",
        help="Which processed txt files to archive. Default: reports only.",
    )
    parser.add_argument("--archive-dir", default="reports/archive", help="Archive folder, relative to workspace unless absolute.")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    input_dirs = [Path(p).resolve() if Path(p).is_absolute() else workspace / p for p in args.input]
    if not input_dirs:
        input_dirs = default_input_dirs(workspace)

    out_json = Path(args.out_json)
    if not out_json.is_absolute():
        out_json = workspace / out_json

    archive_dir = Path(args.archive_dir)
    if not archive_dir.is_absolute():
        archive_dir = workspace / archive_dir

    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    items, stats, parsed_files = merge_wos_exports(input_dirs)

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    archive_stats: dict[str, Any] = {"archived_files": 0, "archive_failed": 0, "archive_dir": str(archive_dir)}
    if not args.no_archive and args.archive_scope != "none":
        archive_stats = archive_files(parsed_files, reports_dir, archive_dir, args.archive_scope)

    final_stats = {
        **stats,
        **archive_stats,
        "inputs": [str(path) for path in input_dirs],
        "output_json": str(out_json),
    }
    print("WoS merge complete")
    print(json.dumps(final_stats, ensure_ascii=False, indent=2))
    print(f"JSON : {out_json}")


if __name__ == "__main__":
    main()

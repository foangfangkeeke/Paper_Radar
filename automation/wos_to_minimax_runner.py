#!/usr/bin/env python3
"""Run WoS browser export, parse exports, and screen papers with MiniMax."""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from crossref_fallback_tool import fetch_crossref_fallback_exports
from minimax_screening_tool import parse_wos_exports_and_screen
from wos_browser_tool import fetch_wos_from_project_configs


def parse_args() -> argparse.Namespace:
    root_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Fetch WoS exports and screen them with MiniMax.")
    parser.add_argument("--workspace", default=str(root_default), help="Project root path.")
    parser.add_argument("--start-date", default="2023-01-01", help="Publication Date start, YYYY-MM-DD.")
    parser.add_argument("--end-date", default=dt.date.today().isoformat(), help="Publication Date end, YYYY-MM-DD.")
    parser.add_argument("--skip-wos", action="store_true", help="Reuse existing data/source_exports/*.txt.")
    parser.add_argument("--no-crossref-fallback", action="store_true", help="Do not use Crossref when WoS export fails.")
    parser.add_argument("--min-push-score", type=int, default=None)
    parser.add_argument("--no-legacy", action="store_true")
    return parser.parse_args()


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(str(value)[:10])


def main() -> None:
    args = parse_args()
    workspace = Path(args.workspace).resolve()
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)

    def log(message: str) -> None:
        print(f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")

    if args.skip_wos:
        export_files = sorted((workspace / "data" / "source_exports").glob("*.txt"))
        log(f"Reusing existing WoS export files | files={len(export_files)}")
    else:
        try:
            export_files = fetch_wos_from_project_configs(
                workspace=workspace,
                start_date=start_date,
                end_date=end_date,
                log=log,
            )
        except Exception as exc:
            if args.no_crossref_fallback:
                raise
            log(f"WARNING: WoS fetch failed; switching to Crossref fallback. reason={exc}")
            export_files = fetch_crossref_fallback_exports(
                workspace=workspace,
                start_date=start_date,
                end_date=end_date,
                log=log,
            )

    if not export_files and not args.no_crossref_fallback:
        log("WARNING: no WoS export files available; switching to Crossref fallback.")
        export_files = fetch_crossref_fallback_exports(
            workspace=workspace,
            start_date=start_date,
            end_date=end_date,
            log=log,
        )

    if not export_files:
        raise RuntimeError("No WoS export txt files found.")

    queue = parse_wos_exports_and_screen(
        export_files=export_files,
        workspace=workspace,
        start_date=start_date,
        end_date=end_date,
        min_push_score=args.min_push_score,
        write_legacy=not bool(args.no_legacy),
        log=log,
    )
    log(f"Done | pushQueueSize={len(queue)} | output={workspace / 'data' / 'paper_push_queue.json'}")


if __name__ == "__main__":
    main()

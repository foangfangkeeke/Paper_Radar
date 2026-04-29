#!/usr/bin/env python3
"""CLI runner for WoS Plain Text export."""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from crossref_fallback_tool import fetch_crossref_fallback_exports
from wos_browser_tool import fetch_wos_from_project_configs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run WoS browser export.")
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--start-date", default="2023-01-01", help="Publication Date start, YYYY-MM-DD.")
    parser.add_argument("--end-date", default=dt.date.today().isoformat(), help="Publication Date end, YYYY-MM-DD.")
    parser.add_argument("--run-id", default="", help="Optional run id under data/source_exports/runs/.")
    parser.add_argument("--no-crossref-fallback", action="store_true", help="Do not use Crossref when WoS export fails.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    workspace = Path(args.workspace).resolve()
    start_date = dt.date.fromisoformat(str(args.start_date)[:10])
    end_date = dt.date.fromisoformat(str(args.end_date)[:10])
    run_id = args.run_id.strip() or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = workspace / "data" / "source_exports" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        files = fetch_wos_from_project_configs(
            workspace=workspace,
            start_date=start_date,
            end_date=end_date,
            download_dir=run_dir,
        )
    except Exception as exc:
        if args.no_crossref_fallback:
            raise
        print(f"WARNING: WoS export failed; switching to Crossref fallback. reason={exc}")
        files = fetch_crossref_fallback_exports(
            workspace=workspace,
            start_date=start_date,
            end_date=end_date,
            output_dir=run_dir,
        )
    if not files and not args.no_crossref_fallback:
        print("WARNING: WoS export produced no files; switching to Crossref fallback.")
        files = fetch_crossref_fallback_exports(
            workspace=workspace,
            start_date=start_date,
            end_date=end_date,
            output_dir=run_dir,
        )
    print(f"Source exports | runId={run_id} | runDir={run_dir}")
    for path in files:
        print(path)


if __name__ == "__main__":
    main()

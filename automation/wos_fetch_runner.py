#!/usr/bin/env python3
"""Small runner for the extracted WoS browser export tool."""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from wos_browser_tool import fetch_wos_from_project_configs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the extracted WoS browser export tool.")
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--start-date", default="2023-01-01", help="Publication Date start, YYYY-MM-DD.")
    parser.add_argument("--end-date", default=dt.date.today().isoformat(), help="Publication Date end, YYYY-MM-DD.")
    # Backward-compatible aliases. If provided, they are converted to Jan 1 / Dec 31.
    parser.add_argument("--start-year", type=int, default=None)
    parser.add_argument("--end-year", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    start_date = args.start_date
    end_date = args.end_date
    if args.start_year is not None:
        start_date = f"{args.start_year:04d}-01-01"
    if args.end_year is not None:
        end_date = f"{args.end_year:04d}-12-31"

    files = fetch_wos_from_project_configs(
        workspace=args.workspace,
        start_date=start_date,
        end_date=end_date,
    )
    print("WoS exports:")
    for path in files:
        print(path)


if __name__ == "__main__":
    main()

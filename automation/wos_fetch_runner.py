#!/usr/bin/env python3
"""CLI runner for WoS Plain Text export."""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from wos_browser_tool import fetch_wos_from_project_configs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run WoS browser export.")
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--start-date", default="2023-01-01", help="Publication Date start, YYYY-MM-DD.")
    parser.add_argument("--end-date", default=dt.date.today().isoformat(), help="Publication Date end, YYYY-MM-DD.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    files = fetch_wos_from_project_configs(
        workspace=args.workspace,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    print("WoS exports:")
    for path in files:
        print(path)


if __name__ == "__main__":
    main()

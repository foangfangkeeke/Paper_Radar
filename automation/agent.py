#!/usr/bin/env python3
"""
Paper Radar workflow agent.

Small deterministic workflow agent. Default weekly flow:
  fetch -> merge -> dry-run screen -> confirmed screen

WoS browser automation is available through the `fetch` step, but can be
skipped when you have manually downloaded WoS txt files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path


def run_cmd(cmd: list[str], cwd: Path) -> None:
    print("\n$ " + " ".join(cmd))
    completed = subprocess.run(cmd, cwd=str(cwd))
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def ask_yes(prompt: str) -> bool:
    answer = input(prompt).strip().lower()
    return answer in {"y", "yes"}


def resolve_dates(args: argparse.Namespace) -> tuple[str, str]:
    start_date = getattr(args, "start_date", None) or "2023-01-01"
    end_date = getattr(args, "end_date", None) or dt.date.today().isoformat()
    start_year = getattr(args, "start_year", None)
    end_year = getattr(args, "end_year", None)
    if start_year is not None:
        start_date = f"{start_year:04d}-01-01"
    if end_year is not None:
        end_date = f"{end_year:04d}-12-31"
    return start_date, end_date


def add_date_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start-date", default="2023-01-01", help="WoS Publication Date start, YYYY-MM-DD.")
    parser.add_argument("--end-date", default=dt.date.today().isoformat(), help="WoS Publication Date end, YYYY-MM-DD.")
    parser.add_argument("--start-year", type=int, default=None, help="Backward-compatible alias for --start-date YYYY-01-01.")
    parser.add_argument("--end-year", type=int, default=None, help="Backward-compatible alias for --end-date YYYY-12-31.")


def build_fetch_cmd(py: str, args: argparse.Namespace) -> list[str]:
    start_date, end_date = resolve_dates(args)
    return [
        py,
        "automation/wos_fetch_runner.py",
        "--start-date",
        start_date,
        "--end-date",
        end_date,
    ]


def build_merge_cmd(py: str, no_archive: bool) -> list[str]:
    cmd = [py, "automation/merge_exports.py"]
    if no_archive:
        cmd.append("--no-archive")
    return cmd


def build_screen_cmd(py: str, args: argparse.Namespace, dry_run: bool = False) -> list[str]:
    cmd = [py, "automation/screen_items.py"]
    if dry_run:
        cmd.append("--dry-run")
    if getattr(args, "limit", 0):
        cmd += ["--limit", str(args.limit)]
    if getattr(args, "min_push_score", 0):
        cmd += ["--min-push-score", str(args.min_push_score)]
    if getattr(args, "rebuild_push", False):
        cmd.append("--rebuild-push")
    if getattr(args, "no_legacy", False):
        cmd.append("--no-legacy")
    return cmd


def main() -> None:
    workspace_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Paper Radar workflow agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", default=str(workspace_default))

    p_fetch = sub.add_parser("fetch", parents=[common], help="Fetch WoS exports using browser automation.")
    add_date_args(p_fetch)

    p_merge = sub.add_parser("merge", parents=[common], help="Merge WoS exports into wos_minimax_items.json.")
    p_merge.add_argument("--no-archive", action="store_true")

    p_screen = sub.add_parser("screen", parents=[common], help="Screen new papers with MiniMax.")
    p_screen.add_argument("--dry-run", action="store_true")
    p_screen.add_argument("--limit", type=int, default=0)
    p_screen.add_argument("--min-push-score", type=int, default=0)
    p_screen.add_argument("--rebuild-push", action="store_true")
    p_screen.add_argument("--no-legacy", action="store_true")

    p_weekly = sub.add_parser(
        "weekly-update",
        parents=[common],
        help="Fetch WoS, merge exports, preview new papers, then optionally screen with MiniMax.",
    )
    add_date_args(p_weekly)
    p_weekly.add_argument("--skip-fetch", action="store_true", help="Skip WoS browser fetch; use existing downloaded txt files.")
    p_weekly.add_argument("--yes", action="store_true", help="Do not ask before MiniMax screening.")
    p_weekly.add_argument("--no-archive", action="store_true")
    p_weekly.add_argument("--limit", type=int, default=0)
    p_weekly.add_argument("--min-push-score", type=int, default=0)
    p_weekly.add_argument("--no-legacy", action="store_true")

    p_push = sub.add_parser("push", parents=[common], help="Generate a Markdown digest from pending push queue.")
    p_push.add_argument("--count", type=int, default=2)
    p_push.add_argument("--dry-run", action="store_true")
    p_push.add_argument("--keep-pending", action="store_true")

    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    py = sys.executable

    if args.command == "fetch":
        run_cmd(build_fetch_cmd(py, args), workspace)
        return

    if args.command == "merge":
        run_cmd(build_merge_cmd(py, args.no_archive), workspace)
        return

    if args.command == "screen":
        run_cmd(build_screen_cmd(py, args, dry_run=args.dry_run), workspace)
        return

    if args.command == "weekly-update":
        if not args.skip_fetch:
            run_cmd(build_fetch_cmd(py, args), workspace)
        else:
            print("Skipping WoS fetch; using existing downloaded txt files.")

        run_cmd(build_merge_cmd(py, args.no_archive), workspace)
        run_cmd(build_screen_cmd(py, args, dry_run=True), workspace)

        if args.yes or ask_yes("Continue to MiniMax screening? Type y to continue: "):
            run_cmd(build_screen_cmd(py, args, dry_run=False), workspace)
        else:
            print("Stopped before MiniMax screening.")
        return

    if args.command == "push":
        cmd = [py, "automation/push_items.py", "--count", str(args.count)]
        if args.dry_run:
            cmd.append("--dry-run")
        if args.keep_pending:
            cmd.append("--keep-pending")
        run_cmd(cmd, workspace)
        return


if __name__ == "__main__":
    main()

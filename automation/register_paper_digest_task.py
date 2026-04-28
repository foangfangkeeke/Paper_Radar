#!/usr/bin/env python3
"""Register Windows scheduled tasks for the paper radar workflow."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def schtasks_delete(name: str) -> None:
    subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def schtasks_create(name: str, command: str, schedule: str, time_value: str, days: str | None = None) -> None:
    args = ["schtasks", "/Create", "/TN", name, "/TR", command, "/SC", schedule, "/ST", time_value, "/F"]
    if days:
        args.extend(["/D", days])
    subprocess.run(args, check=True)


def run(args: argparse.Namespace) -> None:
    workspace = Path(args.workspace_root).resolve()
    python_exe = Path(args.python_exe).resolve()
    digest_script = workspace / "automation" / "paper_workflow.py"
    queue_script = workspace / "automation" / "paper_queue_build.py"
    if not python_exe.exists():
        raise FileNotFoundError(f"Missing crawler39 Python executable: {python_exe}")
    if not digest_script.exists():
        raise FileNotFoundError(f"Missing workflow script: {digest_script}")
    if not queue_script.exists():
        raise FileNotFoundError(f"Missing queue build script: {queue_script}")

    digest_command = f'"{python_exe}" "{digest_script}" --workspace-root "{workspace}"'
    queue_command = f'"{python_exe}" "{queue_script}" --mode Weekly'

    schtasks_delete(args.task_name)
    digest_days = "MON,TUE,WED,THU,FRI" if args.weekdays_only else None
    schtasks_create(args.task_name, digest_command, "WEEKLY" if args.weekdays_only else "DAILY", args.daily_at, digest_days)
    print(f"Scheduled task registered: {args.task_name} at {args.daily_at} (weekdaysOnly={args.weekdays_only})")

    schtasks_delete(args.weekly_queue_task_name)
    schtasks_create(args.weekly_queue_task_name, queue_command, "WEEKLY", args.weekly_queue_at, args.weekly_queue_day[:3].upper())
    print(f"Scheduled task registered: {args.weekly_queue_task_name} every {args.weekly_queue_day} at {args.weekly_queue_at}")
    print(f'Manual full build: "{python_exe}" "{queue_script}" --mode Full')
    print(f'Manual daily workflow: "{python_exe}" "{digest_script}" --workspace-root "{workspace}"')


def parse_args() -> argparse.Namespace:
    root_default = Path(__file__).resolve().parents[1]
    default_python = Path.home() / ".conda" / "envs" / "crawler39" / "python.exe"
    parser = argparse.ArgumentParser(description="Register paper radar Windows scheduled tasks.")
    parser.add_argument("--workspace-root", default=str(root_default))
    parser.add_argument("--task-name", default="PaperDigestTask")
    parser.add_argument("--daily-at", default="09:00")
    parser.add_argument("--weekdays-only", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--weekly-queue-task-name", default="PaperQueueWeeklyTask")
    parser.add_argument("--weekly-queue-at", default="08:30")
    parser.add_argument("--weekly-queue-day", choices=["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"], default="Monday")
    parser.add_argument("--python-exe", default=str(default_python))
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())

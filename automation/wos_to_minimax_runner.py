#!/usr/bin/env python3
"""Run WoS browser export, parse exports, and screen papers with MiniMax."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from abstract_enrichment_tool import enrich_abstracts
from crossref_fallback_tool import fetch_crossref_fallback_exports
from merge_exports import merge_wos_exports
from minimax_screening_tool import screen_items_file
from wos_browser_tool import fetch_wos_from_project_configs


def parse_args() -> argparse.Namespace:
    root_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Fetch WoS exports and screen them with MiniMax.")
    parser.add_argument("--workspace", default=str(root_default), help="Project root path.")
    parser.add_argument("--start-date", default="2023-01-01", help="Publication Date start, YYYY-MM-DD.")
    parser.add_argument("--end-date", default=dt.date.today().isoformat(), help="Publication Date end, YYYY-MM-DD.")
    parser.add_argument("--skip-wos", action="store_true", help="Reuse existing txt files in the current source export folder.")
    parser.add_argument("--run-id", default="", help="Optional run id under data/source_exports/runs/.")
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

    source_runs_dir = workspace / "data" / "source_exports" / "runs"
    if args.skip_wos and not args.run_id.strip():
        existing_runs = sorted([path for path in source_runs_dir.glob("*") if path.is_dir()], key=lambda path: path.stat().st_mtime)
        run_id = existing_runs[-1].name if existing_runs else dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    else:
        run_id = args.run_id.strip() or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = workspace / "data" / "source_exports" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    current_items_path = workspace / "data" / "current_source_items.json"
    enriched_items_path = workspace / "data" / "current_source_items.enriched.json"
    current_stats_path = workspace / "data" / "current_source_items.stats.json"
    legacy_stats_path = workspace / "data" / "wos_minimax_items.stats.json"
    merge_inputs: list[Path] = [run_dir]

    if args.skip_wos:
        export_files = sorted(run_dir.glob("*.txt"))
        log(f"Reusing existing source export files | runDir={run_dir}; files={len(export_files)}")
    else:
        try:
            export_files = fetch_wos_from_project_configs(
                workspace=workspace,
                start_date=start_date,
                end_date=end_date,
                download_dir=run_dir,
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
                output_dir=run_dir,
                log=log,
            )

    if not export_files and not args.no_crossref_fallback:
        log("WARNING: no WoS export files available; switching to Crossref fallback.")
        export_files = fetch_crossref_fallback_exports(
            workspace=workspace,
            start_date=start_date,
            end_date=end_date,
            output_dir=run_dir,
            log=log,
        )

    if not export_files:
        raise RuntimeError("No WoS export txt files found.")

    items, stats, _ = merge_wos_exports(merge_inputs)
    current_items_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    final_stats = {
        **stats,
        "runId": run_id,
        "runDir": str(run_dir),
        "exportFiles": [str(path) for path in export_files],
        "outputJson": str(current_items_path),
    }
    stats_text = json.dumps(final_stats, ensure_ascii=False, indent=2)
    current_stats_path.write_text(stats_text, encoding="utf-8")
    legacy_stats_path.write_text(stats_text, encoding="utf-8")
    log(f"Current run merge complete | runId={run_id}; files={len(export_files)}; items={len(items)}; output={current_items_path}")

    enrich_abstracts(
        input_path=current_items_path,
        output_path=enriched_items_path,
        workspace=workspace,
        log=log,
    )

    # Keep the historical filename as a compatibility pointer to the latest
    # current-run enriched items; the run itself is still isolated by run_id.
    (workspace / "data" / "wos_minimax_items.json").write_text(
        enriched_items_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    queue = screen_items_file(
        workspace=workspace,
        input_path=enriched_items_path,
        min_push_score=args.min_push_score,
        write_legacy=not bool(args.no_legacy),
        log=log,
    )
    log(f"Done | pushQueueSize={len(queue)} | output={workspace / 'data' / 'paper_push_queue.json'}")


if __name__ == "__main__":
    main()

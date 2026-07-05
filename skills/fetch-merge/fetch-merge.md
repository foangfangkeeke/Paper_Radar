# Fetch & Merge

Search Web of Science via CDP browser automation, export with abstracts, and merge/deduplicate.

## Scripts

- **`wos_cdp_workflow.py`** — CDP-based WoS search and batch Plain Text export (with abstracts). Supports `--start-date`, `--end-date`, `--export-dir`. Keywords and journals from `paper-watch.config.json`. Exports in 1000-record batches from a single search.
- **`merge_exports.py`** — Parse WoS txt files, deduplicate by DOI/WoS ID/title, output `data/paper_items.json`.

## Config Files

- **`paper-watch.config.json`** — 4 research directions with WoS queries and target journals

## Usage

```bash
# Full search for all keyword groups
python skills/fetch-merge/wos_cdp_workflow.py

# With date range
python skills/fetch-merge/wos_cdp_workflow.py --start-date 2026-04-01 --end-date 2026-05-26

# Test mode (single keyword group)
python skills/fetch-merge/wos_cdp_workflow.py --test

# Launch Chrome with debug port (manual)
python skills/fetch-merge/wos_cdp_workflow.py --launch

# Merge exported txt files
python skills/fetch-merge/merge_exports.py --input data/source_exports/runs/<run_id>
```

## Cloudflare / Login

First access to WoS requires Cloudflare verification + CARSI institutional login. The persistent Chrome profile (`data/chrome_profile/`) retains cookies after a single login. If searches redirect to the SSO login page, cookies have expired — re-login manually in the browser.

## Output

- `data/source_exports/runs/<run_id>/` — raw WoS txt files per run
- `data/paper_items.json` — deduplicated paper list. Each item must keep a stable `Key` for de-duplication and queue tracking. Raw WoS fields may include `TI`, `SO`, and `AB`; downstream screening normalizes these to `Title`, `Journal`, and `Abstract`.

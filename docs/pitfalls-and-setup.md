# Pitfalls & First-Time Setup

This file records only setup steps and pitfalls that still affect running or maintaining Paper Radar. Historical dead ends are kept only when they explain a rule that should not be changed casually.

## Critical Pitfalls

### Chrome Debug Port Requires An Isolated Profile

Launching Chrome with `--remote-debugging-port` can silently fail when an existing Chrome process owns the default profile. The new process may only pass the URL to the existing browser and ignore the debug-port flag.

Use an explicit `--user-data-dir`:

- WoS search: persistent profile under `data/chrome_profile/`, so login cookies survive.
- PDF download: temporary profile under `%TEMP%/chrome_pdf_download/`.

### WoS China URL Behavior Differs

The China-region WoS site uses `https://webofscience.clarivate.cn/wos/alldb/basic-search`. Do not decide search success by checking whether the result URL contains `summary`; that pattern is not reliable on this site. The useful check is whether the page has moved away from `basic-search` and has not been redirected to login.

### Cloudflare Requires Manual Action

WoS and ScienceDirect can trigger Cloudflare verification or CAPTCHA. The scripts may detect and print warnings, but the user must complete the browser challenge manually. Do not hide this behind repeated silent retries.

### ScienceDirect PDF Download Uses The Live Browser Session

ScienceDirect PDF downloads rely on a live CDP Chrome session. The downloader
opens the article page, extracts the `pdfDownload` metadata, opens the `pdfft`
URL to obtain the signed `pdf.sciencedirectassets.com` URL, then downloads that
signed URL with the current browser cookies. Do not URL-decode the signed URL;
the encoded query string is part of the signature.

Known failed approaches:

| Approach | Result |
|---|---|
| Direct `urllib` or `requests` without browser cookies | 403 / Cloudflare cookie mismatch |
| `Network.getResponseBody` | Body not reliably retrievable |
| Injected JS `fetch()` | CORS / cross-origin restrictions |
| URL-decoding the signed CDN URL before download | AWS signature invalid / 403 |
| Signed CDN URL + CDP browser cookies | Current working approach |

### WoS Search Should Use Browser Automation

Direct WoS API calls were blocked or parsed differently for combined field/date queries. The current approach is CDP browser automation: fill search form, click search, export Plain Text.

### Zotero Import Uses debug-bridge

Zotero import is done through the debug-bridge plugin, not by direct SQLite writes. Zotero must be running during import.

Direct SQLite writes are deprecated because they caused database locks, sync/version conflicts, and certificate verification popups.

## First-Time Setup

### Dependencies

| Dependency | Purpose |
|---|---|
| Google Chrome | CDP browser automation |
| Python 3.10+ | Run all scripts |
| `websocket-client` | CDP WebSocket communication |
| `pdftotext` / poppler-utils | Extract PDF text for detailed screening |
| Zotero 7 | Reference management |
| Zotero debug-bridge plugin | Import via Zotero internal JS API |
| Nutstore WebDAV | Zotero attachment sync, if used |

Install Python dependency:

```bash
pip install websocket-client
```

### Config Files

| File | Purpose |
|---|---|
| `skills/fetch-merge/paper-watch.config.json` | WoS keyword groups and target journals |

| `skills/download/download.config.json` | PDF staging download directory |
| `skills/zotero/zotero.config.json` | Zotero collection, storage path, debug-bridge token |

### Directory Initialization

```bash
mkdir -p data/pdfs data/source_exports/runs reports
```

### debug-bridge Setup

1. Install the debug-bridge plugin in Zotero.
2. Restart Zotero.
3. In Zotero, run this JavaScript from Tools -> Developer -> Run JavaScript:

   ```js
   Zotero.Prefs.set("extensions.zotero.debug-bridge.token", "paper_radar", true)
   ```

4. Keep Zotero running before calling `skills/zotero/zotero_import.py`.

## First Run Checklist

### WoS Search

```bash
python skills/fetch-merge/wos_cdp_workflow.py --test
```

Complete Cloudflare verification and institutional login in the opened Chrome profile if prompted. Later runs reuse `data/chrome_profile/`.

### PDF Download

```bash
python skills/download/paper_downloader.py --doi "10.xxx/yyy" --title "Paper Title" --journal "European Journal of Operational Research"
```

If the browser shows a Cloudflare challenge, complete it manually.

### Zotero Import

```bash
python skills/zotero/zotero_import.py --doi "10.xxx/yyy" --title "Paper Title" --pdf "data/pdfs/paper.pdf" --tags-json '{"Object":"..."}' --comment "Research problem: ..."
```

Zotero must be running, and the debug-bridge token must match `skills/zotero/zotero.config.json`.

## Module Notes

### Fetch-Merge

- WoS exports are saved under `data/source_exports/runs/<run_id>/`.
- `wos_cdp_workflow.py` automatically splits exports larger than 1000 records into batches.
- `merge_exports.py` deduplicates by DOI, WoS ID, and title.

### PDF Download

- `data/pdfs/` is a staging directory.
- ScienceDirect support depends on a live CDP Chrome session and signed CDN
  download with browser cookies.
- Cloudflare warnings should be surfaced to the user immediately.

### Zotero Import

- Tags are stored as single `Key: Value` entries.
- Keywords are stored as one tag: `Keywords: kw1; kw2; kw3`.
- Scoring fields such as `DirectionRelevance`, `MethodRelevance`, `Novelty`, `Transferability`, `Total`, and `Score` are not written as Zotero tags.
- Comment line breaks are converted into Zotero note HTML paragraphs.
- WoS all-caps journal names are normalized before import.
- After successful import, the source PDF in `data/pdfs/` is deleted because Zotero has copied it into storage.

## Development Rules

- Do not rewrite working CDP download logic only for elegance or generality.
- Developer Agent owns code, docs, config templates, tests, and workflow entry points.
- Paper Push Agent owns screening, ranking, push selection, queue outputs, download calls, Zotero import calls, and tags/comment generation.
- Git staging and commits are user decisions.

See also:

- `agents/developer-agent.md`
- `agents/paper-push-agent.md`
- `docs/test-plan.md`
- `skills/fetch-merge/fetch-merge.md`
- `skills/screen/screen.md`
- `skills/download/download.md`
- `skills/zotero/zotero.md`

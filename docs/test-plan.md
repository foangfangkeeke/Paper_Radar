# Test Plan

Module-by-module validation of all 6 pipeline stages. All test artifacts go to an isolated directory — no pollution of existing data.

Test root: `data/test_fetch_merge/`

---

## Step 1: fetch-merge — WoS Search + Merge Dedup

**Method**: Run two searches with different date ranges on the same keyword group (range B covers range A), merge incrementally, and verify idempotency.

```bash
TEST_DIR=data/test_fetch_merge
mkdir -p $TEST_DIR/exports

# 1a. Small range search (first keyword group only, 2026)
python skills/fetch-merge/wos_cdp_workflow.py --test \
  --start-date 2026-01-01 --end-date 2026-05-26 \
  --export-dir $TEST_DIR/exports/run_2026

# 1b. First merge (run_2026 only)
python skills/fetch-merge/merge_exports.py \
  --input $TEST_DIR/exports/run_2026 \
  --out-json $TEST_DIR/paper_items.json \
  --archive-scope none

# 1c. Large range search (covers 1a, 2025-2026)
python skills/fetch-merge/wos_cdp_workflow.py --test \
  --start-date 2025-01-01 --end-date 2026-05-26 \
  --export-dir $TEST_DIR/exports/run_2025_2026

# 1d. Second merge (both runs, append to existing paper_items.json)
python skills/fetch-merge/merge_exports.py \
  --input $TEST_DIR/exports/run_2026 \
  --input $TEST_DIR/exports/run_2025_2026 \
  --out-json $TEST_DIR/paper_items.json \
  --archive-scope none

# 1e. Third merge (idempotency check: same inputs merged again)
python skills/fetch-merge/merge_exports.py \
  --input $TEST_DIR/exports/run_2026 \
  --input $TEST_DIR/exports/run_2025_2026 \
  --out-json $TEST_DIR/paper_items.json \
  --archive-scope none
```

**Verification**:
- Both searches successfully export .txt files
- First merge: `appended_records` = 33, `output_records` = 33
- Second merge: `appended_records` = 104, `duplicates_skipped` > 0, `output_records` = 137
- Third merge: `appended_records` = 0, `duplicates_skipped` = 170 (idempotent)

**Cleanup**: `rm -rf $TEST_DIR`

---

## Step 2: Preliminary Screen — Candidate Lookup + Paper Push Agent Scoring

**Method**: Screen a first batch, verify queue writing; then incrementally screen another batch, verify append (not overwrite).

```bash
TEST_DIR=data/test_fetch_merge

# 2a. First batch: find candidates (base_queue doesn't exist → all are candidates)
python skills/screen/find_candidates.py --count \
  --items $TEST_DIR/paper_items.json \
  --base-queue $TEST_DIR/paper_base_queue.json

python skills/screen/find_candidates.py --limit 5 --json \
  --items $TEST_DIR/paper_items.json \
  --base-queue $TEST_DIR/paper_base_queue.json

# Paper Push Agent screens the above 5 papers → writes to paper_base_queue.json + paper_push_queue.json

# 2b. Incremental batch: find candidates again (already-screened should be excluded)
python skills/screen/find_candidates.py --count \
  --items $TEST_DIR/paper_items.json \
  --base-queue $TEST_DIR/paper_base_queue.json

# Paper Push Agent screens 5 more papers → appends to existing queues
```

**Verification**:
- 2a first candidate count = 137 (all unscreened)
- 2a after: base_queue has 5 entries, push_queue has entries with Score >= 8
- 2b candidate count = 132 (137 - 5 already screened)
- 2b after: base_queue has 10 entries (appended, not overwritten)

## Step 3: push — List Unpushed + Paper Push Agent Selection

**Method**: List unpushed papers in push_queue. Paper Push Agent selects one using dynamic scoring rules, marks PushedAt.

```bash
TEST_DIR=data/test_fetch_merge

# 3a. List unpushed papers
python skills/screen/rank_push_queue.py --unpushed --json --limit 10 --offset 0 \
  --push-queue $TEST_DIR/paper_push_queue.json

# 3b. Paper Push Agent selects one → marks PushedAt → writes back to paper_push_queue.json
```

**Verification**:
- 3a output matches papers in push_queue where PushedAt is empty
- 3b after: `--unpushed` output has one fewer paper

## Step 4: download — PDF Download

**Method**: Download the PDF for the paper pushed in Step 3 via paper_downloader.py.

```bash
TEST_DIR=data/test_fetch_merge

# 4a. Download PDF (ScienceDirect via CDP)
python skills/download/paper_downloader.py \
  --doi "10.1016/j.tre.2026.104709" \
  --title "Integrated and shared charging optimization of electric buses and shared micromobility incorporating solar photovoltaic" \
  --journal "Transportation Research Part E: Logistics and Transportation Review" \
  --download-dir $TEST_DIR/pdfs
```

**Verification**:
- PDF file written to `$TEST_DIR/pdfs/`
- If Cloudflare CAPTCHA appears, manually verify in browser to continue

## Step 5: Detailed Screen — Full-Text Reading

**Method**: Paper Push Agent extracts full text via pdftotext, reads thoroughly per screen.md rules, and produces Tags + Comment.

```bash
# 5a. Extract PDF text
pdftotext -layout "$TEST_DIR/pdfs/<pdf_file>" -

# 5b. Paper Push Agent reads full text → writes detailed_screen.json
```

**Output file**: `$TEST_DIR/detailed_screen.json`

```json
{
  "comment": "<four-part comment: Research problem / Key assumptions / Method / Key findings>",
  "tags": {
    "Object": "...",
    "Application domain": "...",
    "Method": "...",
    ...
  }
}
```

**Verification**:
- Comment covers all four parts: Research problem, Key assumptions, Method, Key findings
- Tags cover 10+ dimensions

## Step 6: zotero — Import

**Method**: Call zotero_import.py to import paper + PDF + tags + comment into Zotero.

```bash
TEST_DIR=data/test_fetch_merge

# 6a. Import with custom storage path and test collection
python skills/zotero/zotero_import.py \
  --doi "10.1016/j.tre.2026.104709" \
  --title "Integrated and shared charging optimization of electric buses and shared micromobility incorporating solar photovoltaic" \
  --journal "Transportation Research Part E: Logistics and Transportation Review" \
  --pdf $TEST_DIR/pdfs/<pdf_file> \
  --tags-json '<detailed_screen tags>' \
  --comment '<detailed_screen comment>' \
  --collection "Test" \
  --storage "C:/Users/foangfangkeeke/Zotero/storage"
```

**Verification**:
- New entry (journalArticle) appears in Zotero with PDF attachment
- Tags written per detailed_screen
- Comment attached as Zotero note
- Entry assigned to specified collection ("Test")
- Source PDF deleted after successful import

**Actual results**:
- Item key: `2A4Z95K9`, storage key: `IPBDDFRU`
- PDF attachment, tags, comment, collection all correctly written
- 1/1 import succeeded

## Step 7: Daily keep-alive health check

**Method**: Run the scheduled workflow immediately. It checks EBSCO and
ScienceDirect with real PDF downloads, closes the publisher browser, then
restarts the shared `data/chrome_profile` on the WoS CDP port for one search
and Plain Text export.

```bash
python skills/download/keep_alive.py --no-delay
```

**Verification**:
- EBSCO and ScienceDirect health-check PDFs download successfully
- Health-check PDFs are saved under `data/healthchecks/pdfs/`, separate from
  normal downloads in `data/pdfs/`
- WoS exports one non-empty TXT containing abstracts
- The temporary WoS TXT and its `data/healthchecks/wos_exports/` run directory
  are deleted immediately
- No WoS export is merged and no paper queue is modified
- Both publisher and WoS Chrome instances are closed
- EBSCO, ScienceDirect, and WoS are all attempted even when an earlier stage fails
- Each failed stage logs its full exception without hiding the final failure
- The command exits with status 0 only when every check succeeds

**Actual results (2026-07-01)**:
- Forced EBSCO-stage exception was logged with traceback; the following stage ran
- EBSCO PDF: 3,785 KB, successful
- ScienceDirect PDF: 7,712 KB, successful after the Cloudflare wait flow
- Network capture confirmed the final PDF host is `pdf.sciencedirectassets.com`
  after redirects through `doi.org`, `linkinghub.elsevier.com`, and
  `www.sciencedirect.com`
- WoS export: 11 records, 25,743 bytes, then deleted with its run directory
- Publisher Chrome (9224) and WoS Chrome (9225) both closed
- Full command exited with status 0

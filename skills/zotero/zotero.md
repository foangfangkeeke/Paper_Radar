# Zotero

Import papers with PDF attachments into Zotero via debug-bridge plugin.

## Prerequisites

- **debug-bridge plugin** installed in Zotero (from Better BibTeX project)
- **Token configured**: `Zotero.Prefs.set("extensions.zotero.debug-bridge.token", "paper_radar", true)` in Zotero Run JavaScript
- **Zotero must be RUNNING** during import (the opposite of the old SQLite approach)

## Scripts

- **`zotero_import.py`** — Import via debug-bridge (parent item + PDF attachment + tags + note + collection)

## Config

### zotero.config.json

```json
{
  "collection": "每日阅读",
  "zoteroStorage": "C:/Users/.../Zotero/storage",
  "debugBridgeToken": "paper_radar"
}
```

## Import Checklist

Before calling `zotero_import.py`, verify:

1. **Tags** (10-15, only include applicable fields)
   - Common: Object, Application domain, Method, Decision level, Objective, Evaluation setting, Transferable idea, Keywords
   - Direction-specific: see `skills/screen/screen.md` §4
   - Format: `field: value`, never `field: none`
2. **Comment** (4-part, 500-1000 chars)
   - Research problem / Key assumptions / Method / Key findings

## Usage

```bash
# Single paper (basic, tags from push_queue)
python skills/zotero/zotero_import.py --doi "10.xxx/yyy" --title "Paper Title" --pdf "data/pdfs/paper.pdf"

# Single paper with detailed screen results (recommended)
python skills/zotero/zotero_import.py \
  --doi "10.xxx/yyy" \
  --title "Paper Title" \
  --pdf "data/pdfs/paper.pdf" \
  --tags-json '{"Object":"BRT corridor","Method":"two-stage SMIP",...}' \
  --comment "Research problem: ..."

# Custom collection
python skills/zotero/zotero_import.py --doi "..." --title "..." --pdf "..." --collection "my_collection"

# Override journal / storage
python skills/zotero/zotero_import.py --doi "..." --title "..." --pdf "..." --journal "Transportation Research Part C"
python skills/zotero/zotero_import.py --doi "..." --title "..." --pdf "..." --storage "D:/Zotero/storage"
```

## Mechanism

Uses the debug-bridge plugin's HTTP endpoint (`localhost:23119/debug-bridge/execute`) to run JavaScript inside Zotero's process. Zotero handles its own DB writes, avoiding the cert verification popup and libraryVersion issues of the old direct-SQLite approach.

Hybrid flow per paper:
1. **Zotero DOI lookup** — uses Zotero's DOI Content Negotiation translator to fetch metadata (title, authors, journal, year, etc.)
2. **JS creates** parent item + PDF attachment placeholder → returns itemID, attKey
3. **Python copies** PDF file to `Zotero/storage/{attKey}/`
4. **JS finalizes** attachment path, tags, child note, collection assignment

Metadata extraction priority:
1. **Zotero DOI translator** (primary) — same as "Add Item by Identifier" in Zotero UI
2. **pdftotext** (fallback) — only used when DOI is unavailable

What gets written:
- **Parent item** (journalArticle): title, abstract, journal, DOI, URL, authors (from Zotero DOI lookup), year, extra
- **Attachment** (PDF): stored file with auto-generated index
- **Tags**: from `--tags-json` (detailed screen) or paper Tags dict. Formatted as `field: value`
- **Child note**: from `--comment` (detailed screen) or paper Comment field. HTML note under parent item
- **Extra**: direction match + score breakdown
- **Collection**: assigned to configured collection (default: `每日阅读`)

## Output

- `Zotero/storage/{key}/` — Zotero storage with PDF + auto-generated aux files
- Zotero DB updated internally by Zotero (no external writes)
- Original PDF deleted after successful import

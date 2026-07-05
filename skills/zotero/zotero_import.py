#!/usr/bin/env python3
"""Import papers with PDF attachments into Zotero via debug-bridge.

Zotero must be RUNNING with the debug-bridge plugin installed.
Uses a hybrid approach: JS creates items -> Python copies PDF -> JS finalizes.

Usage:
  from skills.zotero.zotero_import import import_papers
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills"))
CONFIG_PATH = ROOT / "skills" / "zotero" / "zotero.config.json"

from cdp_client import extract_doi

DEBUG_BRIDGE_URL = "http://127.0.0.1:23119/debug-bridge/execute"
HTTP_TIMEOUT = 30


def _load_zotero_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}

_SCORING_FIELDS = {
    "directionrelevance", "methodrelevance", "novelty", "transferability",
    "total", "score",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _escape_js(s: str) -> str:
    """Escape a Python string for embedding in a JS single-quoted string literal."""
    s = s.replace('\\', '\\\\')
    s = s.replace("'", "\\'")
    s = s.replace('\r\n', '\\n')
    s = s.replace('\n', '\\n')
    s = s.replace('\r', '\\n')
    return s


def _debug_bridge_call(js: str) -> dict:
    """Call debug-bridge and return parsed JSON response."""
    cfg = _load_zotero_config()
    token = cfg.get("debugBridgeToken", "paper_radar")
    req = urllib.request.Request(
        DEBUG_BRIDGE_URL,
        data=js.encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "text/plain",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        raise RuntimeError(f"debug-bridge connection failed (is Zotero running?): {e}") from e
    if raw.startswith("debug-bridge failed:"):
        raise RuntimeError(raw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# PDF metadata extraction
# ---------------------------------------------------------------------------

# Translator ID for DOI Content Negotiation in Zotero
_DOI_TRANSLATOR_ID = "b28d0d42-8549-4c6d-83fc-8382874a5cb9"


def _fetch_metadata_from_zotero(doi: str) -> dict | None:
    """Fetch metadata via Zotero's DOI Content Negotiation translator.

    This is the same mechanism Zotero uses for "Add Item by Identifier".
    Returns a dict with title, authors, journal, year, etc., or None on failure.
    """
    if not doi:
        return None
    js = f"""try {{
    const translate = new Zotero.Translate.Search();
    translate.setSearch({{DOI: '{_escape_js(doi)}'}});
    translate.setTranslator('{_DOI_TRANSLATOR_ID}');
    const items = await translate.translate();
    if (items && items.length > 0) {{
        return JSON.stringify(items[0]);
    }}
    return JSON.stringify(null);
}} catch(e) {{
    return JSON.stringify({{error: e.message}});
}}"""
    try:
        result = _debug_bridge_call(js)
    except Exception as e:
        print(f"  [zotero-doi] {e}")
        return None

    # Result may be a JSON string or already parsed dict
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            print(f"  [zotero-doi] Failed to parse response")
            return None

    if isinstance(result, dict) and result.get("error"):
        print(f"  [zotero-doi] {result['error']}")
        return None
    if not result:
        return None

    # Parse the translator result
    item = result if isinstance(result, dict) else {}
    if not item.get("title"):
        return None

    # Parse year from date field (formats: "2003", "04/2003", "2003-04-21")
    year = None
    date_str = item.get("date", "")
    m = re.search(r"\b((?:19|20)\d{2})\b", date_str)
    if m:
        year = m.group(1)

    # Parse authors from creators array
    authors = []
    for c in item.get("creators", []):
        if c.get("creatorType") == "author":
            authors.append((c.get("firstName", ""), c.get("lastName", "")))

    print(f"  [zotero-doi] Fetched from DOI: {item.get('title', '')[:60]}...")
    return {
        "title": item.get("title"),
        "authors": authors,
        "year": year,
        "journal": item.get("publicationTitle"),
        "volume": item.get("volume"),
        "issue": item.get("issue"),
        "pages": item.get("pages"),
        "issn": item.get("ISSN"),
        "doi": item.get("DOI"),
        "abstract": item.get("abstractNote"),
    }


def _extract_pdf_metadata(pdf_path: Path) -> tuple[list, str | None]:
    """Extract authors and year from PDF first page via pdftotext (fallback)."""
    authors: list = []
    year: str | None = None
    try:
        result = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "1", "-layout", str(pdf_path), "-"],
            capture_output=True, timeout=15)
        if result.returncode != 0:
            return authors, year
        text = result.stdout.decode("utf-8", errors="replace")
    except FileNotFoundError:
        print("  [pdftotext] pdftotext not found in PATH")
        return authors, year
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"  [pdftotext] {e}")
        return authors, year

    lines = [l.strip() for l in text.split("\n") if l.strip()]

    for line in lines[:20]:
        m = re.search(r"\b(20\d{2})\b", line)
        if m:
            year = m.group(1)
            break

    skip_words = {"abstract", "university", "department", "http", "doi", "copyright",
                  "published", "received", "accepted", "correspondence", "email",
                  "introduction", "supplement", "figure", "table", "vol.", "issue",
                  "pages", "pp.", "all rights", "elsevier", "springer", "ieee",
                  "contents lists", "journal homepage"}
    for line in lines[:30]:
        if len(line) > 250:
            continue
        if "," not in line:
            continue
        low = line.lower()
        if any(w in low for w in skip_words):
            continue
        for part in line.split(","):
            part = re.sub(r"\s+", " ", part).strip(",. *†‡§¶0123456789 ")
            if not part or len(part) < 2:
                continue
            words = [w for w in part.split() if not (len(w) == 1 and w.islower())]
            if not words:
                continue
            if len(words) == 1:
                authors.append(("", words[0]))
            elif len(words) >= 2:
                authors.append((" ".join(words[:-1]), words[-1]))
        break
    return authors, year


# ---------------------------------------------------------------------------
# Metadata builders
# ---------------------------------------------------------------------------

def _build_extra(paper: dict) -> str:
    doi = extract_doi(paper.get("Key", ""))
    parts = []
    if doi:
        parts.append(f"DOI: {doi}")
    sb = paper.get("ScoreBreakdown") or {}
    parts.append(f"PaperRadar Score: {paper.get('Score', '?')} "
                 f"(DR:{sb.get('DirectionRelevance','?')} MR:{sb.get('MethodRelevance','?')} "
                 f"NV:{sb.get('Novelty','?')} TR:{sb.get('Transferability','?')})")
    for d in paper.get("MatchedDirections") or []:
        parts.append(f"Direction: {d}")
    return "\n".join(parts)


def _normalize_journal(name: str) -> str:
    """Normalize journal name: Title Case, colon for TR Part series, etc."""
    if not name or not name.strip():
        return name
    name = name.strip()
    name = re.sub(
        r'(Transportation Research Part [A-F])-(Emerging|Methodological|Policy|Transport|Logistics|Traffic)',
        r'\1: \2', name, flags=re.IGNORECASE,
    )
    if name == name.upper():
        name = name.title()
    for word in ("And", "Of", "For", "In", "On", "The", "A", "An", "To", "&"):
        name = re.sub(rf'\b{word}\b', word.lower(), name)
    name = name[0].upper() + name[1:] if name else name
    name = re.sub(r'\boperations research\b', 'Operations Research', name)
    name = re.sub(r'\bmanagement science\b', 'Management Science', name)
    return name


def _build_comment(paper: dict) -> str:
    return paper.get("Comment", "")


# ---------------------------------------------------------------------------
# Debug-bridge JS builders
# ---------------------------------------------------------------------------

def _js_create_item(paper: dict, authors: list, year: str | None) -> str:
    """Build JS to create parent item + attachment. Returns {itemID, attID, attKey, libID}."""
    title = _escape_js(paper.get("Title", ""))
    abstract = _escape_js(paper.get("Abstract", ""))
    journal = _escape_js(_normalize_journal(paper.get("Journal", "")))
    doi = _escape_js(extract_doi(paper.get("Key", "")) or "")
    url = _escape_js(f"https://doi.org/{doi}" if doi else "")
    access_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    extra = _escape_js(_build_extra(paper))

    year_line = f"item.setField('date', '{_escape_js(year)}');" if year else ""

    creators_js = "[]"
    if authors:
        items = []
        for first, last in authors:
            items.append(
                f"{{firstName:'{_escape_js(first)}',lastName:'{_escape_js(last)}',creatorType:'author'}}"
            )
        creators_js = "[" + ",".join(items) + "]"

    return f"""const libID = Zotero.Libraries.userLibraryID;
const item = new Zotero.Item('journalArticle');
item.libraryID = libID;
item.setField('title', '{title}');
item.setField('abstractNote', '{abstract}');
item.setField('publicationTitle', '{journal}');
item.setField('DOI', '{doi}');
item.setField('url', '{url}');
item.setField('accessDate', '{access_date}');
item.setField('libraryCatalog', 'Web of Science');
item.setField('extra', '{extra}');
{year_line}
item.setCreators({creators_js});
await item.saveTx();
const att = new Zotero.Item('attachment');
att.libraryID = libID;
att.parentID = item.id;
att.attachmentLinkMode = 0;
att.attachmentContentType = 'application/pdf';
att.setField('title', 'PDF');
await att.saveTx();
return {{itemID: item.id, itemKey: item.key, attID: att.id, attKey: att.key, libID: libID}};"""


def _js_finalize(paper: dict, item_id: int, att_id: int, collection_name: str) -> str:
    """Build JS to finalize: update attachment path, add tags, note, collection."""
    coll_name = _escape_js(collection_name)

    tags = paper.get("Tags") or {}
    tag_lines = []
    for key, val in tags.items():
        if key.lower() in _SCORING_FIELDS:
            continue
        if key == "Keywords":
            if isinstance(val, list):
                merged = "; ".join(str(kw) for kw in val if kw)
            elif isinstance(val, str) and val.strip():
                merged = val.strip()
            else:
                continue
            tag_lines.append(f"item.addTag('{_escape_js(f'Keywords: {merged}')}'); await item.saveTx();")
        elif isinstance(val, str) and val.strip().lower() not in ("none", ""):
            tag_lines.append(f"item.addTag('{_escape_js(f'{key}: {val}')}'); await item.saveTx();")

    tags_js = "\n  ".join(tag_lines)
    if tags_js:
        tags_js = "  " + tags_js + "\n"

    comment = _build_comment(paper)
    note_js = ""
    if comment:
        paragraphs = "".join(
            f"<p>{p}</p>" for p in comment.replace("\r\n", "\n").split("\n") if p.strip()
        )
        note_html = (
            '<div class="zotero-note znv1">'
            f'<div data-schema-version="9">{paragraphs}</div>'
            '</div>'
        )
        note_html_esc = _escape_js(note_html)
        note_js = f"""
  const note = new Zotero.Item('note');
  note.libraryID = libID;
  note.parentID = {item_id};
  note.setNote('{note_html_esc}');
  await note.saveTx();"""

    return f"""const libID = Zotero.Libraries.userLibraryID;
const att = Zotero.Items.get({att_id});
att.attachmentPath = 'storage:paper.pdf';
await att.saveTx();
const item = Zotero.Items.get({item_id});
{tags_js}{note_js}
function findColl(list, name) {{
  for (const c of list) {{
    if (c.name === name && c.parentID) return c;
    const found = findColl(c.getChildCollections(), name);
    if (found) return found;
  }}
  return null;
}}
let coll = findColl(Zotero.Collections.getByLibrary(libID), '{coll_name}');
if (!coll) {{
  coll = Zotero.Collections.getByLibrary(libID).find(c => c.name === '{coll_name}');
}}
if (!coll) {{
  coll = new Zotero.Collection();
  coll.name = '{coll_name}';
  coll.libraryID = libID;
  await coll.saveTx();
}}
item.addToCollection(coll.id);
await item.saveTx();
return {{ok: true}};"""


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_papers(papers_with_pdfs: list, collection_name: str = "每日阅读",
                  storage_path: Path | None = None) -> int:
    """Import papers via debug-bridge (Zotero must be running).
    Returns number of papers successfully saved."""
    if not papers_with_pdfs:
        return 0

    if storage_path:
        zotero_storage = Path(storage_path)
    else:
        cfg = _load_zotero_config()
        zotero_storage = Path(cfg.get("zoteroStorage", Path.home() / "Zotero" / "storage"))

    count = 0
    for paper, pdf_path in papers_with_pdfs:
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            print(f"  PDF missing: {pdf_path}")
            continue

        item_id = att_id = att_key = None
        try:
            # Try Zotero DOI translator first (authoritative source)
            doi = extract_doi(paper.get("Key", "")) or paper.get("DOI", "")
            zotero_meta = _fetch_metadata_from_zotero(doi) if doi else None

            if zotero_meta:
                # Use Zotero metadata: authors, year, and optionally enrich journal
                pdf_authors = zotero_meta.get("authors", [])
                pdf_year = zotero_meta.get("year")
                # Enrich paper dict with Zotero metadata if missing
                if not paper.get("Journal") and zotero_meta.get("journal"):
                    paper["Journal"] = zotero_meta["journal"]
                if not paper.get("Title") and zotero_meta.get("title"):
                    paper["Title"] = zotero_meta["title"]
                if not paper.get("Abstract") and zotero_meta.get("abstract"):
                    paper["Abstract"] = zotero_meta["abstract"]
            else:
                # Fallback: extract from PDF via pdftotext
                pdf_authors, pdf_year = _extract_pdf_metadata(pdf_path)

            # Step 1: JS creates parent item + attachment placeholder
            title_short = paper.get("Title", "")[:80]
            print(f"  Creating: {title_short}...")
            result = _debug_bridge_call(_js_create_item(paper, pdf_authors, pdf_year))
            item_id = result["itemID"]
            att_id = result["attID"]
            att_key = result["attKey"]

            # Step 2: Python copies PDF to Zotero storage
            storage_dir = zotero_storage / att_key
            storage_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pdf_path, storage_dir / "paper.pdf")
            print(f"  PDF -> storage/{att_key}/")

            # Step 3: JS finalizes path, tags, note, collection
            _debug_bridge_call(_js_finalize(paper, item_id, att_id, collection_name))

            # Step 4: Delete source PDF
            pdf_path.unlink()
            print(f"  Imported [{result['itemKey']}] -> '{collection_name}'")
            count += 1
        except Exception as e:
            print(f"  Error: {e}")
            if item_id is not None:
                print(f"    Cleanup info: item_id={item_id} att_id={att_id} "
                      f"att_key={att_key} storage_dir={zotero_storage / att_key}")
            continue

    print(f"  Saved {count}/{len(papers_with_pdfs)} papers to Zotero.")
    return count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    p = argparse.ArgumentParser(description="Import papers with PDFs into Zotero")
    p.add_argument("--doi", default="", help="DOI of a single paper")
    p.add_argument("--title", default="", help="Title for single paper import")
    p.add_argument("--pdf", default="", help="Path to PDF for single paper import")
    p.add_argument("--tags-json", default="",
                   help="Tags as JSON string (from detailed screen, overrides queue)")
    p.add_argument("--comment", default="",
                   help="Comment string (from detailed screen, overrides queue)")
    p.add_argument("--journal", default="",
                   help="Journal name (overrides queue)")
    p.add_argument("--collection", default="",
                   help="Zotero collection name (overrides config)")
    p.add_argument("--storage", default="",
                   help="Zotero storage directory (overrides config)")
    args = p.parse_args()

    # Load collection config
    config_path = ROOT / "skills" / "zotero" / "zotero.config.json"
    collection = args.collection
    if not collection and config_path.exists():
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        collection = cfg.get("collection", "")

    if args.doi:
        doi = args.doi.strip()
        title = args.title.strip()
        pdf_path = args.pdf.strip()
        if not title:
            print("Error: --title required with --doi")
            sys.exit(1)
        if not pdf_path:
            print("Error: --pdf required with --doi")
            sys.exit(1)
        paper = {"Key": f"doi:{doi}", "Title": title}
        # Try to enrich from push queue
        queue_path = ROOT / "data" / "paper_push_queue.json"
        if queue_path.exists():
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            for p in queue:
                if extract_doi(p.get("Key", "")) == doi:
                    paper = p
                    break
        # Override with detailed screen results if provided
        if args.tags_json:
            try:
                paper["Tags"] = json.loads(args.tags_json)
            except json.JSONDecodeError as e:
                print(f"Warning: invalid --tags-json: {e}")
        if args.comment:
            paper["Comment"] = args.comment
        if args.journal:
            paper["Journal"] = args.journal
        storage = Path(args.storage) if args.storage else None
        count = import_papers([(paper, pdf_path)], collection, storage_path=storage)
        if count == 0:
            sys.exit(1)
        return

    p.print_help()


if __name__ == "__main__":
    main()

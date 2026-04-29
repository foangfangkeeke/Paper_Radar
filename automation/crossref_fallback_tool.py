#!/usr/bin/env python3
"""Crossref fallback that writes WoS-compatible Plain Text exports.

This is used only when WoS browser export is unavailable.  The output is a
synthetic savedrecs-style txt file with the same fields consumed by the current
merge/parser path: TI, SO, AB, plus DI/PY when Crossref provides them.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


LogFn = Callable[[str], None]
USER_AGENT = "paper-radar-agent/1.0 (mailto:zb2557604@buaa.edu.cn)"

JOURNAL_ISSN_MAP = {
    "Operations Research": ["0030-364X", "1526-5463"],
    "Management Science": ["0025-1909", "1526-5501"],
    "Mathematics of Operations Research": ["0364-765X", "1526-5471"],
    "INFORMS Journal on Optimization": ["2575-1484", "2575-1492"],
    "INFORMS Journal on Computing": ["1091-9856", "1526-5528"],
    "Manufacturing & Service Operations Management": ["1523-4614", "1526-5498"],
    "Mathematical Programming": ["0025-5610", "1436-4646"],
    "Computers & Operations Research": ["0305-0548", "1873-765X"],
    "European Journal of Operational Research": ["0377-2217", "1872-6860"],
    "Transportation Science": ["0041-1655", "1526-5447"],
    "Transportation Research Part A: Policy and Practice": ["0965-8564", "1879-2375"],
    "Transportation Research Part B: Methodological": ["0191-2615", "1879-2367"],
    "Transportation Research Part C: Emerging Technologies": ["0968-090X", "1879-2359"],
    "Transportation Research Part D: Transport and Environment": ["1361-9209", "1879-2340"],
    "Transportation Research Part E: Logistics and Transportation Review": ["1366-5545", "1878-5794"],
    "Transportation Research Part F: Traffic Psychology and Behaviour": ["1369-8478", "1873-5517"],
    "Transport Policy": ["0967-070X", "1879-310X"],
    "Applied Energy": ["0306-2619", "1872-9118"],
    "Energy": ["0360-5442", "1873-6785"],
    "Nature Energy": ["2058-7546"],
}


def clean_text(value: Any) -> str:
    text = "" if value is None else html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_journal_title(value: Any) -> str:
    text = clean_text(value).lower().replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_target_journal(journal: str, targets: list[str]) -> bool:
    normalized = normalize_journal_title(journal)
    return bool(normalized) and any(normalized == normalize_journal_title(target) for target in targets)


def keyword_name(entry: Any, idx: int) -> str:
    if isinstance(entry, str):
        return f"keyword_{idx}"
    if isinstance(entry, dict):
        return clean_text(entry.get("name") or entry.get("key") or f"keyword_{idx}")
    return f"keyword_{idx}"


def keyword_query(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        return str(entry.get("query") or entry.get("text") or entry.get("name") or "").strip()
    return ""


def target_journals(config: dict[str, Any]) -> list[str]:
    journals: list[str] = []
    for entry in config.get("topJournals", []):
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("title") or "")
        else:
            name = ""
        name = clean_text(name)
        if name:
            journals.append(name)
    return journals


def paper_date(item: dict[str, Any], today: dt.date) -> dt.date | None:
    for field in ("published-print", "published-online", "published", "created"):
        parts = item.get(field, {}).get("date-parts")
        if not parts:
            continue
        try:
            values = parts[0]
            year = int(values[0])
            month = int(values[1]) if len(values) > 1 else 1
            day = int(values[2]) if len(values) > 2 else 1
            value = dt.date(year, month, day)
        except Exception:
            continue
        if dt.date(1900, 1, 1) <= value <= today:
            return value
    return None


def paper_key(title: str, doi: str) -> str:
    doi = clean_text(doi).lower()
    if doi:
        return "doi:" + doi
    return title_key(title)


def title_key(title: str) -> str:
    normalized_title = re.sub(r"[^a-z0-9]+", " ", clean_text(title).lower()).strip()
    return "title:" + normalized_title if normalized_title else ""


def doi_key(doi: str) -> str:
    doi = clean_text(doi).lower()
    return "doi:" + doi if doi else ""


def crossref_item_to_record(item: dict[str, Any], source: str, today: dt.date) -> dict[str, Any] | None:
    title_values = item.get("title") or []
    journal_values = item.get("container-title") or []
    title = clean_text(title_values[0] if title_values else "")
    journal = clean_text(journal_values[0] if journal_values else "")
    if not title or not journal:
        return None
    published = paper_date(item, today)
    doi = clean_text(item.get("DOI"))
    return {
        "Key": paper_key(title, doi),
        "DoiKey": doi_key(doi),
        "TitleKey": title_key(title),
        "TI": title,
        "SO": journal,
        "AB": clean_text(item.get("abstract")),
        "DI": doi,
        "PY": str(published.year) if published else "",
        "DateObject": published.isoformat() if published else "",
        "Source": source,
    }


def is_retryable_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in [
            "unexpected_eof",
            "eof occurred",
            "ssl",
            "timeout",
            "timed out",
            "connection",
            "connection reset",
            "connection aborted",
            "max retries",
            "remote end closed",
            "temporarily",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
        ]
    )


def request_crossref(params: dict[str, Any], log: LogFn) -> dict[str, Any] | None:
    if requests is None:
        raise RuntimeError("The requests package is required for Crossref fallback.")
    url = "https://api.crossref.org/works?" + urlencode(params)
    for attempt in range(1, 6):
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=90)
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            return response.json()
        except Exception as exc:
            message = str(exc)
            if attempt >= 5 or not is_retryable_error(message):
                log(f"WARNING: Crossref request failed permanently | {message}")
                return None
            wait_sec = min(30.0, 2.0 * (2 ** (attempt - 1)))
            log(f"WARNING: Crossref request retry | attempt={attempt}; retryInSec={wait_sec:g}; {message}")
            time.sleep(wait_sec)
    return None


def crossref_paged_items(params: dict[str, Any], log: LogFn) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = "*"
    while True:
        page_params = dict(params)
        page_params["rows"] = "200"
        page_params["cursor"] = cursor
        response = request_crossref(page_params, log)
        message = response.get("message") if isinstance(response, dict) else None
        page_items = message.get("items") if isinstance(message, dict) else None
        if not page_items:
            break
        items.extend(page_items)
        next_cursor = str(message.get("next-cursor") or "")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(0.2)
    return items


def merge_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        key = clean_text(record.get("Key"))
        if not key:
            continue
        existing = merged.get(key)
        if existing is None or (not clean_text(existing.get("AB")) and clean_text(record.get("AB"))):
            merged[key] = record
    return list(merged.values())


def format_wos_field(tag: str, value: Any) -> list[str]:
    text = clean_text(value)
    if not text:
        return []
    width = 220
    chunks = [text[i : i + width] for i in range(0, len(text), width)]
    return [f"{tag} {chunks[0]}"] + [f"   {chunk}" for chunk in chunks[1:]]


def write_synthetic_wos_export(records: list[dict[str, Any]], output_path: Path) -> Path:
    lines = ["FN Crossref Fallback", "VR 1.0"]
    for record in records:
        lines.append("PT J")
        for tag in ("TI", "SO", "PY", "DI", "AB"):
            lines.extend(format_wos_field(tag, record.get(tag)))
        lines.append("ER")
    lines.append("EF")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def fetch_crossref_fallback_exports(
    workspace: str | Path,
    start_date: dt.date,
    end_date: dt.date,
    output_dir: str | Path | None = None,
    log: LogFn = print,
) -> list[Path]:
    workspace_path = Path(workspace).resolve()
    config = read_json(workspace_path / "automation" / "paper-watch.config.json", {})
    if not isinstance(config, dict):
        config = {}
    journals = target_journals(config)
    keywords = [(keyword_name(entry, idx), keyword_query(entry)) for idx, entry in enumerate(config.get("keywords", []), start=1)]
    keywords = [(name, query) for name, query in keywords if query]
    if not journals or not keywords:
        raise RuntimeError("Crossref fallback requires topJournals and keywords in paper-watch.config.json.")

    today = dt.date.today()
    records: list[dict[str, Any]] = []
    query_field = str(config.get("crossrefKeywordQueryField") or "query.bibliographic")
    if query_field not in {"query", "query.bibliographic", "query.title"}:
        query_field = "query.bibliographic"

    for journal in journals:
        issns = JOURNAL_ISSN_MAP.get(journal, [])
        if issns:
            journal_filter = ",".join(f"issn:{issn}" for issn in issns)
            filter_value = f"from-pub-date:{start_date.isoformat()},until-pub-date:{end_date.isoformat()},type:journal-article,{journal_filter}"
            local_check = False
            label = "issn:" + "|".join(issns)
        else:
            filter_value = f"from-pub-date:{start_date.isoformat()},until-pub-date:{end_date.isoformat()},type:journal-article"
            local_check = True
            label = "container-title"

        for name, query in keywords:
            params = {"filter": filter_value, "sort": "published", "order": "desc", query_field: query}
            if local_check:
                params["query.container-title"] = journal
            items = crossref_paged_items(params, log)
            kept = 0
            for item in items:
                record = crossref_item_to_record(item, f"crossref:{name};journal:{journal}", today)
                if not record:
                    continue
                if local_check and not is_target_journal(str(record.get("SO", "")), [journal]):
                    continue
                date_object = clean_text(record.get("DateObject"))
                if date_object:
                    pub_date = dt.date.fromisoformat(date_object[:10])
                    if pub_date < start_date or pub_date > end_date:
                        continue
                records.append(record)
                kept += 1
            log(
                f"Crossref fallback search | keyword={name}; journal={journal}; filter={label}; "
                f"fetched={len(items)}; kept={kept}; rawTotal={len(records)}"
            )

    deduped = merge_records(records)
    generated_at = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    export_dir = Path(output_dir) if output_dir is not None else workspace_path / "data" / "source_exports"
    if not export_dir.is_absolute():
        export_dir = workspace_path / export_dir
    export_path = export_dir / f"crossref_fallback_{generated_at}.txt"
    write_synthetic_wos_export(deduped, export_path)

    # Keep the merge-compatible fields explicit; extra fields are saved only for
    # inspection and are ignored by merge_exports.py.
    write_json(
        workspace_path / "data" / "paper-source-candidates.json",
        {
            "generatedAt": dt.datetime.now().astimezone().isoformat(),
            "source": "crossref_fallback",
            "exportPath": str(export_path),
            "fromDate": start_date.isoformat(),
            "untilDate": end_date.isoformat(),
            "rawCount": len(records),
            "dedupedCount": len(deduped),
            "papers": deduped,
            "mergeCompatibleFields": ["key", "TI", "SO", "AB"],
        },
    )
    log(f"Crossref fallback export written | papers={len(deduped)}; path={export_path}")
    return [export_path] if deduped else []

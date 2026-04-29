#!/usr/bin/env python3
"""Fill missing abstracts for current-run paper items.

This runs after source export merge and before MiniMax screening.  It only
touches items from the current run and uses a local DOI cache to avoid repeated
metadata requests across runs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


LogFn = Callable[[str], None]
USER_AGENT = "paper-radar-agent/1.0"


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
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


def doi_from_item(item: dict[str, Any]) -> str:
    doi = clean_text(item.get("DI") or item.get("DOI") or item.get("doi"))
    if doi:
        return doi.lower()
    key = clean_text(item.get("key") or item.get("Key"))
    if key.lower().startswith("doi:"):
        return key[4:].strip().lower()
    return ""


def request_json(url: str, log: LogFn) -> dict[str, Any] | None:
    if requests is None:
        return None
    for attempt in range(1, 4):
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            if response.status_code == 404:
                return None
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
            return response.json()
        except Exception as exc:
            if attempt >= 3:
                log(f"WARNING: abstract enrichment request failed | url={url}; reason={exc}")
                return None
            time.sleep(min(10.0, 2.0 * attempt))
    return None


def abstract_from_semantic_scholar(doi: str, log: LogFn) -> str:
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(doi, safe='')}?fields=abstract"
    data = request_json(url, log)
    if not isinstance(data, dict):
        return ""
    return clean_text(data.get("abstract"))


def abstract_from_openalex(doi: str, log: LogFn) -> str:
    url = f"https://api.openalex.org/works/https://doi.org/{quote(doi, safe='')}"
    data = request_json(url, log)
    if not isinstance(data, dict):
        return ""
    direct = clean_text(data.get("abstract"))
    if direct:
        return direct
    inverted = data.get("abstract_inverted_index")
    if not isinstance(inverted, dict):
        return ""
    positions: dict[int, str] = {}
    for word, indexes in inverted.items():
        if not isinstance(indexes, list):
            continue
        for index in indexes:
            try:
                positions[int(index)] = str(word)
            except Exception:
                continue
    if not positions:
        return ""
    return clean_text(" ".join(positions[index] for index in sorted(positions)))


def enrich_abstracts(
    input_path: str | Path,
    output_path: str | Path,
    workspace: str | Path,
    cache_path: str | Path = "data/abstract_enrichment_cache.json",
    log: LogFn = print,
) -> list[dict[str, Any]]:
    workspace_path = Path(workspace).resolve()
    input_file = Path(input_path)
    if not input_file.is_absolute():
        input_file = workspace_path / input_file
    output_file = Path(output_path)
    if not output_file.is_absolute():
        output_file = workspace_path / output_file
    cache_file = Path(cache_path)
    if not cache_file.is_absolute():
        cache_file = workspace_path / cache_file

    items = read_json(input_file, [])
    if not isinstance(items, list):
        raise RuntimeError(f"Abstract enrichment input must be a list: {input_file}")
    cache = read_json(cache_file, {})
    if not isinstance(cache, dict):
        cache = {}

    missing = 0
    cache_hits = 0
    enriched = 0
    unresolved = 0
    output: list[dict[str, Any]] = []

    for raw in items:
        item = dict(raw) if isinstance(raw, dict) else {}
        if clean_text(item.get("AB")):
            output.append(item)
            continue

        missing += 1
        doi = doi_from_item(item)
        if not doi:
            unresolved += 1
            output.append(item)
            continue

        cached = cache.get(doi)
        abstract = clean_text(cached.get("abstract") if isinstance(cached, dict) else "")
        source = clean_text(cached.get("source") if isinstance(cached, dict) else "")
        if abstract:
            cache_hits += 1
        else:
            abstract = abstract_from_semantic_scholar(doi, log)
            source = "semantic_scholar" if abstract else ""
            if not abstract:
                abstract = abstract_from_openalex(doi, log)
                source = "openalex" if abstract else ""
            cache[doi] = {
                "abstract": abstract,
                "source": source,
                "checkedAt": dt.datetime.now().astimezone().isoformat(),
            }

        if abstract:
            item["AB"] = abstract
            item["AbstractSource"] = source or "cache"
            enriched += 1
        else:
            unresolved += 1
        output.append(item)

    write_json(output_file, output)
    write_json(cache_file, cache)
    log(
        f"Abstract enrichment complete | input={len(items)}; missing={missing}; "
        f"cacheHits={cache_hits}; enriched={enriched}; unresolved={unresolved}; output={output_file}"
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich missing abstracts by DOI.")
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--input", default="data/current_source_items.json")
    parser.add_argument("--output", default="data/current_source_items.enriched.json")
    parser.add_argument("--cache", default="data/abstract_enrichment_cache.json")
    args = parser.parse_args()
    enrich_abstracts(args.input, args.output, args.workspace, args.cache)


if __name__ == "__main__":
    main()

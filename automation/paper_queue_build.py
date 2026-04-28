#!/usr/bin/env python3
"""
Build the paper candidate queue with a small paper-radar agent.

The agent chooses different tools by mode:
1. Full: import Web of Science plain text exports from data/wos_exports
2. Weekly: search Crossref with target journal, date, and Boolean query filters

MiniMax only sees papers that passed the mode-specific hard filters.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from urllib.error import HTTPError, URLError

try:
    import requests
except ImportError:  # pragma: no cover - fallback for minimal Python installs.
    requests = None


USER_AGENT = "paper-radar-agent/1.0 (mailto:zb2557604@buaa.edu.cn)"
DEFAULT_FROM_DATE = dt.date(2023, 1, 1)
SCREEN_SCHEMA_VERSION = 4
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


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.workspace = Path(args.workspace_root).resolve()
        self.run_log_path: Path | None = None
        self.proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
        self.opener = self._build_opener()

    def _build_opener(self):
        if not self.proxy_url:
            return build_opener()
        return build_opener(ProxyHandler({"http": self.proxy_url, "https": self.proxy_url}))

    def log(self, message: str) -> None:
        line = f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        print(line)
        if self.run_log_path:
            self.run_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.run_log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            self.log(f"WARNING: could not parse JSON file {path}: {exc}")
            return default

    def write_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def resolve_workspace_path(self, value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value)
        if path.is_absolute():
            return path
        return self.workspace / path

    def request_json(self, method: str, url: str, body: Any | None = None, headers: dict[str, str] | None = None, timeout: int = 120) -> Any | None:
        request_headers = {"User-Agent": USER_AGENT}
        if headers:
            request_headers.update(headers)
        proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None
        if requests is not None:
            try:
                response = requests.request(method.upper(), url, json=body, headers=request_headers, timeout=timeout, proxies=proxies)
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code} for {url}: {response.text}")
                return response.json()
            except Exception as exc:
                raise RuntimeError(f"Request failed for {url}: {exc}") from exc

        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json; charset=utf-8")
        req = Request(url, data=data, headers=request_headers, method=method.upper())
        try:
            with self.opener.open(req, timeout=timeout) as response:
                text = response.read().decode("utf-8")
                return json.loads(text)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Request failed for {url}: {exc}") from exc

    def crossref_get(self, endpoint: str, params: dict[str, Any]) -> Any | None:
        url = f"https://api.crossref.org/{endpoint}?{urlencode(params)}"
        max_retries = 4
        for attempt in range(1, max_retries + 2):
            try:
                return self.request_json("GET", url, timeout=120)
            except Exception as exc:
                message = str(exc)
                if attempt > max_retries or not is_retryable_crossref_error(message):
                    self.log(f"WARNING: Crossref request failed: {url}")
                    self.log(f"WARNING: {message}")
                    return None
                wait_sec = min(30.0, 2.0 * (2 ** (attempt - 1)))
                self.log(f"WARNING: Crossref transient failure; attempt={attempt}; retryInSec={wait_sec:g}; {message}")
                time.sleep(wait_sec)
        return None

    def crossref_paged_items(self, params: dict[str, Any], max_items: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor = "*"
        unlimited = max_items <= 0
        limit = sys.maxsize if unlimited else max_items
        while len(items) < limit:
            rows = 200 if unlimited else min(200, limit - len(items))
            page_params = dict(params)
            page_params["rows"] = str(rows)
            page_params["cursor"] = cursor
            response = self.crossref_get("works", page_params)
            message = response.get("message") if isinstance(response, dict) else None
            page_items = message.get("items") if isinstance(message, dict) else None
            if not page_items:
                break
            items.extend(page_items)
            next_cursor = str(message.get("next-cursor", ""))
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            time.sleep(0.2)
        return items if unlimited else items[:max_items]


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_abstract_markup(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_text(text)


def normalize_journal_title(value: Any) -> str:
    text = clean_text(value).lower().replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_target_journal(journal: str, targets: list[str]) -> bool:
    journal_norm = normalize_journal_title(journal)
    return bool(journal_norm) and any(journal_norm == normalize_journal_title(target) for target in targets)


def search_query_name(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("name") or entry.get("key") or entry.get("query") or "")
    return str(entry or "")


def search_query_text(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("query") or entry.get("text") or entry.get("name") or "")
    return str(entry or "")


def research_directions(config: dict[str, Any]) -> list[dict[str, str]]:
    return [{"name": search_query_name(item), "query": search_query_text(item)} for item in config.get("keywords", []) if search_query_text(item)]


def date_windows(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    windows: list[tuple[dt.date, dt.date]] = []
    current = start
    while current <= end:
        window_end = min(dt.date(current.year + 1, 12, 31), end)
        windows.append((current, window_end))
        current = window_end + dt.timedelta(days=1)
    return windows


def date_from_parts(parts: Any) -> dt.date | None:
    if not parts:
        return None
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return dt.date(year, month, day)
    except Exception:
        return None


def item_date(item: dict[str, Any], today: dt.date) -> dt.date | None:
    published: list[dt.date] = []
    for field in ("published-print", "published-online"):
        parts = item.get(field, {}).get("date-parts")
        if parts:
            value = date_from_parts(parts[0])
            if value:
                published.append(value)
    if published:
        valid = sorted(value for value in published if dt.date(1900, 1, 1) <= value <= today)
        return valid[0] if valid else None
    parts = item.get("created", {}).get("date-parts")
    if parts:
        created = date_from_parts(parts[0])
        if created and dt.date(1900, 1, 1) <= created <= today:
            return created
    return None


def paper_key(paper: dict[str, Any]) -> str:
    doi = clean_text(paper.get("DOI"))
    if doi:
        return "doi:" + doi.lower()
    title = re.sub(r"[^a-z0-9]+", " ", clean_text(paper.get("Title")).lower()).strip()
    return "title:" + title


def paper_abstract(paper: dict[str, Any]) -> str:
    return clean_text(paper.get("Abstract") or paper.get("Summary"))


def paper_record(source: str, item: dict[str, Any], today: dt.date) -> dict[str, Any]:
    title_values = item.get("title") or []
    journal_values = item.get("container-title") or []
    doi = clean_text(item.get("DOI"))
    url = f"https://doi.org/{doi}" if doi else clean_text(item.get("URL"))
    published = item_date(item, today)
    record = {
        "Key": "",
        "Source": source,
        "Title": clean_text(title_values[0] if title_values else "Untitled"),
        "Journal": clean_text(journal_values[0] if journal_values else "Unknown journal"),
        "Date": published.isoformat() if published else "Unknown",
        "DateObject": published.isoformat() if published else None,
        "DOI": doi,
        "Url": url,
        "Abstract": strip_abstract_markup(item.get("abstract")),
        "CodeUrl": None,
        "GitHubStars": 0,
    }
    record["Key"] = paper_key(record)
    return record


def parse_wos_plain_text(path: Path, today: dt.date) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    raw_records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    current_tag = ""
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        if line == "ER":
            if current:
                raw_records.append(current)
            current = {}
            current_tag = ""
            continue
        if not line or line.startswith("FN ") or line.startswith("VR ") or line == "EF":
            continue
        tag = line[:2].strip()
        if len(line) >= 3 and tag:
            value = line[3:].strip() if len(line) > 3 else ""
            if tag:
                current_tag = tag
                if tag in current and value:
                    current[tag] = clean_text(current[tag] + " " + value)
                else:
                    current[tag] = clean_text(value)
                continue
        if current_tag and line.startswith(" "):
            current[current_tag] = clean_text(current.get(current_tag, "") + " " + line.strip())
    if current:
        raw_records.append(current)

    records: list[dict[str, Any]] = []
    for raw in raw_records:
        title = clean_text(raw.get("TI"))
        journal = clean_text(raw.get("SO") or raw.get("JI") or raw.get("J9"))
        if not title or not journal:
            continue
        year = as_int(raw.get("PY"), 0)
        published = dt.date(year, 1, 1) if 1900 <= year <= today.year else None
        doi = clean_text(raw.get("DI"))
        record = {
            "Key": "",
            "Source": f"wos_export:{path.name}",
            "Title": title,
            "Journal": journal,
            "Date": published.isoformat() if published else "Unknown",
            "DateObject": published.isoformat() if published else None,
            "DOI": doi,
            "Url": f"https://doi.org/{doi}" if doi else "",
            "Abstract": clean_text(raw.get("AB")),
            "CodeUrl": None,
            "GitHubStars": 0,
            "WosAccessionNumber": clean_text(raw.get("UT")),
        }
        record["Key"] = paper_key(record)
        records.append(record)
    return records


def journal_quality_score(journal: str, top_journals: list[str]) -> int:
    normalized = normalize_journal_title(journal)
    for top in top_journals:
        if normalized == normalize_journal_title(top):
            return 5 if normalized in {"nature", "science"} else 4
    return 0


def source_direction(source: str) -> str:
    match = re.search(r"keyword:([^;]+)", source or "")
    return match.group(1) if match else ""


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def normalize_tags(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key, raw in value.items():
        name = clean_text(key)
        if not name:
            continue
        if isinstance(raw, list):
            items = [clean_text(item) for item in raw if clean_text(item)]
            if items:
                normalized[name] = items
        else:
            text = clean_text(raw)
            if text:
                normalized[name] = text
    return normalized


def queue_record(paper: dict[str, Any], screen: dict[str, Any], config: dict[str, Any], today: dt.date) -> dict[str, Any]:
    llm_score = as_int(screen.get("score", screen.get("topicFit", 0)))
    journal = journal_quality_score(str(paper.get("Journal", "")), config.get("topJournals", []))
    total = llm_score + journal
    direction = str(screen.get("primaryDirection") or "")
    if not direction:
        direction = source_direction(str(paper.get("Source", "")))
    raw_directions = screen.get("directions", [])
    if isinstance(raw_directions, list):
        matched_directions = [clean_text(value) for value in raw_directions if clean_text(value)]
    else:
        matched_directions = []
    if not matched_directions and direction:
        matched_directions = [direction]
    tags = normalize_tags(screen.get("tags"))
    comment = clean_text(screen.get("comment") or screen.get("reason") or "")
    return {
        "Key": paper["Key"],
        "Source": paper["Source"],
        "Title": paper["Title"],
        "Journal": paper["Journal"],
        "Date": paper["Date"],
        "DateObject": paper["DateObject"],
        "DOI": paper["DOI"],
        "Url": paper["Url"],
        "Abstract": paper_abstract(paper),
        "CodeUrl": paper.get("CodeUrl"),
        "GitHubStars": paper.get("GitHubStars", 0),
        "MatchedDirections": matched_directions,
        "RecommendationScore": total,
        "ScoreBreakdown": {
            "LlmScore": llm_score,
            "JournalQuality": journal,
            "Total": total,
        },
        "LlmScreen": {
            "Accepted": bool(screen.get("accept")),
            "Score": llm_score,
            "PrimaryDirection": direction,
            "Directions": matched_directions,
            "Tags": tags,
            "Comment": comment,
            "Keywords": tags.get("Keywords", []),
        },
        "ScreenSchemaVersion": SCREEN_SCHEMA_VERSION,
        "QueuedAt": dt.datetime.now().astimezone().isoformat(),
    }


def merge_queue(queue: list[dict[str, Any]], accepted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for paper in queue + accepted:
        key = paper.get("Key")
        if not key:
            continue
        previous = merged.get(key)
        paper_schema = int(paper.get("ScreenSchemaVersion", 0) or 0)
        previous_schema = int(previous.get("ScreenSchemaVersion", 0) or 0) if previous else 0
        if (
            previous is None
            or paper_schema > previous_schema
            or (
                paper_schema == previous_schema
                and int(paper.get("RecommendationScore", 0)) > int(previous.get("RecommendationScore", 0))
            )
        ):
            merged[key] = paper
    return sorted(
        merged.values(),
        key=lambda paper: (int(paper.get("RecommendationScore", 0)), str(paper.get("DateObject") or "")),
        reverse=True,
    )


def merge_papers_by_key(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for paper in existing + incoming:
        key = str(paper.get("Key") or "")
        if key and key not in merged:
            merged[key] = paper
    return list(merged.values())


def build_node_key(journal: str, window_start: dt.date, window_end: dt.date) -> str:
    return f"{normalize_journal_title(journal)}|{window_start.isoformat()}|{window_end.isoformat()}"


def load_build_state(runner: Runner, state_path: Path, mode: str) -> dict[str, Any]:
    state = runner.read_json(state_path, {})
    if not isinstance(state, dict) or state.get("mode") != mode:
        state = {}
    completed = state.get("completedNodes", [])
    if not isinstance(completed, list):
        completed = []
    state["completedNodes"] = completed
    return state


def save_build_state(
    runner: Runner,
    state_path: Path,
    mode: str,
    completed_nodes: list[str],
    current_node: dict[str, Any] | None,
    accepted_count: int,
    queue_size: int,
) -> None:
    runner.write_json(
        state_path,
        {
            "generatedAt": dt.datetime.now().astimezone().isoformat(),
            "mode": mode,
            "screenSchemaVersion": SCREEN_SCHEMA_VERSION,
            "completedNodes": completed_nodes,
            "currentNode": current_node,
            "acceptedCount": accepted_count,
            "queueSize": queue_size,
        },
    )


def write_screening_checkpoint(
    runner: Runner,
    queue_path: Path,
    cache_path: Path,
    state_path: Path,
    queue: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    cache_items: list[dict[str, Any]],
    mode: str,
    batch_count: int,
    total_batches: int,
    processed_count: int,
    total_to_screen: int,
    completed_nodes: list[str] | None = None,
    current_node: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    merged_queue = merge_queue(queue, accepted)
    runner.write_json(queue_path, merged_queue)
    cache_by_key: dict[str, dict[str, Any]] = {}
    for item in cache_items:
        key = str(item.get("Key") or item.get("key") or "")
        if key:
            cache_by_key[key] = item
    runner.write_json(cache_path, {"generatedAt": dt.datetime.now().astimezone().isoformat(), "papers": list(cache_by_key.values())})
    runner.write_json(
        state_path,
        {
            "generatedAt": dt.datetime.now().astimezone().isoformat(),
            "mode": mode,
            "screenSchemaVersion": SCREEN_SCHEMA_VERSION,
            "batchCount": batch_count,
            "totalBatches": total_batches,
            "processedCount": processed_count,
            "totalToScreen": total_to_screen,
            "acceptedCount": len(accepted),
            "queueSize": len(merged_queue),
            "completedNodes": completed_nodes or [],
            "currentNode": current_node,
            "queuePath": str(queue_path),
            "cachePath": str(cache_path),
        },
    )
    return merged_queue


def validate_minimax_results(screen_response: dict[str, Any], batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = screen_response.get("results", [])
    if not isinstance(results, list):
        raise RuntimeError("MiniMax response is missing a list field: results.")
    by_key = {paper["Key"]: paper for paper in batch}
    result_keys: set[str] = set()
    normalized_results: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            raise RuntimeError("MiniMax response contains a non-object result item.")
        key = str(result.get("key", ""))
        if key:
            result_keys.add(key)
        normalized_results.append(result)
    missing_keys = [key for key in by_key if key not in result_keys]
    extra_count = len([key for key in result_keys if key not in by_key])
    if missing_keys:
        raise RuntimeError(
            f"MiniMax response count mismatch: input={len(batch)}, output={len(normalized_results)}, "
            f"missing={len(missing_keys)}, extra={extra_count}; missingKeys={', '.join(missing_keys[:5])}"
        )
    return normalized_results


def minimax_screen_batch_checked(runner: Runner, minimax: dict[str, Any], batch: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    max_retries = int(minimax.get("maxRetries", 4))
    retry_base_sec = float(minimax.get("retryBaseSec", 5))
    for attempt in range(1, max_retries + 2):
        screen_response = minimax_screen_batch(runner, minimax, batch, config)
        raw_results = screen_response.get("results", []) if isinstance(screen_response, dict) else []
        raw_count = len(raw_results) if isinstance(raw_results, list) else 0
        runner.log(f"MiniMax response count | attempt={attempt}; input={len(batch)}; output={raw_count}")
        try:
            results = validate_minimax_results(screen_response, batch)
            runner.log(f"MiniMax response validated | attempt={attempt}; input={len(batch)}; output={len(results)}")
            return results
        except Exception as exc:
            if attempt > max_retries:
                raise
            wait_sec = min(60.0, retry_base_sec * (2 ** (attempt - 1)))
            runner.log(f"WARNING: MiniMax response validation failed; attempt={attempt}; retryInSec={wait_sec:g}; {exc}")
            time.sleep(wait_sec)
    raise RuntimeError("MiniMax response validation failed after retries.")


def parse_model_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start : end + 1]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        preview = clean_text(cleaned[:500])
        raise RuntimeError(f"Invalid MiniMax JSON: {exc}; preview={preview}") from exc


def load_minimax_config(runner: Runner, path: Path) -> dict[str, Any]:
    cfg = runner.read_json(path, {})
    api_key = str(cfg.get("apiKey") or os.environ.get("MINIMAX_API_KEY") or "").strip()
    is_placeholder = api_key == "YOUR_MINIMAX_API_KEY" or (api_key.startswith("sk-") and set(api_key[6:]) <= {"x", "X", "*"})
    if not api_key or is_placeholder:
        raise RuntimeError(f"Missing MiniMax API key. Set MINIMAX_API_KEY or create {path}.")
    return {
        "apiKey": api_key,
        "baseUrl": cfg.get("baseUrl", "https://api.minimaxi.com/v1"),
        "model": cfg.get("model", "MiniMax-M2.7"),
        "fallbackModels": [str(model) for model in cfg.get("fallbackModels", []) if str(model).strip()],
        "batchSize": int(cfg.get("batchSize", 20)),
        "timeoutSec": int(cfg.get("timeoutSec", 120)),
        "maxRetries": int(cfg.get("maxRetries", 4)),
        "retryBaseSec": float(cfg.get("retryBaseSec", 5)),
    }


def minimax_screen_batch(runner: Runner, minimax: dict[str, Any], papers: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    directions = research_directions(config)
    payload_papers = [
        {
            "key": clean_text(paper.get("Key")),
            "title": clean_text(paper.get("Title")),
            "abstract": paper_abstract(paper),
        }
        for paper in papers
    ]
    system = (
        "You are an academic paper screening assistant for a PhD literature radar. "
        "You must evaluate papers only from the supplied title and abstract. "
        "Return valid strict JSON only. No markdown, no explanations, no extra text."
    )
    user = f"""Task:
Screen each paper for topical relevance to the user's research agenda. The journal, publication date, and Boolean search filters have already been applied. Your job is to remove false positives and produce compact classification metadata for manual reading.

Research directions:
1. transport_energy_integration
   Transport-energy integration, especially EVs, electric buses, charging infrastructure, wireless charging, smart grids, demand response, renewable energy, energy systems, planning, scheduling, operations, or optimization.

2. transport_emergency_resilience
   Transport disruption, public transport or transit emergency operations, resilience, disruption management, routing, scheduling, resource allocation, recovery, and multimodal emergency response.

3. or_ai
   AI/ML for operations research and optimization, including predict-then-optimize, decision-focused learning, learning to optimize, learning-augmented optimization, MIP, combinatorial optimization, stochastic programming, Benders decomposition, solver learning, or data-driven optimization.

4. ai_behavior
   AI/ML/LLM/deep learning for travel behavior, traveler/passenger behavior, choice modeling, mode choice, discrete choice, preference learning, or transportation/mobility/airport/transit behavior analysis.

Decision rules:
- Use only the title and abstract. Do not infer unstated topics.
- accept=true only if the paper genuinely matches at least one research direction.
- Reject keyword-only false positives, broad background matches, or papers where the relevant term is only incidental.
- If multiple directions are relevant, include all matched directions in "directions" and choose the strongest one as "primaryDirection".
- If accept=false, use primaryDirection="" and directions=[].
- Score measures topical relevance only, not journal quality, recency, or expected citation impact.

Scoring scale:
0-2: irrelevant to the research directions.
3: weak keyword-only or false-positive match.
4-5: peripheral or partial relevance.
6-7: relevant and worth checking.
8-9: strong match to one research direction.
10: highly central to the user's research agenda.

Tagging rules:
- Tags must be justified by the title or abstract.
- Do not force all tag fields. Omit unsupported fields.
- For accepted papers, provide 5-12 useful tags.
- For rejected papers, provide at most 3 tags.
- Prefer precise academic phrases over generic terms.
- Tag values should be short phrases or short arrays, not full sentences.
- "Keywords" should contain 3-8 concise phrases.
- Do not summarize the whole abstract in tags.

Allowed tag fields:
Object; Scenario; Application domain; Transport mode; Energy component; Charging type; Infrastructure; Battery treatment; Grid interaction; Disruption type; Emergency context; Response strategy; Resilience aspect; Behavior subject; Behavior context; Behavior model; AI component; OR component; Integration form; Problem type; Decision level; Method; Solver/algorithm; Learning target; Optimization target; Uncertainty; Data usage; Objective; Stakeholder; Policy relevance; Computational aspect; Evaluation setting; Output type; Keywords.

Comment rule:
- comment must be exactly one concise, objective, academic sentence.
- The sentence should describe the paper's main contribution or focus.
- Do not write pros/cons.
- Do not mention journal quality.
- Do not say "this paper is relevant because...".
- Do not assign related-work section names.

Return format:
Return one JSON object with exactly this structure:
{{
  "results": [
    {{
      "key": "same key as input",
      "accept": true,
      "primaryDirection": "transport_energy_integration",
      "directions": ["transport_energy_integration"],
      "score": 8,
      "tags": {{
        "Object": "electric bus charging system",
        "Method": "mixed-integer optimization",
        "Keywords": ["electric bus", "charging infrastructure", "optimization"]
      }},
      "comment": "The study develops an optimization model for electric bus charging infrastructure planning under operational constraints."
    }}
  ]
}}

Strict JSON requirements:
- Use double quotes for all keys and string values.
- Do not use markdown code fences.
- Do not include trailing commas.
- Do not include null values.
- Every input paper must appear exactly once in results.
- Keep the original paper key unchanged.

Papers:
{json.dumps(payload_papers, ensure_ascii=False, indent=2)}
"""
    url = f"{str(minimax['baseUrl']).rstrip('/')}/chat/completions"
    models = [str(minimax["model"])] + [model for model in minimax.get("fallbackModels", []) if model != minimax["model"]]
    errors: list[str] = []
    max_retries = int(minimax.get("maxRetries", 4))
    retry_base_sec = float(minimax.get("retryBaseSec", 5))
    for model in models:
        body = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        for attempt in range(1, max_retries + 2):
            try:
                response = runner.request_json(
                    "POST",
                    url,
                    body=body,
                    headers={"Authorization": f"Bearer {minimax['apiKey']}"},
                    timeout=int(minimax["timeoutSec"]),
                )
                content = str(response["choices"][0]["message"]["content"])
                runner.log(f"MiniMax model used: {model}; attempt={attempt}")
                return parse_model_json(content)
            except Exception as exc:
                message = str(exc)
                if not is_retryable_minimax_error(message) or attempt > max_retries:
                    errors.append(f"{model}: {message}")
                    runner.log(f"WARNING: MiniMax model failed: {model}; attempt={attempt}; {message}")
                    break
                wait_sec = min(60.0, retry_base_sec * (2 ** (attempt - 1)))
                runner.log(f"WARNING: MiniMax transient failure: {model}; attempt={attempt}; retryInSec={wait_sec:g}; {message}")
                time.sleep(wait_sec)
    raise RuntimeError("All MiniMax models failed: " + " | ".join(errors))


def is_retryable_minimax_error(message: str) -> bool:
    lowered = message.lower()
    permanent_markers = [
        "not support model",
        "invalid api key",
        "unauthorized",
        "permission",
        "forbidden",
        "insufficient",
    ]
    if any(marker in lowered for marker in permanent_markers):
        return False
    transient_markers = [
        "request拥挤",
        "请求拥挤",
        "too many requests",
        "rate limit",
        "timeout",
        "timed out",
        "temporarily",
        "server_error",
        "invalid minimax json",
        "expecting property name enclosed in double quotes",
        "expecting value",
        "unterminated string",
        "extra data",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "connection",
        "max retries",
    ]
    return any(marker in lowered for marker in transient_markers)


def is_retryable_crossref_error(message: str) -> bool:
    lowered = message.lower()
    transient_markers = [
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
    return any(marker in lowered for marker in transient_markers)


def target_journal_specs(runner: Runner, config: dict[str, Any]) -> list[dict[str, Any]]:
    specs = []
    for entry in config.get("topJournals", []):
        if isinstance(entry, str):
            name = entry
            issns = list(JOURNAL_ISSN_MAP.get(name, []))
        else:
            name = str(entry.get("name") or entry.get("title") or "")
            raw_issns = entry.get("issns", entry.get("issn", []))
            issns = raw_issns if isinstance(raw_issns, list) else [raw_issns]
            issns = [str(value) for value in issns if value]
        if not name:
            continue
        specs.append({"name": name, "issns": issns})
    return specs


def load_wos_config(runner: Runner) -> dict[str, Any]:
    path = runner.workspace / "automation" / "wos.config.json"
    cfg = runner.read_json(path, {}) if path.exists() else {}
    if not isinstance(cfg, dict):
        cfg = {}

    alldb_url = "https://www.webofscience.com/wos/alldb/basic-search"

    def normalize_wos_url(value: Any) -> str:
        url = str(value or "").strip()
        if not url:
            return alldb_url
        if url in {"https://www.webofscience.com", "https://www.webofscience.com/"}:
            return alldb_url
        if "/wos/woscc/basic-search" in url:
            return alldb_url
        return url

    return {
        "startUrl": normalize_wos_url(cfg.get("startUrl")),
        "downloadDir": str(cfg.get("downloadDir") or "data/wos_exports"),
        "browserProfileDir": str(cfg.get("browserProfileDir") or "data/browser_profiles/wos"),
        "manualExportTimeoutSec": int(cfg.get("manualExportTimeoutSec", 900)),
        "openBrowserForExport": bool(cfg.get("openBrowserForExport", True)),
        "reuseExistingExports": bool(cfg.get("reuseExistingExports", True)),
        "account": str(cfg.get("account") or ""),
        "password": str(cfg.get("password") or ""),
        "autoSearch": bool(cfg.get("autoSearch", True)),
        "basicSearchUrl": normalize_wos_url(cfg.get("basicSearchUrl")),
        "basicSearchLoadWaitSec": float(cfg.get("basicSearchLoadWaitSec") or 0.5),
        "afterSearchWaitSec": float(cfg.get("afterSearchWaitSec") or 1),
        "wosWaitTimeoutSec": int(cfg.get("wosWaitTimeoutSec") or 60),
        "loginFormWaitSec": float(cfg.get("loginFormWaitSec") or 3),
        "openEachKeywordInNewTab": bool(cfg.get("openEachKeywordInNewTab", True)),
        "beforeSearchClickWaitSec": float(cfg.get("beforeSearchClickWaitSec") or 0.25),
        "navigationWaitSec": float(cfg.get("navigationWaitSec") or 20),
        "autoExport": bool(cfg.get("autoExport", True)),
        "exportFormatLabel": str(cfg.get("exportFormatLabel") or "Plain Text File"),
        "exportRecordContent": str(cfg.get("exportRecordContent") or "Author, Title, Source, Abstract"),
        "exportRecordFrom": int(cfg.get("exportRecordFrom") or 1),
        "exportRecordTo": int(cfg.get("exportRecordTo") or 1000),
        "exportWaitTimeoutSec": int(cfg.get("exportWaitTimeoutSec") or 90),
    }

def stable_text_exports(export_dir: Path, since: float | None = None) -> list[Path]:
    files = []
    for path in export_dir.glob("*.txt"):
        if since is not None and path.stat().st_mtime < since:
            continue
        if path.stat().st_size > 0:
            files.append(path)
    return sorted(files)


def wait_for_wos_exports(export_dir: Path, timeout_sec: int, since: float, runner: Runner) -> list[Path]:
    deadline = time.time() + timeout_sec
    last_log = 0.0
    while time.time() < deadline:
        files = stable_text_exports(export_dir, since)
        partials = list(export_dir.glob("*.crdownload")) + list(export_dir.glob("*.tmp"))
        if files and not partials:
            time.sleep(1.0)
            return stable_text_exports(export_dir, since)
        if time.time() - last_log >= 30:
            runner.log(f"Waiting for WoS plain text export in {export_dir}; found={len(files)}; partialDownloads={len(partials)}")
            last_log = time.time()
        time.sleep(2.0)
    raise RuntimeError(f"Timed out waiting for WoS plain text export under {export_dir}.")


class PaperRadarAgent:
    def __init__(self, args: argparse.Namespace, runner: Runner, config: dict[str, Any]) -> None:
        self.args = args
        self.runner = runner
        self.config = config
        self.wos_driver: Any | None = None
        self.screening = config.get("llmScreening", {})
        self.queue_path = runner.workspace / "data" / "paper-candidate-queue.json"
        self.cache_path = runner.resolve_workspace_path(self.screening.get("cachePath", "data/paper-screening-cache.json"))
        self.state_path = runner.workspace / "data" / "paper-queue-build-state.json"
        self.crossref_checkpoint_path = runner.workspace / "data" / "paper-crossref-candidates.json"
        self.wos_checkpoint_path = runner.workspace / "data" / "paper-wos-candidates.json"
        self.today = dt.date.today()
        if args.mode == "Weekly":
            lookback = args.lookback_days or int(config.get("task", {}).get("weeklyLookbackDays", 7))
            self.min_date = self.today - dt.timedelta(days=lookback)
            self.source_policy = "weekly_crossref_refresh"
            self.candidate_path = self.crossref_checkpoint_path
        else:
            self.min_date = DEFAULT_FROM_DATE
            self.source_policy = "wos_fetch_only" if args.mode == "FetchWoS" else "full_wos_export_review"
            self.candidate_path = self.wos_checkpoint_path
        self.from_date = self.min_date.isoformat()
        self.until_date = self.today.isoformat()
        self.windows = date_windows(self.min_date, self.today)
        self.specs = target_journal_specs(runner, config)
        self.target_names = [spec["name"] for spec in self.specs]
        if not self.specs:
            raise RuntimeError("No topJournals configured. Queue building requires target journals as a hard filter.")

        if args.mode == "FetchWoS":
            self.minimax_path = None
            self.minimax = {}
            self.batch_size = 0
            self.queue = []
            self.new_cache = []
            self.all_candidates = []
            self.state = {}
            self.completed_nodes = []
            self.completed_node_set = set()
            self.accepted = []
            self.batch_count = 0
            self.raw_count = 0
            self.screened_count = 0
            self.min_llm_score = 0
            return

        minimax_path = runner.resolve_workspace_path(self.screening.get("configPath", "automation/minimax.config.json"))
        if self.cache_path is None or minimax_path is None:
            raise RuntimeError("Missing cachePath or MiniMax configPath.")
        self.minimax_path = minimax_path
        self.minimax = load_minimax_config(runner, minimax_path)
        self.batch_size = int(self.minimax["batchSize"])

        self.queue = runner.read_json(self.queue_path, [])
        if not isinstance(self.queue, list):
            self.queue = []
        queue_before = len(self.queue)
        self.queue = [paper for paper in self.queue if is_target_journal(str(paper.get("Journal", "")), self.target_names)]
        if len(self.queue) < queue_before:
            runner.log(f"Existing queue journal hard filter removed {queue_before - len(self.queue)} stale non-target papers before merge.")

        cache_data = runner.read_json(self.cache_path, {"papers": []})
        cache_items = cache_data.get("papers", []) if isinstance(cache_data, dict) else []
        self.new_cache = cache_items if isinstance(cache_items, list) else []
        candidate_data = runner.read_json(self.candidate_path, {"papers": []})
        candidate_items = candidate_data.get("papers", []) if isinstance(candidate_data, dict) else []
        self.all_candidates = candidate_items if isinstance(candidate_items, list) else []
        self.state = load_build_state(runner, self.state_path, args.mode)
        self.completed_nodes = [str(node) for node in self.state.get("completedNodes", [])]
        self.completed_node_set = set(self.completed_nodes)
        self.accepted: list[dict[str, Any]] = []
        self.batch_count = 0
        self.raw_count = 0
        self.screened_count = 0
        self.min_llm_score = int(self.screening.get("minLlmScore", 6))

    def run(self) -> None:
        self.log_start()
        self.log_targets()
        if self.args.mode == "FetchWoS":
            self.run_fetch_wos_only()
        elif self.args.mode == "Full":
            self.run_full_from_wos_exports()
        else:
            self.run_weekly_from_crossref()
        self.finish()

    def log_start(self) -> None:
        if self.args.mode == "FetchWoS":
            self.runner.log(
                f"PaperRadarAgent start. mode={self.args.mode}; policy={self.source_policy}; "
                f"minDate={self.from_date}; dateWindows={len(self.windows)}"
            )
        else:
            self.runner.log(
                f"PaperRadarAgent start. mode={self.args.mode}; policy={self.source_policy}; "
                f"batchSize={self.batch_size}; minDate={self.from_date}; dateWindows={len(self.windows)}"
            )
        if self.runner.proxy_url:
            self.runner.log(f"HTTP proxy enabled for API requests: {self.runner.proxy_url}")
        if self.args.mode != "FetchWoS":
            self.runner.log(f"MiniMax config: baseUrl={self.minimax['baseUrl']}; model={self.minimax['model']}; configPath={self.minimax_path}")

    def run_fetch_wos_only(self) -> None:
        files = self.ensure_wos_exports()
        imported = 0
        kept = 0
        fetched: list[dict[str, Any]] = []
        for path in files:
            records = parse_wos_plain_text(path, self.today)
            imported += len(records)
            for record in records:
                if not self.resolve_target_journal_name(str(record.get("Journal", ""))):
                    continue
                date_value = str(record.get("DateObject") or "")
                if not date_value:
                    continue
                try:
                    record_date = dt.date.fromisoformat(date_value[:10])
                except Exception:
                    continue
                if record_date < self.min_date or record_date > self.today:
                    continue
                fetched.append(record)
                kept += 1
        deduped = merge_papers_by_key([], fetched)
        self.raw_count = imported
        self.all_candidates = deduped
        self.runner.write_json(
            self.wos_checkpoint_path,
            {
                "generatedAt": dt.datetime.now().astimezone().isoformat(),
                "mode": self.args.mode,
                "source": "wos_export",
                "fromDate": self.from_date,
                "untilDate": self.until_date,
                "rawCount": imported,
                "keptHardFiltered": kept,
                "dedupedCount": len(deduped),
                "papers": deduped,
            },
        )
        self.runner.log(
            f"WoS fetch-only complete | imported={imported}; keptHardFiltered={kept}; deduped={len(deduped)}; "
            f"output={self.wos_checkpoint_path}"
        )

    def log_targets(self) -> None:
        for spec in self.specs:
            if spec["issns"]:
                self.runner.log(f"Target journal resolved | {spec['name']} | issn={','.join(spec['issns'])}")
            else:
                self.runner.log(f"Target journal resolved | {spec['name']} | issn=NONE; exact local journal check required.")

    def run_full_from_wos_exports(self) -> None:
        files = self.ensure_wos_exports()
        export_dir = files[0].parent if files else self.runner.workspace / "data" / "wos_exports"
        self.runner.log(f"Agent selected tool: WoSPlainTextImport; exportDir={export_dir}; files={len(files)}")
        grouped: dict[str, dict[str, Any]] = {}
        imported = 0
        kept = 0
        for path in files:
            records = parse_wos_plain_text(path, self.today)
            imported += len(records)
            for record in records:
                spec_name = self.resolve_target_journal_name(str(record.get("Journal", "")))
                if not spec_name or not record.get("DateObject"):
                    continue
                record_date = dt.date.fromisoformat(str(record["DateObject"])[:10])
                if record_date < self.min_date or record_date > self.today:
                    continue
                window = self.window_for_date(record_date)
                if window is None:
                    continue
                window_start, window_end = window
                node_key = "wos|" + build_node_key(spec_name, window_start, window_end)
                node = grouped.setdefault(
                    node_key,
                    {
                        "currentNode": {
                            "key": node_key,
                            "journal": spec_name,
                            "windowStart": window_start.isoformat(),
                            "windowEnd": window_end.isoformat(),
                            "source": "wos_export",
                        },
                        "papers": [],
                    },
                )
                node["papers"].append(record)
                kept += 1
        self.runner.log(f"WoS import complete | imported={imported}; keptHardFiltered={kept}; nodes={len(grouped)}")
        for node_key in sorted(grouped):
            if node_key in self.completed_node_set:
                self.runner.log(f"Skipping completed node | node={node_key}")
                continue
            node = grouped[node_key]
            self.process_node(node["currentNode"], node["papers"], "wos")

    def ensure_wos_exports(self) -> list[Path]:
        wos_config = load_wos_config(self.runner)
        export_dir = self.runner.resolve_workspace_path(wos_config["downloadDir"])
        profile_dir = self.runner.resolve_workspace_path(wos_config["browserProfileDir"])
        if export_dir is None or profile_dir is None:
            raise RuntimeError("Invalid WoS exportDir or browserProfileDir.")

        export_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.mkdir(parents=True, exist_ok=True)

        existing = stable_text_exports(export_dir)
        if existing and wos_config["reuseExistingExports"]:
            self.runner.log(f"WoS exports already available | exportDir={export_dir}; files={len(existing)}")
            return existing
        if not wos_config["openBrowserForExport"]:
            raise RuntimeError(f"No WoS exports found under {export_dir}, and openBrowserForExport=false.")

        started_at = time.time()
        self.wos_driver = self.open_wos_browser(wos_config, export_dir, profile_dir)
        self.runner.log(
            "WoS browser is open. Automatic login has been attempted if account/password are configured. "
            "Complete search/export if needed, then export records as a Plain Text File. "
            "The agent is waiting for the downloaded .txt file."
        )
        return wait_for_wos_exports(export_dir, int(wos_config["manualExportTimeoutSec"]), started_at, self.runner)


    def open_wos_browser(self, wos_config: dict[str, Any], export_dir: Path, profile_dir: Path) -> Any:
        from selenium import webdriver

        options = webdriver.ChromeOptions()
        options.add_argument(f"--user-data-dir={profile_dir}")
        options.add_argument("--start-maximized")
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--ignore-ssl-errors=yes")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-notifications")
        options.add_argument("--log-level=3")
        options.add_argument("--disable-logging")
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_experimental_option(
            "prefs",
            {
                "download.default_directory": str(export_dir.resolve()),
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "safebrowsing.enabled": True,
            },
        )
        driver = webdriver.Chrome(options=options)
        self.wos_driver = driver

        self._navigate_wos_page(
            driver,
            str(wos_config["startUrl"]),
            timeout=float(wos_config.get("navigationWaitSec") or 20),
            label="start",
        )
        self.runner.log(
            f"WoS browser opened | startUrl={wos_config['startUrl']} | "
            f"currentUrl={getattr(driver, 'current_url', '')} | "
            f"downloadDir={export_dir} | profileDir={profile_dir}"
        )
        self.try_wos_login(driver, wos_config)
        if bool(wos_config.get("autoSearch", True)):
            self.perform_wos_keyword_searches(driver, wos_config)
        else:
            self.runner.log("WoS automatic search skipped because autoSearch=false in automation/wos.config.json.")
        return driver

    def _navigate_wos_page(self, driver: Any, url: str, timeout: float = 20, label: str = "wos") -> None:
        """Navigate to WoS and refuse to continue if Chrome remains on a blank page."""
        from selenium.webdriver.common.by import By

        last_url = ""
        for attempt in range(1, 4):
            if attempt == 1:
                driver.get("about:blank")
                time.sleep(0.8)
            driver.get(url)
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    current = str(getattr(driver, "current_url", "") or "")
                    last_url = current
                    body = driver.find_elements(By.TAG_NAME, "body")
                    has_body_text = bool(body and clean_text(body[0].text))
                    has_wos_form = bool(driver.find_elements(By.CSS_SELECTOR, "form[data-ta='search-form'], #snSearchType"))
                    has_buaa_login = bool(driver.find_elements(By.ID, "unPassword"))
                    if current and not current.startswith("about:blank") and (has_body_text or has_wos_form or has_buaa_login):
                        return
                except Exception:
                    pass
                time.sleep(0.5)
            self.runner.log(f"WARNING: WoS navigation retry | label={label}; attempt={attempt}; currentUrl={last_url}")
        raise RuntimeError(
            f"WoS navigation failed: browser stayed blank or page did not load; "
            f"label={label}; url={url}; lastUrl={last_url}"
        )

    def try_wos_login(self, driver: Any, wos_config: dict[str, Any]) -> None:
        """Try automatic BUAA unified-auth login using automation/wos.config.json.

        Expected config:
        {
          "account": "your_student_or_staff_id",
          "password": "your_password"
        }

        If the page is already logged in, or the current page is not the BUAA login page,
        this function safely skips. If a captcha/SMS/QR step appears, it asks the user to
        complete that step manually and then press Enter.
        """
        account = str(wos_config.get("account") or "").strip()
        password = str(wos_config.get("password") or "")
        if not account or not password:
            self.runner.log("WoS login skipped: account/password are not configured in automation/wos.config.json.")
            return

        try:
            self._login_buaa_sso(driver, account, password, timeout=float(wos_config.get("loginFormWaitSec") or 3))
        except Exception as exc:
            self.runner.log(f"WARNING: automatic BUAA login did not complete: {exc}")
            self.runner.log("If the browser is on a captcha/SMS/QR page, complete it manually. The program will keep waiting for WoS export.")

    def _login_buaa_sso(self, driver: Any, account: str, password: str, timeout: float = 3) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, timeout)

        username_locator = (By.ID, "unPassword")
        password_locator = (By.ID, "pwPassword")
        login_button_locator = (By.CSS_SELECTOR, "input.submit-btn[onclick='loginPassword()'], input.submit-btn[value='登录']")

        # WoS may first redirect through several pages. Wait briefly for the BUAA login
        # form. If it never appears, assume the profile is already logged in or the
        # current page is not BUAA SSO.
        try:
            username_box = wait.until(EC.element_to_be_clickable(username_locator))
        except Exception:
            self.runner.log("BUAA login skipped: already logged in or no password form detected.")
            return

        password_box = wait.until(EC.element_to_be_clickable(password_locator))

        username_box.click()
        username_box.clear()
        username_box.send_keys(account)

        password_box.click()
        password_box.clear()
        password_box.send_keys(password)

        if self._is_visible_by_id(driver, "captchaPasswor"):
            input("\n检测到验证码登录框。请在浏览器中手动输入验证码并完成登录，然后回到这里按 Enter 继续...\n")
            self.runner.log("BUAA login continued after manual captcha handling.")
            return

        login_button = wait.until(EC.element_to_be_clickable(login_button_locator))
        login_button.click()
        self.runner.log("BUAA username/password submitted automatically.")

        # Give SSO time to redirect back to WoS. If additional verification appears,
        # ask the user to complete it instead of trying to bypass it.
        time.sleep(3.0)
        if self._is_visible_by_id(driver, "captchaPasswor") or self._is_visible_by_id(driver, "captchaSmsToken"):
            input("\n登录后出现验证码/短信验证。请在浏览器中手动完成，然后回到这里按 Enter 继续...\n")
            self.runner.log("BUAA login continued after manual secondary verification.")
            return

        current_url = str(getattr(driver, "current_url", ""))
        if "login" in current_url.lower() and self._is_visible_by_id(driver, "errPassword"):
            err_text = self._safe_element_text_by_id(driver, "errPassword")
            if err_text:
                raise RuntimeError(f"BUAA login page still visible; errPassword={err_text}")

        self.runner.log("BUAA login step finished; waiting for WoS page/export workflow.")

    def _is_visible_by_id(self, driver: Any, element_id: str) -> bool:
        from selenium.webdriver.common.by import By

        try:
            element = driver.find_element(By.ID, element_id)
            return bool(element.is_displayed())
        except Exception:
            return False

    def _safe_element_text_by_id(self, driver: Any, element_id: str) -> str:
        from selenium.webdriver.common.by import By

        try:
            return clean_text(driver.find_element(By.ID, element_id).text)
        except Exception:
            return ""


    def perform_wos_keyword_searches(self, driver: Any, wos_config: dict[str, Any]) -> None:
        """Run one WoS Fielded Search for each keyword in paper-watch.config.json."""
        keyword_queries = self._build_wos_keyword_queries()
        if not keyword_queries:
            self.runner.log("WoS automatic search skipped: no keyword query found in paper-watch.config.json.")
            return

        journal_query = self._build_wos_publication_titles_query()
        if not journal_query:
            self.runner.log("WoS automatic search skipped: no target journals found in paper-watch.config.json.")
            return

        year_query = self._build_wos_year_query()
        open_new_tab = bool(wos_config.get("openEachKeywordInNewTab", True))
        total = len(keyword_queries)

        for index, keyword in enumerate(keyword_queries, start=1):
            if index > 1 and open_new_tab:
                self._open_new_browser_tab(driver)

            self.runner.log(f"WoS keyword search start | {index}/{total}; keyword={keyword['name']}")
            self.perform_wos_basic_search(
                driver=driver,
                wos_config=wos_config,
                topic_query=self._normalize_wos_topic_query(keyword["query"]),
                journal_query=journal_query,
                year_query=year_query,
                keyword_name=keyword["name"],
                index=index,
                total=total,
            )

        self.runner.log(f"WoS keyword searches submitted | total={total}")

    def perform_wos_basic_search(
        self,
        driver: Any,
        wos_config: dict[str, Any],
        topic_query: str,
        journal_query: str,
        year_query: str,
        keyword_name: str,
        index: int,
        total: int,
    ) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        basic_url = str(wos_config.get("basicSearchUrl") or "https://www.webofscience.com/wos/woscc/basic-search")
        wait_timeout = int(wos_config.get("wosWaitTimeoutSec") or 90)
        page_wait_sec = float(wos_config.get("basicSearchLoadWaitSec") or 0.5)
        after_search_wait_sec = float(wos_config.get("afterSearchWaitSec") or 1)
        wait = WebDriverWait(driver, wait_timeout)

        self._navigate_wos_page(
            driver,
            basic_url,
            timeout=float(wos_config.get("navigationWaitSec") or 20),
            label=f"search-{index}",
        )
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "form[data-ta='search-form'], #snSearchType")))
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[data-ta='search-criteria-input'], input[name='search-main-box']")))
        if page_wait_sec > 0:
            time.sleep(page_wait_sec)

        self._ensure_wos_search_row_count(driver, 3, timeout=wait_timeout)
        self._set_wos_search_row(driver, 0, "Topic", topic_query, timeout=wait_timeout)
        self._set_wos_search_row(driver, 1, "Publication/Source Titles", journal_query, timeout=wait_timeout)
        self._set_wos_search_row(driver, 2, "Year Published", year_query, timeout=wait_timeout)
        self._verify_wos_search_fields(driver, ["Topic", "Publication/Source Titles", "Year Published"])

        try:
            driver.execute_script("if (document.activeElement) document.activeElement.blur();")
        except Exception:
            pass
        time.sleep(float(wos_config.get("beforeSearchClickWaitSec") or 0.25))
        search_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-ta='run-search']")))
        self._scroll_into_view(driver, search_button)
        search_button.click()
        self.runner.log(f"WoS fielded search submitted | {index}/{total}; keyword={keyword_name}")
        if after_search_wait_sec > 0:
            time.sleep(after_search_wait_sec)
        if bool(wos_config.get("autoExport", True)):
            self.export_wos_plain_text(driver, wos_config, keyword_name=keyword_name, index=index, total=total)

    def _build_wos_keyword_queries(self) -> list[dict[str, str]]:
        queries: list[dict[str, str]] = []
        for idx, item in enumerate(self.config.get("keywords", []), start=1):
            text = search_query_text(item).strip()
            if not text:
                continue
            name = search_query_name(item).strip() or f"keyword_{idx}"
            queries.append({"name": name, "query": text})
        return queries

    def _normalize_wos_topic_query(self, query: str) -> str:
        text = str(query or "").strip()
        match = re.fullmatch(r"(?is)\s*TS\s*=\s*\((.*)\)\s*", text)
        if match:
            return match.group(1).strip()
        match = re.fullmatch(r"(?is)\s*TS\s*=\s*(.*)\s*", text)
        if match:
            return match.group(1).strip()
        return text

    def _build_wos_publication_titles_query(self) -> str:
        names = [str(name).strip() for name in self.target_names if str(name).strip()]
        return " OR ".join(f'"{name}"' for name in names)

    def _build_wos_year_query(self) -> str:
        start_year = int(self.min_date.year)
        end_year = int(self.today.year)
        return " OR ".join(str(year) for year in range(start_year, end_year + 1))

    def _ensure_wos_search_row_count(self, driver: Any, count: int, timeout: int = 30) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, timeout)
        for _ in range(count + 3):
            rows = driver.find_elements(By.CSS_SELECTOR, "app-search-row")
            if len(rows) >= count:
                return
            add_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-ta='add-row']")))
            self._scroll_into_view(driver, add_button)
            add_button.click()
            time.sleep(0.25)
        rows = driver.find_elements(By.CSS_SELECTOR, "app-search-row")
        raise RuntimeError(f"Could not create enough WoS search rows: expected={count}; actual={len(rows)}")

    def _set_wos_search_row(self, driver: Any, row_index: int, field_name: str, value: str, timeout: int = 30) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, timeout)
        rows = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "app-search-row")))
        if row_index >= len(rows):
            raise RuntimeError(f"WoS row index out of range: row_index={row_index}; rows={len(rows)}")
        row = rows[row_index]
        self._scroll_into_view(driver, row)

        # Field selection must be finished before writing text.  WoS uses an
        # Angular/Material form; visible text alone is not enough if the dropdown
        # option is not committed.
        self._select_wos_search_field(driver, row, field_name, timeout=timeout)
        self._verify_single_wos_search_field(row, field_name)

        input_box = row.find_element(By.CSS_SELECTOR, "input[data-ta='search-criteria-input'], input[name='search-main-box']")
        wait.until(lambda _: input_box.is_displayed() and input_box.is_enabled())
        self._paste_wos_input_value(driver, input_box, value)

        # Force blur/change so Angular commits the form-control value before the
        # Search button is clicked. This is the main fix for the case where the
        # UI visibly contains text but a later manual Search still fails.
        input_box.send_keys(Keys.TAB)
        time.sleep(0.15)
        actual = clean_text(input_box.get_attribute("value"))
        expected = clean_text(value)
        if actual != expected:
            raise RuntimeError(
                f"WoS input value mismatch after paste: row={row_index + 1}; "
                f"field={field_name}; actualLen={len(actual)}; expectedLen={len(expected)}"
            )

    def _paste_wos_input_value(self, driver: Any, input_box: Any, value: str) -> None:
        from selenium.webdriver.common.keys import Keys

        input_box.click()
        time.sleep(0.05)
        input_box.send_keys(Keys.CONTROL, "a")
        input_box.send_keys(Keys.BACKSPACE)
        time.sleep(0.05)

        pasted = False
        try:
            import pyperclip  # type: ignore

            pyperclip.copy(value)
            input_box.send_keys(Keys.CONTROL, "v")
            pasted = True
        except Exception:
            input_box.send_keys(value)

        # Selenium key input normally fires input/change events, but explicitly
        # dispatch them as a safety net for WoS Angular controls.
        try:
            driver.execute_script(
                """
                const el = arguments[0];
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
                """,
                input_box,
            )
        except Exception:
            pass
        time.sleep(0.15 if pasted else 0.25)

    def _select_wos_search_field(self, driver: Any, row: Any, field_name: str, timeout: int = 30) -> None:
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, timeout)
        dropdown = self._get_wos_search_field_dropdown(row)
        if self._wos_dropdown_has_field(dropdown, field_name):
            return

        self._scroll_into_view(driver, dropdown)
        dropdown.click()
        option = self._find_exact_visible_option_by_text(driver, field_name, timeout=timeout)
        self._scroll_into_view(driver, option)
        option.click()
        wait.until(lambda _: self._wos_dropdown_has_field(self._get_wos_search_field_dropdown(row), field_name))
        time.sleep(0.15)

    def _verify_single_wos_search_field(self, row: Any, expected: str) -> None:
        dropdown = self._get_wos_search_field_dropdown(row)
        if not self._wos_dropdown_has_field(dropdown, expected):
            actual = clean_text(dropdown.text) or clean_text(dropdown.get_attribute("aria-label")) or clean_text(dropdown.get_attribute("data-ta"))
            raise RuntimeError(f"WoS search field mismatch before input: expected={expected}; actual={actual}")

    def _get_wos_search_field_dropdown(self, row: Any) -> Any:
        """Return the field dropdown in a WoS row, not the And/Or connector dropdown."""
        from selenium.webdriver.common.by import By

        candidates = row.find_elements(By.CSS_SELECTOR, "button[role='combobox']")
        field_candidates = []
        for button in candidates:
            try:
                label = clean_text(button.get_attribute("aria-label"))
                data_ta = clean_text(button.get_attribute("data-ta"))
                text = clean_text(button.text)
                lower = f"{label} {data_ta} {text}".lower()
                if "select search field" in lower:
                    return button
                if data_ta and data_ta.lower() not in {"and", "or", "not"}:
                    field_candidates.append(button)
                elif text and text.lower() not in {"and", "or", "not"}:
                    field_candidates.append(button)
            except Exception:
                continue
        if field_candidates:
            return field_candidates[-1]
        raise RuntimeError("Could not find WoS search-field dropdown in row; only connector dropdowns may be visible.")

    def _wos_dropdown_has_field(self, dropdown: Any, field_name: str) -> bool:
        wanted = field_name.strip().lower()
        aliases = {wanted}
        if wanted == "publication/source titles":
            aliases.update({"publication titles", "source titles"})
        text = clean_text(dropdown.text).lower()
        label = clean_text(dropdown.get_attribute("aria-label")).lower()
        data_ta = clean_text(dropdown.get_attribute("data-ta")).lower()
        return any(alias in text or alias in label or alias == data_ta for alias in aliases)

    def _verify_wos_search_fields(self, driver: Any, expected_fields: list[str]) -> None:
        from selenium.webdriver.common.by import By

        rows = driver.find_elements(By.CSS_SELECTOR, "app-search-row")
        for idx, expected in enumerate(expected_fields):
            if idx >= len(rows):
                raise RuntimeError(f"WoS search row missing during verification: row={idx + 1}; expected={expected}")
            dropdown = self._get_wos_search_field_dropdown(rows[idx])
            if not self._wos_dropdown_has_field(dropdown, expected):
                actual = clean_text(dropdown.text) or clean_text(dropdown.get_attribute("aria-label")) or clean_text(dropdown.get_attribute("data-ta"))
                raise RuntimeError(f"WoS search field mismatch: row={idx + 1}; expected={expected}; actual={actual}")

    def _find_exact_visible_option_by_text(self, driver: Any, text: str, timeout: int = 30) -> Any:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, timeout)
        wanted = text.strip().lower()
        aliases = {wanted}
        if wanted == "publication/source titles":
            aliases.update({"publication titles", "source titles"})

        def locate(_: Any) -> Any:
            # Restrict to the currently visible Material overlay/menu first.
            candidates = driver.find_elements(
                By.CSS_SELECTOR,
                ".cdk-overlay-container [role='option'], .cdk-overlay-container button, "
                ".cdk-overlay-container mat-option, .cdk-overlay-container span, "
                "[role='listbox'] [role='option']"
            )
            exact = []
            contains = []
            for element in candidates:
                try:
                    if not element.is_displayed():
                        continue
                    label = clean_text(element.text)
                    if not label:
                        continue
                    lower = label.lower()
                    if lower in aliases:
                        exact.append(element)
                    elif any(alias in lower for alias in aliases):
                        contains.append((len(label), element))
                except Exception:
                    continue
            if exact:
                return exact[0]
            if contains:
                contains.sort(key=lambda pair: pair[0])
                return contains[0][1]
            return False

        return wait.until(locate)

    def _scroll_into_view(self, driver: Any, element: Any) -> None:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
            time.sleep(0.05)
        except Exception:
            pass

    def _open_new_browser_tab(self, driver: Any) -> None:
        driver.execute_script("window.open('about:blank', '_blank');")
        time.sleep(0.2)
        driver.switch_to.window(driver.window_handles[-1])

    def _log_wos_rows_snapshot(self, driver: Any) -> None:
        from selenium.webdriver.common.by import By

        try:
            rows = driver.find_elements(By.CSS_SELECTOR, "app-search-row")
            for idx, row in enumerate(rows[:5], start=1):
                try:
                    field = clean_text(row.find_element(By.CSS_SELECTOR, "button[role='combobox']").text)
                except Exception:
                    field = ""
                try:
                    value = clean_text(row.find_element(By.CSS_SELECTOR, "input[data-ta='search-criteria-input'], input[name='search-main-box']").get_attribute("value"))
                except Exception:
                    value = ""
                self.runner.log(f"WoS row snapshot | row={idx}; field={field}; value={value}")
        except Exception as exc:
            self.runner.log(f"WARNING: failed to log WoS row snapshot: {exc}")

    def _log_wos_search_result_state(self, driver: Any) -> None:
        from selenium.webdriver.common.by import By

        try:
            url = str(getattr(driver, "current_url", ""))
            title = str(getattr(driver, "title", ""))
            body_text = clean_text(driver.find_element(By.TAG_NAME, "body").text)
            lowered = body_text.lower()
            if "server.unexpectederror" in lowered or "unexpectederror" in lowered:
                state = "SERVER_UNEXPECTED_ERROR"
            elif "no results" in lowered or "未找到" in body_text or "没有找到" in body_text:
                state = "NO_RESULTS_TEXT_DETECTED"
            elif "results" in lowered or "结果" in body_text:
                state = "RESULTS_TEXT_DETECTED"
            else:
                state = "UNKNOWN"
            self.runner.log(f"WoS search result state | state={state}; title={title}; url={url}")
        except Exception as exc:
            self.runner.log(f"WARNING: failed to log WoS search result state: {exc}")


    def export_wos_plain_text(self, driver: Any, wos_config: dict[str, Any], keyword_name: str, index: int, total: int) -> None:
        """Open WoS Export -> Plain Text File, set Record Content, and click Export."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        wait_timeout = int(wos_config.get("exportWaitTimeoutSec") or 90)
        wait = WebDriverWait(driver, wait_timeout)

        # Results page top bar: button id="export-trigger-btn".
        export_trigger = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#export-trigger-btn, button[id='export-trigger-btn']")))
        self._scroll_into_view(driver, export_trigger)
        export_trigger.click()

        # First menu: choose Plain Text File.
        format_label = str(wos_config.get("exportFormatLabel") or "Plain Text File")
        format_option = self._find_exact_visible_option_by_text(driver, format_label, timeout=wait_timeout)
        self._scroll_into_view(driver, format_option)
        format_option.click()

        # Second overlay: Export Records to Plain Text File.
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".window, app-export-out-details, #exportButton")))
        self._set_wos_export_range_if_present(driver, wos_config, timeout=wait_timeout)
        self._set_wos_export_record_content(driver, str(wos_config.get("exportRecordContent") or "Author, Title, Source, Abstract"), timeout=wait_timeout)

        export_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#exportButton")))
        self._scroll_into_view(driver, export_button)
        export_button.click()
        self.runner.log(f"WoS plain text export submitted | {index}/{total}; keyword={keyword_name}")
        self._wait_for_wos_download_quiet(wos_config, timeout=wait_timeout)

    def _set_wos_export_range_if_present(self, driver: Any, wos_config: dict[str, Any], timeout: int = 30) -> None:
        """Set export range to configured values when WoS shows range fields.

        WoS exports at most 1000 records each time. We default to 1..1000; if the
        overlay has no range controls, this safely does nothing.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        try:
            radio = driver.find_element(By.CSS_SELECTOR, "input[name='outputMethodType'][value='fromRange'], #radio3-input")
            if radio.is_displayed() or radio.is_enabled():
                try:
                    driver.execute_script("arguments[0].click();", radio)
                except Exception:
                    radio.click()
        except Exception:
            pass

        try:
            start_box = driver.find_element(By.CSS_SELECTOR, "input[name='markFrom'], input[aria-label='Input starting record range']")
            end_box = driver.find_element(By.CSS_SELECTOR, "input[name='markTo'], input[aria-label*='Input ending record range']")
        except Exception:
            return

        start_value = str(int(wos_config.get("exportRecordFrom") or 1))
        end_value = str(int(wos_config.get("exportRecordTo") or 1000))
        for box, value in ((start_box, start_value), (end_box, end_value)):
            try:
                self._scroll_into_view(driver, box)
                box.click()
                box.send_keys(Keys.CONTROL, "a")
                box.send_keys(Keys.BACKSPACE)
                box.send_keys(value)
                driver.execute_script(
                    """
                    const el = arguments[0];
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                    """,
                    box,
                )
                time.sleep(0.1)
            except Exception:
                continue

    def _set_wos_export_record_content(self, driver: Any, record_content: str, timeout: int = 30) -> None:
        """Set Record Content dropdown to Author, Title, Source, Abstract."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, timeout)
        wanted = clean_text(record_content)

        def locate_dropdown(_: Any) -> Any:
            buttons = driver.find_elements(By.CSS_SELECTOR, ".window wos-select button[role='combobox'], app-export-out-details wos-select button[role='combobox']")
            fallback = driver.find_elements(By.CSS_SELECTOR, "button[role='combobox'][aria-label*='Filter by']")
            for button in buttons + fallback:
                try:
                    if not button.is_displayed() or not button.is_enabled():
                        continue
                    label = clean_text(button.get_attribute("aria-label"))
                    text = clean_text(button.text)
                    if "Filter by" in label or "Author, Title" in text or "Author, Title" in label:
                        return button
                except Exception:
                    continue
            return False

        dropdown = wait.until(locate_dropdown)
        current = clean_text(dropdown.text) or clean_text(dropdown.get_attribute("aria-label"))
        if wanted.lower() in current.lower():
            return
        self._scroll_into_view(driver, dropdown)
        dropdown.click()
        option = self._find_exact_visible_option_by_text(driver, wanted, timeout=timeout)
        self._scroll_into_view(driver, option)
        option.click()
        wait.until(lambda _: wanted.lower() in (clean_text(dropdown.text) + " " + clean_text(dropdown.get_attribute("aria-label"))).lower())
        time.sleep(0.15)

    def _wait_for_wos_download_quiet(self, wos_config: dict[str, Any], timeout: int = 90) -> None:
        """Wait briefly for Chrome download to start/finish if a download directory is configured."""
        export_dir = self.runner.resolve_workspace_path(str(wos_config.get("downloadDir") or "data/wos_exports"))
        if export_dir is None:
            return
        export_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + timeout
        saw_partial = False
        last_txt_count = len(list(export_dir.glob("*.txt")))
        while time.time() < deadline:
            partials = list(export_dir.glob("*.crdownload")) + list(export_dir.glob("*.tmp"))
            txt_count = len(list(export_dir.glob("*.txt")))
            if partials:
                saw_partial = True
            if saw_partial and not partials:
                time.sleep(0.5)
                return
            if txt_count > last_txt_count and not partials:
                time.sleep(0.5)
                return
            time.sleep(0.5)
        self.runner.log(f"WARNING: WoS export download was not observed within {timeout}s; continuing to wait in main export watcher.")


    def run_weekly_from_crossref(self) -> None:
        provider = str(self.config.get("paperSearchProvider", "crossref")).lower()
        if provider != "crossref":
            raise RuntimeError(f"Unsupported Weekly paperSearchProvider: {provider}. Use crossref.")
        query_field = self.config.get("crossrefKeywordQueryField", "query.bibliographic")
        if query_field not in {"query", "query.bibliographic", "query.title"}:
            raise RuntimeError(f"Unsupported crossrefKeywordQueryField: {query_field}")
        self.runner.log(f"Agent selected tool: CrossrefSearch; queryField={query_field}; full text is not searched.")
        for spec in self.specs:
            for window_start, window_end in self.windows:
                node_key = build_node_key(spec["name"], window_start, window_end)
                if node_key in self.completed_node_set:
                    self.runner.log(f"Skipping completed node | journal={spec['name']} | window={window_start.isoformat()}..{window_end.isoformat()}")
                    continue
                current_node = {
                    "key": node_key,
                    "journal": spec["name"],
                    "windowStart": window_start.isoformat(),
                    "windowEnd": window_end.isoformat(),
                    "source": "crossref",
                }
                node_raw = self.search_crossref_node(spec, window_start, window_end, query_field)
                self.process_node(current_node, node_raw, "crossref")

    def search_crossref_node(self, spec: dict[str, Any], window_start: dt.date, window_end: dt.date, query_field: str) -> list[dict[str, Any]]:
        node_raw: list[dict[str, Any]] = []
        window_from = window_start.isoformat()
        window_until = window_end.isoformat()
        for keyword in self.config.get("keywords", []):
            keyword_name = search_query_name(keyword)
            keyword_query = search_query_text(keyword)
            if not keyword_query:
                continue
            if spec["issns"]:
                issn_filters = ",".join(f"issn:{issn}" for issn in spec["issns"])
                searches = [
                    {
                        "filter": f"from-pub-date:{window_from},until-pub-date:{window_until},type:journal-article,{issn_filters}",
                        "label": "issn:" + "|".join(spec["issns"]),
                        "local_check": False,
                    }
                ]
            else:
                searches = [
                    {
                        "filter": f"from-pub-date:{window_from},until-pub-date:{window_until},type:journal-article",
                        "label": "container-title",
                        "local_check": True,
                    }
                ]
            for search in searches:
                params = {"filter": search["filter"], "sort": "published", "order": "desc", query_field: keyword_query}
                if search["local_check"]:
                    params["query.container-title"] = spec["name"]
                items = self.runner.crossref_paged_items(params, int(self.config.get("maxRecentPerKeyword", 0)))
                kept = 0
                for item in items:
                    record = paper_record(f"keyword:{keyword_name};journal:{spec['name']}", item, self.today)
                    if search["local_check"] and not is_target_journal(record["Journal"], [spec["name"]]):
                        continue
                    if not record["DateObject"]:
                        continue
                    record_date = dt.date.fromisoformat(record["DateObject"][:10])
                    if record_date < window_start or record_date > window_end:
                        continue
                    node_raw.append(record)
                    kept += 1
                self.runner.log(
                    f"Crossref Search | keyword={keyword_name} | journal={spec['name']} | "
                    f"window={window_from}..{window_until} | filter={search['label']} | "
                    f"fetched={len(items)} | keptHardFiltered={kept} | keptNodeRaw={len(node_raw)}"
                )
        return node_raw

    def process_node(self, current_node: dict[str, Any], node_raw: list[dict[str, Any]], source_label: str) -> None:
        node_key = str(current_node["key"])
        save_build_state(self.runner, self.state_path, self.args.mode, self.completed_nodes, current_node, len(self.accepted), len(self.queue))
        self.raw_count += len(node_raw)
        node_deduped = merge_papers_by_key([], node_raw)
        self.all_candidates = merge_papers_by_key(self.all_candidates, node_deduped)
        self.runner.write_json(
            self.candidate_path,
            {
                "generatedAt": dt.datetime.now().astimezone().isoformat(),
                "mode": self.args.mode,
                "source": source_label,
                "fromDate": self.from_date,
                "untilDate": self.until_date,
                "rawCount": self.raw_count,
                "dedupedCount": len(self.all_candidates),
                "lastNode": current_node,
                "papers": self.all_candidates,
            },
        )
        self.runner.log(f"{source_label.upper()} node checkpoint saved: {self.candidate_path}; nodeDeduped={len(node_deduped)}; totalDeduped={len(self.all_candidates)}")

        known_keys = {
            str(item.get("Key") or item.get("key") or "")
            for item in self.queue + self.accepted + self.new_cache
            if int(item.get("ScreenSchemaVersion", 0) or 0) >= SCREEN_SCHEMA_VERSION
        }
        known_keys.discard("")
        to_screen = [paper for paper in node_deduped if paper["Key"] not in known_keys]
        total_batches = (len(to_screen) + self.batch_size - 1) // self.batch_size if self.batch_size > 0 else 0
        self.runner.log(f"Agent screening decision | node={node_key}; deduped={len(node_deduped)}; toScreen={len(to_screen)}; alreadyKnown={len(known_keys)}")
        for start in range(0, len(to_screen), self.batch_size):
            batch = to_screen[start : start + self.batch_size]
            if not batch:
                break
            self.batch_count += 1
            node_batch = start // self.batch_size + 1
            self.runner.log(f"Sending batch {self.batch_count} to MiniMax; nodeBatch={node_batch}/{total_batches}; papers={len(batch)}.")
            results = minimax_screen_batch_checked(self.runner, self.minimax, batch, self.config)
            by_key = {paper["Key"]: paper for paper in batch}
            accepted_in_batch = 0
            for result in results:
                paper = by_key.get(str(result.get("key", "")))
                if not paper:
                    continue
                record = queue_record(paper, result, self.config, self.today)
                is_accepted = bool(result.get("accept"))
                if is_accepted and record["ScoreBreakdown"]["LlmScore"] >= self.min_llm_score:
                    accepted_in_batch += 1
                    self.accepted.append(record)
                self.new_cache.append(
                    {
                        "Key": paper["Key"],
                        "Title": paper["Title"],
                        "DOI": paper["DOI"],
                        "Accepted": is_accepted,
                        "RecommendationScore": record["RecommendationScore"],
                        "LlmScore": record["LlmScreen"]["Score"],
                        "Tags": record["LlmScreen"]["Tags"],
                        "Comment": record["LlmScreen"]["Comment"],
                        "ScreenSchemaVersion": SCREEN_SCHEMA_VERSION,
                        "ScreenedAt": dt.datetime.now().astimezone().isoformat(),
                        "Mode": self.args.mode,
                        "Node": current_node,
                    }
                )
            self.runner.log(f"MiniMax batch accepted | nodeBatch={node_batch}/{total_batches}; accept/input={accepted_in_batch}/{len(batch)}")
            self.screened_count += len(batch)
            write_screening_checkpoint(
                self.runner,
                self.queue_path,
                self.cache_path,
                self.state_path,
                self.queue,
                self.accepted,
                self.new_cache,
                self.args.mode,
                node_batch,
                total_batches,
                min(start + len(batch), len(to_screen)),
                len(to_screen),
                self.completed_nodes,
                current_node,
            )
            self.runner.log(f"MiniMax checkpoint saved | node={node_key}; nodeBatch={node_batch}/{total_batches}; totalScreened={self.screened_count}.")
        if node_key not in self.completed_node_set:
            self.completed_nodes.append(node_key)
            self.completed_node_set.add(node_key)
        merged_queue = merge_queue(self.queue, self.accepted)
        save_build_state(self.runner, self.state_path, self.args.mode, self.completed_nodes, None, len(self.accepted), len(merged_queue))

    def resolve_target_journal_name(self, journal: str) -> str:
        for name in self.target_names:
            if is_target_journal(journal, [name]):
                return name
        return ""

    def window_for_date(self, value: dt.date) -> tuple[dt.date, dt.date] | None:
        for window_start, window_end in self.windows:
            if window_start <= value <= window_end:
                return window_start, window_end
        return None

    def finish(self) -> None:
        merged_queue = write_screening_checkpoint(
            self.runner,
            self.queue_path,
            self.cache_path,
            self.state_path,
            self.queue,
            self.accepted,
            self.new_cache,
            self.args.mode,
            self.batch_count,
            self.batch_count,
            self.screened_count,
            self.screened_count,
            self.completed_nodes,
            None,
        )
        self.runner.log("Current queue:")
        for item in merged_queue[:20]:
            self.runner.log(f"  QUEUE score={item.get('RecommendationScore')}; dirs={'; '.join(item.get('MatchedDirections', []))}; {format_paper_line(item)}")
        self.runner.log(
            f"PaperRadarAgent complete. mode={self.args.mode}; policy={self.source_policy}; raw={self.raw_count}; "
            f"deduped={len(self.all_candidates)}; screened={self.screened_count}; batches={self.batch_count}; "
            f"accepted={len(self.accepted)}; queue={len(merged_queue)}; log={self.runner.run_log_path or ''}"
        )


def run(args: argparse.Namespace) -> None:
    runner = Runner(args)
    config_path = runner.workspace / "automation" / "paper-watch.config.json"
    config = runner.read_json(config_path, {})
    network = config.get("network", {})
    if not runner.proxy_url and isinstance(network, dict):
        runner.proxy_url = network.get("proxyUrl", "")
        runner.opener = runner._build_opener()

    reports_dir = runner.workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    runner.run_log_path = reports_dir / f"paper_queue_build_{args.mode.lower()}.log"
    runner.run_log_path.write_text(f"Paper radar agent log started at {dt.datetime.now().astimezone().isoformat()}\n", encoding="utf-8")
    PaperRadarAgent(args, runner, config).run()


def format_paper_line(paper: dict[str, Any]) -> str:
    return f"{paper.get('Key')} | {paper.get('Date')} | {paper.get('Source')} | {paper.get('Journal')} | {paper.get('Title')}"


def parse_args() -> argparse.Namespace:
    root_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Run the paper radar agent.")
    parser.add_argument("--mode", choices=["Full", "Weekly", "FetchWoS"], default="Full")
    args = parser.parse_args()
    args.workspace_root = str(root_default)
    args.lookback_days = 0
    return args


if __name__ == "__main__":
    run(parse_args())

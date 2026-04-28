#!/usr/bin/env python3
"""MiniMax paper screening tool for the paper radar agent.

This module is intentionally independent from the WoS browser automation tool.
It accepts normalized paper records, sends them to MiniMax in batches, validates
that every input paper receives one result, and writes queue/cache checkpoints.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

LogFn = Callable[[str], None]
SCREEN_SCHEMA_VERSION = 4
USER_AGENT = "paper-radar-agent/1.0"


# ----------------------------- basic utilities -----------------------------

def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_journal_title(value: Any) -> str:
    text = clean_text(value).lower().replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_target_journal(journal: str, targets: list[str]) -> bool:
    journal_norm = normalize_journal_title(journal)
    return bool(journal_norm) and any(journal_norm == normalize_journal_title(target) for target in targets)


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def paper_key(paper: dict[str, Any]) -> str:
    doi = clean_text(paper.get("DOI"))
    if doi:
        return "doi:" + doi.lower()
    title = re.sub(r"[^a-z0-9]+", " ", clean_text(paper.get("Title")).lower()).strip()
    return "title:" + title


def paper_abstract(paper: dict[str, Any]) -> str:
    return clean_text(paper.get("Abstract") or paper.get("Summary"))


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


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
    return [
        {"name": search_query_name(item), "query": search_query_text(item)}
        for item in config.get("keywords", [])
        if search_query_text(item)
    ]


# ------------------------------- WoS parsing --------------------------------

def parse_wos_plain_text(path: Path, today: dt.date | None = None) -> list[dict[str, Any]]:
    """Parse Web of Science Plain Text File records into normalized papers."""
    today = today or dt.date.today()
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


def merge_papers_by_key(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for paper in existing + incoming:
        key = str(paper.get("Key") or "")
        if key and key not in merged:
            merged[key] = paper
    return list(merged.values())


# ------------------------------ MiniMax config ------------------------------

class MiniMaxConfig:
    api_key: str
    base_url: str = "https://api.minimaxi.com/v1"
    model: str = "MiniMax-M2.7"
    fallback_models: tuple[str, ...] = ()
    batch_size: int = 20
    timeout_sec: int = 120
    max_retries: int = 4
    retry_base_sec: float = 5.0

    @classmethod
    def from_file(cls, path: Path) -> "MiniMaxConfig":
        cfg = read_json(path, {})
        if not isinstance(cfg, dict):
            cfg = {}
        api_key = str(cfg.get("apiKey") or os.environ.get("MINIMAX_API_KEY") or "").strip()
        is_placeholder = api_key == "YOUR_MINIMAX_API_KEY" or (
            api_key.startswith("sk-") and set(api_key[6:]) <= {"x", "X", "*"}
        )
        if not api_key or is_placeholder:
            raise RuntimeError(f"Missing MiniMax API key. Set MINIMAX_API_KEY or create {path}.")
        return cls(
            api_key=api_key,
            base_url=str(cfg.get("baseUrl") or "https://api.minimaxi.com/v1"),
            model=str(cfg.get("model") or "MiniMax-M2.7"),
            fallback_models=tuple(str(m) for m in cfg.get("fallbackModels", []) if str(m).strip()),
            batch_size=int(cfg.get("batchSize") or 20),
            timeout_sec=int(cfg.get("timeoutSec") or 120),
            max_retries=int(cfg.get("maxRetries") or 4),
            retry_base_sec=float(cfg.get("retryBaseSec") or 5),
        )


def is_retryable_minimax_error(message: str) -> bool:
    lowered = message.lower()
    permanent_markers = ["not support model", "invalid api key", "unauthorized", "permission", "forbidden", "insufficient"]
    if any(marker in lowered for marker in permanent_markers):
        return False
    transient_markers = [
        "request拥挤", "请求拥挤", "too many requests", "rate limit", "timeout", "timed out",
        "temporarily", "server_error", "invalid minimax json", "expecting property name enclosed in double quotes",
        "expecting value", "unterminated string", "extra data", "http 429", "http 500", "http 502",
        "http 503", "http 504", "connection", "max retries",
    ]
    return any(marker in lowered for marker in transient_markers)


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


# ----------------------------- screening logic ------------------------------

class MiniMaxScreeningTool:
    """Agent-facing MiniMax scoring tool."""

    def __init__(self, config: MiniMaxConfig, log: LogFn = print) -> None:
        self.config = config
        self.log = log

    def request_json(self, method: str, url: str, body: Any | None = None, headers: dict[str, str] | None = None) -> Any:
        if requests is None:
            raise RuntimeError("The requests package is required for MiniMax screening.")
        request_headers = {"User-Agent": USER_AGENT}
        if headers:
            request_headers.update(headers)
        response = requests.request(
            method.upper(),
            url,
            json=body,
            headers=request_headers,
            timeout=self.config.timeout_sec,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code} for {url}: {response.text}")
        return response.json()

    def screen_batch(self, batch: list[dict[str, Any]], paper_watch_config: dict[str, Any]) -> list[dict[str, Any]]:
        max_retries = self.config.max_retries
        for attempt in range(1, max_retries + 2):
            response = self._screen_batch_once(batch, paper_watch_config)
            raw_results = response.get("results", []) if isinstance(response, dict) else []
            raw_count = len(raw_results) if isinstance(raw_results, list) else 0
            self.log(f"MiniMax response count | attempt={attempt}; input={len(batch)}; output={raw_count}")
            try:
                results = validate_minimax_results(response, batch)
                self.log(f"MiniMax response validated | attempt={attempt}; input={len(batch)}; output={len(results)}")
                return results
            except Exception as exc:
                if attempt > max_retries:
                    raise
                wait_sec = min(60.0, self.config.retry_base_sec * (2 ** (attempt - 1)))
                self.log(f"WARNING: MiniMax response validation failed; attempt={attempt}; retryInSec={wait_sec:g}; {exc}")
                time.sleep(wait_sec)
        raise RuntimeError("MiniMax response validation failed after retries.")

    def _screen_batch_once(self, papers: list[dict[str, Any]], paper_watch_config: dict[str, Any]) -> dict[str, Any]:
        payload_papers = [
            {"key": clean_text(p.get("Key")), "title": clean_text(p.get("Title")), "abstract": paper_abstract(p)}
            for p in papers
        ]
        system = (
            "You are an academic paper screening assistant for a PhD literature radar. "
            "You must evaluate papers only from the supplied title and abstract. "
            "Return valid strict JSON only. No markdown, no explanations, no extra text."
        )
        user = self._build_user_prompt(payload_papers)
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        models = [self.config.model] + [m for m in self.config.fallback_models if m != self.config.model]
        errors: list[str] = []
        for model in models:
            body = {
                "model": model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            for attempt in range(1, self.config.max_retries + 2):
                try:
                    response = self.request_json(
                        "POST",
                        url,
                        body=body,
                        headers={"Authorization": f"Bearer {self.config.api_key}"},
                    )
                    content = str(response["choices"][0]["message"]["content"])
                    self.log(f"MiniMax model used: {model}; attempt={attempt}")
                    return parse_model_json(content)
                except Exception as exc:
                    message = str(exc)
                    if not is_retryable_minimax_error(message) or attempt > self.config.max_retries:
                        errors.append(f"{model}: {message}")
                        self.log(f"WARNING: MiniMax model failed: {model}; attempt={attempt}; {message}")
                        break
                    wait_sec = min(60.0, self.config.retry_base_sec * (2 ** (attempt - 1)))
                    self.log(f"WARNING: MiniMax transient failure: {model}; attempt={attempt}; retryInSec={wait_sec:g}; {message}")
                    time.sleep(wait_sec)
        raise RuntimeError("All MiniMax models failed: " + " | ".join(errors))

    def _build_user_prompt(self, payload_papers: list[dict[str, Any]]) -> str:
        return f"""Task:
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


def source_direction(source: str) -> str:
    match = re.search(r"keyword:([^;]+)", source or "")
    return match.group(1) if match else ""


def journal_quality_score(journal: str, top_journals: list[str]) -> int:
    normalized = normalize_journal_title(journal)
    for top in top_journals:
        if normalized == normalize_journal_title(top):
            return 5 if normalized in {"nature", "science"} else 4
    return 0


def queue_record(paper: dict[str, Any], screen: dict[str, Any], paper_watch_config: dict[str, Any]) -> dict[str, Any]:
    today = dt.date.today()
    llm_score = as_int(screen.get("score", screen.get("topicFit", 0)))
    journal = journal_quality_score(str(paper.get("Journal", "")), paper_watch_config.get("topJournals", []))
    total = llm_score + journal
    direction = str(screen.get("primaryDirection") or "")
    if not direction:
        direction = source_direction(str(paper.get("Source", "")))
    raw_directions = screen.get("directions", [])
    matched_directions = [clean_text(v) for v in raw_directions if clean_text(v)] if isinstance(raw_directions, list) else []
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
        "ScoreBreakdown": {"LlmScore": llm_score, "JournalQuality": journal, "Total": total},
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
            or (paper_schema == previous_schema and int(paper.get("RecommendationScore", 0)) > int(previous.get("RecommendationScore", 0)))
        ):
            merged[key] = paper
    return sorted(
        merged.values(),
        key=lambda paper: (int(paper.get("RecommendationScore", 0)), str(paper.get("DateObject") or "")),
        reverse=True,
    )


def screen_papers_to_queue(
    papers: list[dict[str, Any]],
    workspace: str | Path,
    paper_watch_config: dict[str, Any] | None = None,
    log: LogFn = print,
) -> list[dict[str, Any]]:
    """Screen normalized paper records and update the project candidate queue."""
    workspace_path = Path(workspace).resolve()
    if paper_watch_config is None:
        paper_watch_config = read_json(workspace_path / "automation" / "paper-watch.config.json", {})
        if not isinstance(paper_watch_config, dict):
            paper_watch_config = {}

    screening = paper_watch_config.get("llmScreening", {}) if isinstance(paper_watch_config.get("llmScreening", {}), dict) else {}
    minimax_path = workspace_path / str(screening.get("configPath") or "automation/minimax.config.json")
    cache_path = workspace_path / str(screening.get("cachePath") or "data/paper-screening-cache.json")
    queue_path = workspace_path / "data" / "paper-candidate-queue.json"
    candidate_path = workspace_path / "data" / "paper-wos-candidates.json"
    state_path = workspace_path / "data" / "paper-queue-build-state.json"

    config = MiniMaxConfig.from_file(minimax_path)
    tool = MiniMaxScreeningTool(config, log=log)
    min_llm_score = int(screening.get("minLlmScore", 6))

    queue = read_json(queue_path, [])
    if not isinstance(queue, list):
        queue = []
    cache_data = read_json(cache_path, {"papers": []})
    cache_items = cache_data.get("papers", []) if isinstance(cache_data, dict) else []
    if not isinstance(cache_items, list):
        cache_items = []

    known_keys = {
        str(item.get("Key") or item.get("key") or "")
        for item in queue + cache_items
        if int(item.get("ScreenSchemaVersion", 0) or 0) >= SCREEN_SCHEMA_VERSION
    }
    known_keys.discard("")
    to_screen = [paper for paper in papers if paper.get("Key") and paper["Key"] not in known_keys]
    log(f"MiniMax screening start | papers={len(papers)}; toScreen={len(to_screen)}; alreadyKnown={len(known_keys)}")

    accepted: list[dict[str, Any]] = []
    processed = 0
    total_batches = (len(to_screen) + config.batch_size - 1) // config.batch_size if config.batch_size > 0 else 0

    for start in range(0, len(to_screen), config.batch_size):
        batch = to_screen[start : start + config.batch_size]
        if not batch:
            break
        batch_no = start // config.batch_size + 1
        log(f"Sending batch to MiniMax | batch={batch_no}/{total_batches}; papers={len(batch)}")
        results = tool.screen_batch(batch, paper_watch_config)
        by_key = {paper["Key"]: paper for paper in batch}
        accepted_in_batch = 0
        for result in results:
            paper = by_key.get(str(result.get("key", "")))
            if not paper:
                continue
            record = queue_record(paper, result, paper_watch_config)
            is_accepted = bool(result.get("accept"))
            if is_accepted and record["ScoreBreakdown"]["LlmScore"] >= min_llm_score:
                accepted_in_batch += 1
                accepted.append(record)
            cache_items.append(
                {
                    "Key": paper["Key"],
                    "Title": paper["Title"],
                    "DOI": paper.get("DOI"),
                    "Accepted": is_accepted,
                    "RecommendationScore": record["RecommendationScore"],
                    "LlmScore": record["LlmScreen"]["Score"],
                    "Tags": record["LlmScreen"]["Tags"],
                    "Comment": record["LlmScreen"]["Comment"],
                    "ScreenSchemaVersion": SCREEN_SCHEMA_VERSION,
                    "ScreenedAt": dt.datetime.now().astimezone().isoformat(),
                    "Mode": "WoSTool",
                }
            )
        processed += len(batch)
        merged_queue = merge_queue(queue, accepted)
        write_json(queue_path, merged_queue)
        write_json(cache_path, {"generatedAt": dt.datetime.now().astimezone().isoformat(), "papers": cache_items})
        write_json(
            state_path,
            {
                "generatedAt": dt.datetime.now().astimezone().isoformat(),
                "mode": "WoSToolMiniMax",
                "screenSchemaVersion": SCREEN_SCHEMA_VERSION,
                "processedCount": processed,
                "totalToScreen": len(to_screen),
                "acceptedCount": len(accepted),
                "queueSize": len(merged_queue),
                "queuePath": str(queue_path),
                "cachePath": str(cache_path),
            },
        )
        log(f"MiniMax batch accepted | batch={batch_no}/{total_batches}; accept/input={accepted_in_batch}/{len(batch)}")

    merged_queue = merge_queue(queue, accepted)
    write_json(queue_path, merged_queue)
    write_json(candidate_path, {"generatedAt": dt.datetime.now().astimezone().isoformat(), "source": "wos_export", "papers": papers})
    log(f"MiniMax screening complete | screened={len(to_screen)}; accepted={len(accepted)}; queue={len(merged_queue)}")
    return merged_queue


def parse_wos_exports_and_screen(
    export_files: list[str | Path],
    workspace: str | Path,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    paper_watch_config: dict[str, Any] | None = None,
    log: LogFn = print,
) -> list[dict[str, Any]]:
    """Parse WoS txt files, hard-filter by journal/date, dedupe, and send to MiniMax."""
    workspace_path = Path(workspace).resolve()
    if paper_watch_config is None:
        paper_watch_config = read_json(workspace_path / "automation" / "paper-watch.config.json", {})
        if not isinstance(paper_watch_config, dict):
            paper_watch_config = {}
    targets = [str(v) for v in paper_watch_config.get("topJournals", [])]
    today = dt.date.today()
    start_date = start_date or dt.date(2023, 1, 1)
    end_date = end_date or today

    imported = 0
    kept: list[dict[str, Any]] = []
    for file in export_files:
        path = Path(file)
        records = parse_wos_plain_text(path, today=today)
        imported += len(records)
        for record in records:
            if targets and not is_target_journal(str(record.get("Journal", "")), targets):
                continue
            date_value = str(record.get("DateObject") or "")
            if not date_value:
                continue
            try:
                record_date = dt.date.fromisoformat(date_value[:10])
            except Exception:
                continue
            if start_date <= record_date <= end_date:
                kept.append(record)
    deduped = merge_papers_by_key([], kept)
    log(f"WoS parse complete | files={len(export_files)}; imported={imported}; kept={len(kept)}; deduped={len(deduped)}")
    return screen_papers_to_queue(deduped, workspace_path, paper_watch_config=paper_watch_config, log=log)


TOOL_SPEC = {
    "name": "screen_papers_with_minimax",
    "description": "Send normalized paper records to MiniMax in batches, validate JSON results, and update the local candidate queue/cache.",
    "inputs": {
        "papers": "List of normalized paper records with Key, Title, Journal, DateObject, DOI, Abstract.",
        "workspace": "Project root containing automation/minimax.config.json and automation/paper-watch.config.json.",
    },
    "output": "Updated paper candidate queue list sorted by recommendation score.",
}

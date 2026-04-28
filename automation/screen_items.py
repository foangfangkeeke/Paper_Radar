#!/usr/bin/env python3
"""
Screen merged WoS items with MiniMax and maintain two queues.

Input:
  data/wos_minimax_items.json
    Either a plain list: [{key, TI, SO, AB}, ...]
    or a wrapped object: {"papers": [...], "stats": {...}}

Config:
  automation/minimax.config.json
    apiKey, baseUrl, model, batchSize, timeoutSec, maxRetries, retryBaseSec
  automation/paper-watch.config.json
    keywords/topJournals and optional llmScreening.minLlmScore

Output:
  data/paper_base_queue.json   all screened papers, including rejected
  data/paper_push_queue.json   accepted papers worth pushing

Compatibility output also written:
  data/paper-candidate-queue.json
  data/paper-screening-cache.json
  data/paper-queue-build-state.json
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
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

USER_AGENT = "paper-radar-agent/1.0 (mailto:zb2557604@buaa.edu.cn)"
SCREEN_SCHEMA_VERSION = 4


class Runner:
    def __init__(self, workspace: Path, log_name: str = "screen_items") -> None:
        self.workspace = workspace.resolve()
        self.proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
        self.opener = self._build_opener()
        reports_dir = self.workspace / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        self.run_log_path = reports_dir / f"{log_name}.log"
        self.run_log_path.write_text(
            f"MiniMax screening log started at {dt.datetime.now().astimezone().isoformat()}\n",
            encoding="utf-8",
        )

    def _build_opener(self):
        if not self.proxy_url:
            return build_opener()
        return build_opener(ProxyHandler({"http": self.proxy_url, "https": self.proxy_url}))

    def log(self, message: str) -> None:
        line = f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        print(line)
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

    def resolve_path(self, value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value)
        return path if path.is_absolute() else self.workspace / path

    def request_json(self, method: str, url: str, body: Any | None = None, headers: dict[str, str] | None = None, timeout: int = 120) -> Any:
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


def load_minimax_config(runner: Runner, path: Path) -> dict[str, Any]:
    cfg = runner.read_json(path, {})
    if not isinstance(cfg, dict):
        raise RuntimeError(f"MiniMax config must be a JSON object: {path}")
    api_key = str(cfg.get("apiKey") or os.environ.get("MINIMAX_API_KEY") or "").strip()
    is_placeholder = api_key == "YOUR_MINIMAX_API_KEY" or (api_key.startswith("sk-") and set(api_key[6:]) <= {"x", "X", "*"})
    if not api_key or is_placeholder:
        raise RuntimeError(f"Missing MiniMax API key. Set apiKey in {path} or MINIMAX_API_KEY.")
    return {
        "apiKey": api_key,
        "baseUrl": str(cfg.get("baseUrl") or "https://api.minimaxi.com/v1"),
        "model": str(cfg.get("model") or "MiniMax-M2.7"),
        "fallbackModels": [str(model) for model in cfg.get("fallbackModels", []) if str(model).strip()] if isinstance(cfg.get("fallbackModels", []), list) else [],
        "batchSize": int(cfg.get("batchSize", 20)),
        "timeoutSec": int(cfg.get("timeoutSec", 120)),
        "maxRetries": int(cfg.get("maxRetries", 4)),
        "retryBaseSec": float(cfg.get("retryBaseSec", 5)),
    }


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


def normalize_input_item(item: dict[str, Any]) -> dict[str, Any] | None:
    key = clean_text(item.get("key") or item.get("Key"))
    title = clean_text(item.get("TI") or item.get("Title") or item.get("title"))
    journal = clean_text(item.get("SO") or item.get("Journal") or item.get("journal"))
    abstract = clean_text(item.get("AB") or item.get("Abstract") or item.get("abstract"))
    if not title or not journal:
        return None
    if not key:
        key = "title:" + re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return {
        "key": key,
        "TI": title,
        "SO": journal,
        "AB": abstract,
    }


def load_input_items(runner: Runner, input_path: Path) -> list[dict[str, Any]]:
    payload = runner.read_json(input_path, None)
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        # Compatibility with the earlier merge script that wrote {stats, papers}.
        candidates = payload.get("papers") or payload.get("items") or payload.get("results")
        if isinstance(candidates, list):
            raw_items = candidates
        else:
            raise RuntimeError(f"Input object must contain a list field named papers/items/results: {input_path}")
    else:
        raise RuntimeError(f"Input must be a list or an object with papers/items/results: {input_path}")

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped = 0
    for raw in raw_items:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        item = normalize_input_item(raw)
        if item is None:
            skipped += 1
            continue
        if item["key"] in seen:
            continue
        seen.add(item["key"])
        items.append(item)
    if skipped:
        runner.log(f"WARNING: skipped invalid input items: {skipped}")
    return items


def minimax_screen_batch(runner: Runner, minimax: dict[str, Any], batch: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    # Keep the prompt, scoring standard, tag rules, and output schema identical to paper_queue_build.py.
    # Only field mapping changes: key/TI/AB -> key/title/abstract for the MiniMax payload.
    directions = research_directions(config)
    payload_papers = [
        {
            "key": clean_text(paper.get("key")),
            "title": clean_text(paper.get("TI")),
            "abstract": clean_text(paper.get("AB")),
        }
        for paper in batch
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


def validate_minimax_results(screen_response: dict[str, Any], batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = screen_response.get("results", [])
    if not isinstance(results, list):
        raise RuntimeError("MiniMax response is missing a list field: results.")
    input_keys = {str(paper["key"]) for paper in batch}
    result_keys: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            raise RuntimeError("MiniMax response contains a non-object result item.")
        key = str(result.get("key", ""))
        if key:
            result_keys.add(key)
        normalized.append(result)
    missing = [key for key in input_keys if key not in result_keys]
    extra = [key for key in result_keys if key not in input_keys]
    if missing:
        raise RuntimeError(
            f"MiniMax response count mismatch: input={len(batch)}, output={len(normalized)}, "
            f"missing={len(missing)}, extra={len(extra)}; missingKeys={', '.join(missing[:5])}"
        )
    return normalized


def normalize_tags(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, Any] = {}
    for key, raw in value.items():
        name = clean_text(key)
        if not name:
            continue
        if isinstance(raw, list):
            values = [clean_text(item) for item in raw if clean_text(item)]
            if values:
                output[name] = values
        else:
            text = clean_text(raw)
            if text:
                output[name] = text
    return output


def build_screened_record(item: dict[str, Any], result: dict[str, Any], min_push_score: int) -> dict[str, Any]:
    score = int(result.get("score", result.get("topicFit", 0)) or 0)
    accepted = bool(result.get("accept"))
    in_push_queue = accepted and score >= min_push_score
    directions_raw = result.get("directions", [])
    directions = [clean_text(x) for x in directions_raw if clean_text(x)] if isinstance(directions_raw, list) else []
    primary_direction = clean_text(result.get("primaryDirection") or (directions[0] if directions else ""))
    now = dt.datetime.now().astimezone().isoformat()
    return {
        "key": item["key"],
        "TI": item["TI"],
        "SO": item["SO"],
        "AB": item.get("AB", ""),
        "score": score,
        "accepted": accepted,
        "primaryDirection": primary_direction,
        "directions": directions,
        "tags": normalize_tags(result.get("tags")),
        "comment": clean_text(result.get("comment") or result.get("reason") or ""),
        "status": "pending" if in_push_queue else "rejected",
        "in_push_queue": in_push_queue,
        "screened_at": now,
        "pushed_at": None,
        "feedback": None,
        "source": "wos",
    }


def to_legacy_queue_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "Key": record["key"],
        "Source": "wos_merged",
        "Title": record["TI"],
        "Journal": record["SO"],
        "Date": "Unknown",
        "DateObject": None,
        "DOI": record["key"][4:] if str(record["key"]).startswith("doi:") else "",
        "Url": f"https://doi.org/{record['key'][4:]}" if str(record["key"]).startswith("doi:") else "",
        "Abstract": record.get("AB", ""),
        "CodeUrl": None,
        "GitHubStars": 0,
        "MatchedDirections": record.get("directions", []),
        "RecommendationScore": int(record.get("score", 0)),
        "ScoreBreakdown": {
            "LlmScore": int(record.get("score", 0)),
            "JournalQuality": 0,
            "Total": int(record.get("score", 0)),
        },
        "LlmScreen": {
            "Accepted": bool(record.get("accepted")),
            "Score": int(record.get("score", 0)),
            "PrimaryDirection": record.get("primaryDirection", ""),
            "Directions": record.get("directions", []),
            "Tags": record.get("tags", {}),
            "Comment": record.get("comment", ""),
            "Keywords": record.get("tags", {}).get("Keywords", []) if isinstance(record.get("tags"), dict) else [],
        },
        "ScreenSchemaVersion": SCREEN_SCHEMA_VERSION,
        "QueuedAt": record.get("screened_at"),
    }


def merge_by_key(existing: list[dict[str, Any]], incoming: list[dict[str, Any]], key_name: str = "key") -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in existing + incoming:
        key = str(item.get(key_name) or item.get("Key") or "")
        if not key:
            continue
        merged[key] = item
    return list(merged.values())


def save_outputs(
    runner: Runner,
    base_path: Path,
    push_path: Path,
    legacy_queue_path: Path,
    legacy_cache_path: Path,
    state_path: Path,
    base_queue: list[dict[str, Any]],
    push_queue: list[dict[str, Any]],
    processed_count: int,
    total_to_screen: int,
    write_legacy: bool = True,
) -> None:
    base_queue_sorted = sorted(base_queue, key=lambda x: (str(x.get("screened_at") or ""), str(x.get("key") or "")), reverse=True)
    push_queue_sorted = sorted(push_queue, key=lambda x: (int(x.get("score", 0)), str(x.get("screened_at") or "")), reverse=True)
    runner.write_json(base_path, base_queue_sorted)
    runner.write_json(push_path, push_queue_sorted)

    if write_legacy:
        legacy_queue = [to_legacy_queue_record(item) for item in push_queue_sorted]
        runner.write_json(legacy_queue_path, legacy_queue)
        runner.write_json(
            legacy_cache_path,
            {
                "generatedAt": dt.datetime.now().astimezone().isoformat(),
                "papers": [
                    {
                        "Key": item["key"],
                        "Title": item["TI"],
                        "Journal": item["SO"],
                        "Accepted": bool(item.get("accepted")),
                        "RecommendationScore": int(item.get("score", 0)),
                        "LlmScore": int(item.get("score", 0)),
                        "Tags": item.get("tags", {}),
                        "Comment": item.get("comment", ""),
                        "ScreenSchemaVersion": SCREEN_SCHEMA_VERSION,
                        "ScreenedAt": item.get("screened_at"),
                        "Mode": "ScreenItems",
                        "Source": "wos_merged",
                    }
                    for item in base_queue_sorted
                ],
            },
        )
    runner.write_json(
        state_path,
        {
            "generatedAt": dt.datetime.now().astimezone().isoformat(),
            "mode": "ScreenItems",
            "screenSchemaVersion": SCREEN_SCHEMA_VERSION,
            "processedCount": processed_count,
            "totalToScreen": total_to_screen,
            "baseQueueSize": len(base_queue_sorted),
            "pushQueueSize": len(push_queue_sorted),
            "baseQueuePath": str(base_path),
            "pushQueuePath": str(push_path),
        },
    )


def screen_items(
    workspace: Path,
    input_path: Path,
    minimax_path: Path,
    watch_config_path: Path,
    base_path: Path,
    push_path: Path,
    legacy_queue_path: Path,
    legacy_cache_path: Path,
    state_path: Path,
    min_push_score: int | None,
    limit: int,
    dry_run: bool,
    rebuild_push: bool = False,
    write_legacy: bool = True,
) -> None:
    runner = Runner(workspace)
    config = runner.read_json(watch_config_path, {})
    if not isinstance(config, dict):
        config = {}
    minimax = load_minimax_config(runner, minimax_path)
    if min_push_score is None:
        min_push_score = int(config.get("llmScreening", {}).get("minLlmScore", 6) if isinstance(config.get("llmScreening"), dict) else 6)

    items = load_input_items(runner, input_path)
    if limit and limit > 0:
        items = items[:limit]

    base_queue = runner.read_json(base_path, [])
    if not isinstance(base_queue, list):
        base_queue = []
    push_queue = runner.read_json(push_path, [])
    if not isinstance(push_queue, list):
        push_queue = []

    if rebuild_push:
        rebuilt_push = [
            dict(item, in_push_queue=True, status=(item.get("status") if item.get("status") in {"pending", "pushed", "skipped", "archived"} else "pending"))
            for item in base_queue
            if bool(item.get("accepted")) and int(item.get("score", 0) or 0) >= int(min_push_score)
        ]
        save_outputs(
            runner,
            base_path,
            push_path,
            legacy_queue_path,
            legacy_cache_path,
            state_path,
            base_queue,
            rebuilt_push,
            processed_count=0,
            total_to_screen=0,
            write_legacy=write_legacy,
        )
        runner.log(
            f"Push queue rebuilt | baseQueue={len(base_queue)}; pushQueue={len(rebuilt_push)}; "
            f"minPushScore={min_push_score}; legacy={write_legacy}"
        )
        return

    screened_keys = {str(item.get("key") or item.get("Key") or "") for item in base_queue}
    screened_keys.discard("")
    to_screen = [item for item in items if item["key"] not in screened_keys]

    runner.log(
        f"ScreenItems start | input={len(items)}; alreadyScreened={len(screened_keys)}; "
        f"toScreen={len(to_screen)}; batchSize={minimax['batchSize']}; minPushScore={min_push_score}"
    )

    if dry_run:
        runner.log("Dry run only. First items to screen:")
        for item in to_screen[:20]:
            runner.log(f"  {item['key']} | {item['SO']} | {item['TI']}")
        return

    batch_size = max(1, int(minimax["batchSize"]))
    processed = 0
    total_batches = (len(to_screen) + batch_size - 1) // batch_size if to_screen else 0

    for start in range(0, len(to_screen), batch_size):
        batch = to_screen[start : start + batch_size]
        batch_no = start // batch_size + 1
        runner.log(f"Sending batch to MiniMax | batch={batch_no}/{total_batches}; papers={len(batch)}")
        response = minimax_screen_batch(runner, minimax, batch, config)
        results = validate_minimax_results(response, batch)
        by_key = {item["key"]: item for item in batch}
        new_base: list[dict[str, Any]] = []
        new_push: list[dict[str, Any]] = []
        for result in results:
            key = str(result.get("key", ""))
            item = by_key.get(key)
            if item is None:
                continue
            record = build_screened_record(item, result, int(min_push_score))
            new_base.append(record)
            if record["in_push_queue"]:
                new_push.append(record)

        base_queue = merge_by_key(base_queue, new_base, key_name="key")
        push_queue = merge_by_key(push_queue, new_push, key_name="key")
        processed += len(batch)
        save_outputs(
            runner,
            base_path,
            push_path,
            legacy_queue_path,
            legacy_cache_path,
            state_path,
            base_queue,
            push_queue,
            processed,
            len(to_screen),
            write_legacy=write_legacy,
        )
        runner.log(
            f"Batch saved | batch={batch_no}/{total_batches}; acceptedForPush={len(new_push)}/{len(batch)}; "
            f"baseQueue={len(base_queue)}; pushQueue={len(push_queue)}"
        )

    runner.log(f"ScreenItems complete | processed={processed}; baseQueue={len(base_queue)}; pushQueue={len(push_queue)}")


def resolve(workspace: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else workspace / path


def main() -> None:
    root_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Screen merged WoS items with MiniMax and update base/push queues.")
    parser.add_argument("--workspace", default=str(root_default))
    parser.add_argument("--input", default="data/wos_minimax_items.json")
    parser.add_argument("--minimax-config", default="automation/minimax.config.json")
    parser.add_argument("--watch-config", default="automation/paper-watch.config.json")
    parser.add_argument("--base-queue", default="data/paper_base_queue.json")
    parser.add_argument("--push-queue", default="data/paper_push_queue.json")
    parser.add_argument("--legacy-candidate-queue", default="data/paper-candidate-queue.json")
    parser.add_argument("--legacy-cache", default="data/paper-screening-cache.json")
    parser.add_argument("--state", default="data/paper-queue-build-state.json")
    parser.add_argument("--min-push-score", type=int, default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rebuild-push", action="store_true", help="Rebuild paper_push_queue.json from paper_base_queue.json without calling MiniMax.")
    parser.add_argument("--no-legacy", action="store_true", help="Do not write old compatibility files paper-candidate-queue/cache/state outputs.")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    screen_items(
        workspace=workspace,
        input_path=resolve(workspace, args.input),
        minimax_path=resolve(workspace, args.minimax_config),
        watch_config_path=resolve(workspace, args.watch_config),
        base_path=resolve(workspace, args.base_queue),
        push_path=resolve(workspace, args.push_queue),
        legacy_queue_path=resolve(workspace, args.legacy_candidate_queue),
        legacy_cache_path=resolve(workspace, args.legacy_cache),
        state_path=resolve(workspace, args.state),
        min_push_score=args.min_push_score,
        limit=args.limit,
        dry_run=bool(args.dry_run),
        rebuild_push=bool(args.rebuild_push),
        write_legacy=not bool(args.no_legacy),
    )


if __name__ == "__main__":
    main()

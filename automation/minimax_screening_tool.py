#!/usr/bin/env python3
"""MiniMax screening tool for WoS paper radar.

Single responsibility:
1. parse/normalize paper records,
2. call MiniMax once per batch,
3. validate the returned JSON,
4. write base/push queues and compatibility outputs.
"""

from __future__ import annotations

import argparse
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
SCREEN_SCHEMA_VERSION = 5
USER_AGENT = "paper-radar-agent/1.0"


# ----------------------------- basic utilities -----------------------------

def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def normalize_journal_title(value: Any) -> str:
    text = clean_text(value).lower().replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def config_journal_names(config: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in config.get("topJournals", []):
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("title") or "")
        else:
            name = ""
        if name.strip():
            names.append(name.strip())
    return names


def is_target_journal(journal: str, targets: list[str]) -> bool:
    journal_norm = normalize_journal_title(journal)
    return bool(journal_norm) and any(journal_norm == normalize_journal_title(target) for target in targets)


def paper_key_from_parts(title: str, doi: str = "") -> str:
    doi = clean_text(doi).lower()
    if doi:
        return "doi:" + doi
    return title_key_from_title(title)


def title_key_from_title(title: str) -> str:
    normalized_title = re.sub(r"[^a-z0-9]+", " ", clean_text(title).lower()).strip()
    return "title:" + normalized_title if normalized_title else ""


def doi_key_from_doi(doi: str) -> str:
    doi = clean_text(doi).lower()
    return "doi:" + doi if doi else ""


def paper_key(paper: dict[str, Any]) -> str:
    return paper_key_from_parts(clean_text(paper.get("Title")), clean_text(paper.get("DOI")))


def paper_abstract(paper: dict[str, Any]) -> str:
    return clean_text(paper.get("Abstract") or paper.get("AB") or paper.get("Summary"))


def merge_by_key(items: list[dict[str, Any]], key_names: tuple[str, ...] = ("Key", "key")) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in items:
        key = ""
        for name in key_names:
            key = clean_text(item.get(name))
            if key:
                break
        if key:
            merged[key] = item
    return list(merged.values())


def merge_by_identity(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    aliases: dict[str, str] = {}
    for item in items:
        identities = paper_identities(item)
        canonical = ""
        for identity in identities:
            canonical = aliases.get(identity, "")
            if canonical:
                break
        if not canonical:
            canonical = identities[0] if identities else clean_text(item.get("Key") or item.get("key"))
        if not canonical:
            continue

        existing = merged.get(canonical)
        merged[canonical] = prefer_richer_paper(existing, item)
        for identity in identities:
            aliases[identity] = canonical
    return list(merged.values())


def paper_identities(item: dict[str, Any]) -> list[str]:
    title = clean_text(item.get("Title") or item.get("title") or item.get("TI"))
    doi = clean_text(item.get("DOI") or item.get("doi") or item.get("DI"))
    explicit_key = clean_text(item.get("Key") or item.get("key"))
    title_key = clean_text(item.get("TitleKey")) or title_key_from_title(title)
    doi_key = clean_text(item.get("DoiKey")) or doi_key_from_doi(doi)
    identities = [value for value in (doi_key, title_key, explicit_key) if value]
    return list(dict.fromkeys(identities))


def prefer_richer_paper(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if existing is None:
        return incoming
    existing_score = int(bool(clean_text(existing.get("DOI")) or clean_text(existing.get("DoiKey")))) + int(bool(paper_abstract(existing)))
    incoming_score = int(bool(clean_text(incoming.get("DOI")) or clean_text(incoming.get("DoiKey")))) + int(bool(paper_abstract(incoming)))
    return incoming if incoming_score > existing_score else existing


# ------------------------------- paper parsing ------------------------------

def normalize_paper_record(item: dict[str, Any], source: str = "input") -> dict[str, Any] | None:
    title = clean_text(item.get("Title") or item.get("title") or item.get("TI"))
    journal = clean_text(item.get("Journal") or item.get("journal") or item.get("SO"))
    abstract = clean_text(item.get("Abstract") or item.get("abstract") or item.get("AB"))
    doi = clean_text(item.get("DOI") or item.get("doi"))
    key = clean_text(item.get("Key") or item.get("key")) or paper_key_from_parts(title, doi)
    if not title or not journal:
        return None

    date_object = clean_text(item.get("DateObject") or item.get("dateObject") or item.get("Date") or item.get("date"))
    date_text = clean_text(item.get("Date") or item.get("date") or date_object) or "Unknown"
    url = clean_text(item.get("Url") or item.get("url")) or (f"https://doi.org/{doi}" if doi else "")

    return {
        "Key": key,
        "DoiKey": clean_text(item.get("DoiKey")) or doi_key_from_doi(doi),
        "TitleKey": clean_text(item.get("TitleKey")) or title_key_from_title(title),
        "Source": clean_text(item.get("Source") or item.get("source") or source),
        "Title": title,
        "Journal": journal,
        "Date": date_text,
        "DateObject": date_object or None,
        "DOI": doi,
        "Url": url,
        "Abstract": abstract,
        "CodeUrl": item.get("CodeUrl"),
        "GitHubStars": as_int(item.get("GitHubStars"), 0),
        "WosAccessionNumber": clean_text(item.get("WosAccessionNumber") or item.get("UT")),
    }


def load_normalized_papers(input_path: Path, source: str = "input") -> list[dict[str, Any]]:
    payload = read_json(input_path, None)
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("papers"), list):
        raw_items = payload["papers"]
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        raw_items = payload["items"]
    elif isinstance(payload, dict) and isinstance(payload.get("results"), list):
        raw_items = payload["results"]
    else:
        raise RuntimeError(f"Input must be a list or an object with papers/items/results: {input_path}")

    papers: list[dict[str, Any]] = []
    for raw in raw_items:
        if isinstance(raw, dict):
            paper = normalize_paper_record(raw, source=source)
            if paper:
                papers.append(paper)
    return merge_by_key(papers)


def parse_wos_plain_text(path: Path, today: dt.date | None = None) -> list[dict[str, Any]]:
    """Parse Web of Science Plain Text exports into normalized paper records."""
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
            value = line[3:].strip()
            current_tag = tag
            current[tag] = clean_text(f"{current.get(tag, '')} {value}" if tag in current else value)
        elif current_tag and line.startswith(" "):
            current[current_tag] = clean_text(f"{current.get(current_tag, '')} {line.strip()}")

    if current:
        raw_records.append(current)

    papers: list[dict[str, Any]] = []
    for raw in raw_records:
        title = clean_text(raw.get("TI"))
        journal = clean_text(raw.get("SO") or raw.get("JI") or raw.get("J9"))
        if not title or not journal:
            continue
        year = as_int(raw.get("PY"), 0)
        published = dt.date(year, 1, 1) if 1900 <= year <= today.year else None
        doi = clean_text(raw.get("DI"))
        paper = {
            "Key": paper_key_from_parts(title, doi),
            "DoiKey": doi_key_from_doi(doi),
            "TitleKey": title_key_from_title(title),
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
        papers.append(paper)
    return papers


def keep_by_date(record: dict[str, Any], start_date: dt.date, end_date: dt.date) -> bool:
    value = clean_text(record.get("DateObject"))
    if not value:
        return False
    try:
        date_value = dt.date.fromisoformat(value[:10])
    except ValueError:
        return False
    return start_date <= date_value <= end_date or start_date.year <= date_value.year <= end_date.year


# ------------------------------ MiniMax client ------------------------------

@dataclass(frozen=True)
class MiniMaxConfig:
    api_key: str
    base_url: str = "https://api.minimaxi.com/v1"
    model: str = "MiniMax-M2.7"
    batch_size: int = 20
    timeout_sec: int = 120
    max_retries: int = 4
    retry_base_sec: float = 5.0

    @classmethod
    def from_file(cls, path: Path) -> "MiniMaxConfig":
        cfg = read_json(path, {})
        if not isinstance(cfg, dict):
            raise RuntimeError(f"MiniMax config must be a JSON object: {path}")

        api_key = clean_text(cfg.get("apiKey") or os.environ.get("MINIMAX_API_KEY"))
        is_placeholder = api_key == "YOUR_MINIMAX_API_KEY" or (
            api_key.startswith("sk-") and set(api_key[6:]) <= {"x", "X", "*"}
        )
        if not api_key or is_placeholder:
            raise RuntimeError(f"Missing MiniMax API key. Set apiKey in {path} or MINIMAX_API_KEY.")

        batch_size = int(cfg.get("batchSize") or 20)
        if batch_size <= 0:
            raise RuntimeError("MiniMax batchSize must be positive.")

        return cls(
            api_key=api_key,
            base_url=str(cfg.get("baseUrl") or "https://api.minimaxi.com/v1"),
            model=str(cfg.get("model") or "MiniMax-M2.7"),
            batch_size=batch_size,
            timeout_sec=int(cfg.get("timeoutSec") or 120),
            max_retries=int(cfg.get("maxRetries") or 4),
            retry_base_sec=float(cfg.get("retryBaseSec") or 5),
        )


def is_retryable_minimax_error(message: str) -> bool:
    lowered = message.lower()
    if any(x in lowered for x in ["invalid api key", "unauthorized", "permission", "forbidden", "not support model"]):
        return False
    return any(
        x in lowered
        for x in [
            "timeout",
            "timed out",
            "rate limit",
            "too many requests",
            "request拥挤",
            "请求拥挤",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "connection",
            "server_error",
            "invalid minimax json",
            "expecting property name enclosed in double quotes",
            "expecting value",
            "unterminated string",
            "extra data",
            "missing list field",
            "non-object result",
            "missing key",
            "unknown key",
            "duplicate key",
            "invalid score",
            "missing scorebreakdown",
            "missing tags object",
            "response missing",
        ]
    )


def parse_model_json(text: str) -> dict[str, Any]:
    cleaned = clean_text(text)
    cleaned = re.sub(r"^```json\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start : end + 1]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid MiniMax JSON: {exc}; preview={clean_text(cleaned[:500])}") from exc


class MiniMaxScreeningTool:
    def __init__(self, config: MiniMaxConfig, log: LogFn = print) -> None:
        if requests is None:
            raise RuntimeError("The requests package is required for MiniMax screening.")
        self.config = config
        self.log = log

    def request_json(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        response = requests.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {self.config.api_key}", "User-Agent": USER_AGENT},
            timeout=self.config.timeout_sec,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code} for {url}: {response.text}")
        return response.json()

    def screen_batch(self, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prompt = build_user_prompt(batch)
        body = {
            "model": self.config.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an academic paper screening assistant for a PhD literature radar. "
                        "Use only the supplied title, journal, date, and abstract. "
                        "Return strict JSON only."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }

        for attempt in range(1, self.config.max_retries + 2):
            try:
                response = self.request_json(body)
                content = str(response["choices"][0]["message"]["content"])
                result = validate_minimax_results(parse_model_json(content), batch)
                self.log(f"MiniMax response validated | attempt={attempt}; input={len(batch)}; output={len(result)}")
                return result
            except Exception as exc:
                message = str(exc)
                if attempt > self.config.max_retries or not is_retryable_minimax_error(message):
                    raise
                wait_sec = min(60.0, self.config.retry_base_sec * (2 ** (attempt - 1)))
                self.log(f"WARNING: MiniMax transient failure | attempt={attempt}; retryInSec={wait_sec:g}; {message}")
                time.sleep(wait_sec)

        raise RuntimeError("MiniMax screening failed.")


def build_user_prompt(papers: list[dict[str, Any]]) -> str:
    payload = [
        {
            "key": clean_text(paper.get("Key")),
            "title": clean_text(paper.get("Title")),
            "journal": clean_text(paper.get("Journal")),
            "abstract": paper_abstract(paper),
        }
        for paper in papers
    ]

    return f"""Task:
Screen every paper for the user's research agenda, assign a recommendation score, and produce rich but concise tags.

Research directions:
1. transport_energy_integration
   Transport-energy integration, EVs, electric buses, charging infrastructure, wireless charging, smart grids, demand response, renewable energy, energy-system planning/scheduling/operations/optimization.

2. transport_emergency_resilience
   Transport disruption, public transport/transit emergency operations, resilience, disruption management, routing, scheduling, resource allocation, recovery, and multimodal emergency response.

3. or_ai
   AI/ML for operations research and optimization, including decision-focused learning, learning to optimize, learning-augmented optimization, MIP, combinatorial optimization, stochastic programming, Benders decomposition, solver learning, or data-driven optimization.

4. ai_behavior
   AI/ML/LLM/deep learning for travel behavior, traveler/passenger behavior, choice modeling, mode choice, discrete choice, preference learning, or transportation/mobility/airport/transit behavior analysis.

Decision rules:
- Use only the supplied metadata. Do not invent unstated topics.
- accept=true only if the paper genuinely matches at least one research direction.
- Reject keyword-only false positives or papers where the matching term is incidental.
- If accept=false: primaryDirection="", directions=[], score=0-3, and use at most 3 tag fields.
- If accept=true: include every matched direction and choose the strongest one as primaryDirection.

Scoring:
Return both "score" and "scoreBreakdown".
- directionRelevance: 0-10, match to the user's research directions.
- methodRelevance: 0-10, usefulness of the method/model/algorithm.
- novelty: 0-10, freshness or distinctiveness suggested by title/abstract.
- transferability: 0-10, usefulness for the user's future papers or modeling design.
- total: sum of the four components, 0-40.
- score: rounded average of the four components, 0-10.

Tagging rules:
- For accepted papers, produce 8-18 useful tag fields when supported by the title/abstract.
- "Keywords" must contain 6-12 concise phrases for accepted papers.
- Prefer precise academic phrases over generic labels.
- Use short strings for single-value tags and short arrays for multi-value tags.
- Do not summarize the full abstract in tags.
- Do not repeat the same idea under many names.
- You may add additional concise tag fields if they are clearly supported.

Suggested tag fields:
Object; Scenario; Application domain; Transport mode; Energy component; Charging type; Infrastructure; Battery treatment; Grid interaction; Disruption type; Emergency context; Response strategy; Resilience aspect; Behavior subject; Behavior context; Behavior model; AI component; OR component; Integration form; Problem type; Decision level; Method; Model form; Solver/algorithm; Decomposition; Learning target; Prediction target; Optimization target; Decision variables; Constraints/resource; Uncertainty; Data usage; Objective; Stakeholder; Policy relevance; Computational aspect; Evaluation setting; Experiment design; Output type; Transferable idea; Keywords.

Direction-specific tagging:
- transport_energy_integration: try to include Object, Energy component, Infrastructure/Charging type, Grid interaction, Decision level, Method, Objective, Evaluation setting.
- transport_emergency_resilience: try to include Disruption type, Emergency context, Response strategy, Resilience aspect, Transport mode, Decision level, Method, Objective.
- or_ai: try to include AI component, OR component, Learning target, Optimization target, Solver/algorithm, Problem type, Data usage, Transferable idea.
- ai_behavior: try to include Behavior subject, Behavior context, Behavior model, AI component, Data usage, Prediction target, Heterogeneity, Transferable idea.

Comment:
comment must be exactly one concise academic sentence describing the paper's main focus.

Return exactly one JSON object:
{{
  "results": [
    {{
      "key": "same key as input",
      "accept": true,
      "primaryDirection": "transport_energy_integration",
      "directions": ["transport_energy_integration"],
      "score": 8,
      "scoreBreakdown": {{
        "directionRelevance": 9,
        "methodRelevance": 8,
        "novelty": 7,
        "transferability": 8,
        "total": 32
      }},
      "tags": {{
        "Object": "electric bus charging system",
        "Application domain": "public transport electrification",
        "Problem type": "infrastructure planning",
        "Decision level": "strategic planning",
        "Method": "mixed-integer optimization",
        "Objective": "lifecycle cost minimization",
        "Evaluation setting": "bus network case study",
        "Transferable idea": "joint charging infrastructure and operations design",
        "Keywords": ["electric bus", "charging infrastructure", "wireless charging", "optimization", "battery health", "public transport"]
      }},
      "comment": "The study develops an optimization model for electric bus charging infrastructure planning under operational constraints."
    }}
  ]
}}

Strict JSON requirements:
- Use double quotes for all keys and string values.
- Do not use markdown code fences.
- Do not include trailing commas or null values.
- Every input paper must appear exactly once in results.
- Keep the original paper key unchanged.

Papers:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def validate_minimax_results(response: dict[str, Any], batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = response.get("results")
    if not isinstance(results, list):
        raise RuntimeError("MiniMax response is missing list field: results.")

    input_keys = [clean_text(paper.get("Key")) for paper in batch]
    input_key_set = set(input_keys)
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []

    for item in results:
        if not isinstance(item, dict):
            raise RuntimeError("MiniMax response contains a non-object result.")
        key = clean_text(item.get("key"))
        if not key:
            raise RuntimeError("MiniMax result is missing key.")
        if key not in input_key_set:
            raise RuntimeError(f"MiniMax returned an unknown key: {key}")
        if key in seen:
            raise RuntimeError(f"MiniMax returned a duplicate key: {key}")
        seen.add(key)

        score = as_int(item.get("score"), -1)
        if score < 0 or score > 10:
            raise RuntimeError(f"MiniMax result has invalid score for {key}: {item.get('score')}")
        if not isinstance(item.get("scoreBreakdown"), dict):
            raise RuntimeError(f"MiniMax result is missing scoreBreakdown for {key}.")
        if not isinstance(item.get("tags"), dict):
            raise RuntimeError(f"MiniMax result is missing tags object for {key}.")
        normalized.append(item)

    missing = [key for key in input_keys if key not in seen]
    if missing:
        raise RuntimeError(f"MiniMax response missing {len(missing)} keys; first={missing[:5]}")
    return normalized


# ------------------------------- queue output -------------------------------

def normalize_score_breakdown(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise RuntimeError("scoreBreakdown must be an object.")
    output = {
        "DirectionRelevance": as_int(value.get("directionRelevance"), 0),
        "MethodRelevance": as_int(value.get("methodRelevance"), 0),
        "Novelty": as_int(value.get("novelty"), 0),
        "Transferability": as_int(value.get("transferability"), 0),
    }
    output["Total"] = sum(output.values())
    return output


def normalize_tags(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, Any] = {}
    for key, raw in value.items():
        name = clean_text(key)
        if not name:
            continue
        if isinstance(raw, list):
            items = [clean_text(item) for item in raw if clean_text(item)]
            if items:
                output[name] = items
        else:
            text = clean_text(raw)
            if text:
                output[name] = text
    return output


def build_queue_record(paper: dict[str, Any], result: dict[str, Any], min_push_score: int) -> dict[str, Any]:
    score = as_int(result.get("score"), 0)
    breakdown = normalize_score_breakdown(result.get("scoreBreakdown"))
    accepted = bool(result.get("accept"))
    directions_raw = result.get("directions", [])
    directions = [clean_text(x) for x in directions_raw if clean_text(x)] if isinstance(directions_raw, list) else []
    primary_direction = clean_text(result.get("primaryDirection") or (directions[0] if directions else ""))
    tags = normalize_tags(result.get("tags"))
    now = dt.datetime.now().astimezone().isoformat()
    in_push_queue = accepted and score >= min_push_score

    return {
        "Key": paper["Key"],
        "DoiKey": clean_text(paper.get("DoiKey")) or doi_key_from_doi(paper.get("DOI", "")),
        "TitleKey": clean_text(paper.get("TitleKey")) or title_key_from_title(paper.get("Title", "")),
        "Title": paper["Title"],
        "Journal": paper["Journal"],
        "Date": paper["Date"],
        "DateObject": paper["DateObject"],
        "DOI": paper["DOI"],
        "Url": paper["Url"],
        "Abstract": paper_abstract(paper),
        "Source": paper["Source"],
        "Accepted": accepted,
        "Status": "pending" if in_push_queue else "rejected",
        "InPushQueue": in_push_queue,
        "PrimaryDirection": primary_direction,
        "MatchedDirections": directions,
        "Score": score,
        "RecommendationScore": breakdown["Total"],
        "ScoreBreakdown": breakdown,
        "Tags": tags,
        "Keywords": tags.get("Keywords", []),
        "Comment": clean_text(result.get("comment") or ""),
        "ScreenSchemaVersion": SCREEN_SCHEMA_VERSION,
        "ScreenedAt": now,
        "PushedAt": None,
        "Feedback": None,
    }


def sort_base_queue(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda x: (str(x.get("ScreenedAt") or ""), str(x.get("Key") or "")), reverse=True)


def sort_push_queue(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda x: (int(x.get("RecommendationScore", 0)), int(x.get("Score", 0)), str(x.get("DateObject") or "")),
        reverse=True,
    )


def legacy_queue_record(record: dict[str, Any]) -> dict[str, Any]:
    tags = record.get("Tags", {}) if isinstance(record.get("Tags"), dict) else {}
    return {
        "Key": record["Key"],
        "DoiKey": record.get("DoiKey", ""),
        "TitleKey": record.get("TitleKey", ""),
        "Source": record["Source"],
        "Title": record["Title"],
        "Journal": record["Journal"],
        "DateObject": record["DateObject"],
        "Abstract": record.get("Abstract", ""),
        "MatchedDirections": record.get("MatchedDirections", []),
        "RecommendationScore": int(record.get("RecommendationScore", 0)),
        "ScoreBreakdown": record.get("ScoreBreakdown", {}),
        "LlmScreen": {
            "Accepted": bool(record.get("Accepted")),
            "Score": int(record.get("Score", 0)),
            "PrimaryDirection": record.get("PrimaryDirection", ""),
            "Directions": record.get("MatchedDirections", []),
            "Tags": tags,
            "Comment": record.get("Comment", ""),
            "Keywords": tags.get("Keywords", []),
        },
        "ScreenSchemaVersion": SCREEN_SCHEMA_VERSION,
        "QueuedAt": record.get("ScreenedAt"),
    }


def save_queues(
    workspace: Path,
    base_queue: list[dict[str, Any]],
    push_queue: list[dict[str, Any]],
    processed_count: int,
    total_to_screen: int,
    write_legacy: bool = True,
) -> None:
    data_dir = workspace / "data"
    base_sorted = sort_base_queue(base_queue)
    push_sorted = sort_push_queue(push_queue)

    write_json(data_dir / "paper_base_queue.json", base_sorted)
    write_json(data_dir / "paper_push_queue.json", push_sorted)

    if write_legacy:
        write_json(data_dir / "paper-candidate-queue.json", [legacy_queue_record(item) for item in push_sorted])
        write_json(
            data_dir / "paper-screening-cache.json",
            {
                "generatedAt": dt.datetime.now().astimezone().isoformat(),
                "papers": [
                    {
                        "Key": item["Key"],
                        "DoiKey": item.get("DoiKey", ""),
                        "TitleKey": item.get("TitleKey", ""),
                        "Title": item["Title"],
                        "Journal": item["Journal"],
                        "Accepted": bool(item.get("Accepted")),
                        "Score": int(item.get("Score", 0)),
                        "RecommendationScore": int(item.get("RecommendationScore", 0)),
                        "ScoreBreakdown": item.get("ScoreBreakdown", {}),
                        "Tags": item.get("Tags", {}),
                        "Comment": item.get("Comment", ""),
                        "ScreenSchemaVersion": SCREEN_SCHEMA_VERSION,
                        "ScreenedAt": item.get("ScreenedAt"),
                    }
                    for item in base_sorted
                ],
            },
        )

    write_json(
        data_dir / "paper-queue-build-state.json",
        {
            "generatedAt": dt.datetime.now().astimezone().isoformat(),
            "mode": "MiniMaxScreening",
            "screenSchemaVersion": SCREEN_SCHEMA_VERSION,
            "processedCount": processed_count,
            "totalToScreen": total_to_screen,
            "baseQueueSize": len(base_sorted),
            "pushQueueSize": len(push_sorted),
            "baseQueuePath": str(data_dir / "paper_base_queue.json"),
            "pushQueuePath": str(data_dir / "paper_push_queue.json"),
        },
    )


def screen_papers_to_queues(
    papers: list[dict[str, Any]],
    workspace: str | Path,
    paper_watch_config: dict[str, Any] | None = None,
    min_push_score: int | None = None,
    write_legacy: bool = True,
    log: LogFn = print,
) -> list[dict[str, Any]]:
    workspace_path = Path(workspace).resolve()
    if paper_watch_config is None:
        paper_watch_config = read_json(workspace_path / "automation" / "paper-watch.config.json", {})
        if not isinstance(paper_watch_config, dict):
            paper_watch_config = {}

    screening = paper_watch_config.get("llmScreening", {})
    if not isinstance(screening, dict):
        screening = {}
    minimax_path = workspace_path / str(screening.get("configPath") or "automation/minimax.config.json")
    min_score = int(min_push_score if min_push_score is not None else screening.get("minLlmScore", 6))

    config = MiniMaxConfig.from_file(minimax_path)
    tool = MiniMaxScreeningTool(config, log=log)

    base_path = workspace_path / "data" / "paper_base_queue.json"
    push_path = workspace_path / "data" / "paper_push_queue.json"

    base_queue = read_json(base_path, [])
    push_queue = read_json(push_path, [])
    if not isinstance(base_queue, list):
        base_queue = []
    if not isinstance(push_queue, list):
        push_queue = []

    screened_identities = {
        identity
        for item in base_queue
        if int(item.get("ScreenSchemaVersion", 0) or 0) >= SCREEN_SCHEMA_VERSION
        for identity in paper_identities(item)
    }
    screened_identities.discard("")

    deduped = merge_by_identity([paper for paper in papers if paper.get("Key")])
    to_screen = [paper for paper in deduped if not any(identity in screened_identities for identity in paper_identities(paper))]
    log(
        f"MiniMax screening start | input={len(papers)}; deduped={len(deduped)}; "
        f"toScreen={len(to_screen)}; batchSize={config.batch_size}; minPushScore={min_score}"
    )

    processed = 0
    total_batches = (len(to_screen) + config.batch_size - 1) // config.batch_size if to_screen else 0

    for start in range(0, len(to_screen), config.batch_size):
        batch = to_screen[start : start + config.batch_size]
        batch_no = start // config.batch_size + 1
        log(f"Sending batch to MiniMax | batch={batch_no}/{total_batches}; papers={len(batch)}")
        results = tool.screen_batch(batch)
        by_key = {paper["Key"]: paper for paper in batch}
        new_records: list[dict[str, Any]] = []

        for result in results:
            key = clean_text(result.get("key"))
            paper = by_key[key]
            record = build_queue_record(paper, result, min_score)
            new_records.append(record)

        base_queue = merge_by_key(base_queue + new_records)
        push_queue = merge_by_key(push_queue + [record for record in new_records if record["InPushQueue"]])
        processed += len(batch)

        save_queues(
            workspace_path,
            base_queue,
            push_queue,
            processed_count=processed,
            total_to_screen=len(to_screen),
            write_legacy=write_legacy,
        )
        log(
            f"Batch saved | batch={batch_no}/{total_batches}; "
            f"acceptedForPush={sum(1 for x in new_records if x['InPushQueue'])}/{len(batch)}; "
            f"baseQueue={len(base_queue)}; pushQueue={len(push_queue)}"
        )

    save_queues(
        workspace_path,
        base_queue,
        push_queue,
        processed_count=processed,
        total_to_screen=len(to_screen),
        write_legacy=write_legacy,
    )
    log(f"MiniMax screening complete | screened={len(to_screen)}; baseQueue={len(base_queue)}; pushQueue={len(push_queue)}")
    return sort_push_queue(push_queue)


def parse_wos_exports_and_screen(
    export_files: list[str | Path],
    workspace: str | Path,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    paper_watch_config: dict[str, Any] | None = None,
    min_push_score: int | None = None,
    write_legacy: bool = True,
    log: LogFn = print,
) -> list[dict[str, Any]]:
    workspace_path = Path(workspace).resolve()
    if paper_watch_config is None:
        paper_watch_config = read_json(workspace_path / "automation" / "paper-watch.config.json", {})
        if not isinstance(paper_watch_config, dict):
            paper_watch_config = {}

    targets = config_journal_names(paper_watch_config)
    start_date = start_date or dt.date(2023, 1, 1)
    end_date = end_date or dt.date.today()

    imported = 0
    kept: list[dict[str, Any]] = []
    for file in export_files:
        path = Path(file)
        records = parse_wos_plain_text(path)
        imported += len(records)
        for record in records:
            if targets and not is_target_journal(str(record.get("Journal", "")), targets):
                continue
            if keep_by_date(record, start_date, end_date):
                kept.append(record)

    deduped = merge_by_key(kept)
    write_json(
        workspace_path / "data" / "paper-source-candidates.json",
        {"generatedAt": dt.datetime.now().astimezone().isoformat(), "source": "source_exports", "papers": deduped},
    )
    log(f"WoS parse complete | files={len(export_files)}; imported={imported}; kept={len(kept)}; deduped={len(deduped)}")
    return screen_papers_to_queues(
        deduped,
        workspace_path,
        paper_watch_config=paper_watch_config,
        min_push_score=min_push_score,
        write_legacy=write_legacy,
        log=log,
    )


def screen_items_file(
    workspace: str | Path,
    input_path: str | Path = "data/wos_minimax_items.json",
    min_push_score: int | None = None,
    limit: int = 0,
    dry_run: bool = False,
    rebuild_push: bool = False,
    write_legacy: bool = True,
    log: LogFn = print,
) -> list[dict[str, Any]]:
    workspace_path = Path(workspace).resolve()
    input_file = Path(input_path)
    if not input_file.is_absolute():
        input_file = workspace_path / input_file

    paper_watch_config = read_json(workspace_path / "automation" / "paper-watch.config.json", {})
    if not isinstance(paper_watch_config, dict):
        paper_watch_config = {}

    if rebuild_push:
        screening = paper_watch_config.get("llmScreening", {})
        min_score = int(min_push_score if min_push_score is not None else (screening.get("minLlmScore", 6) if isinstance(screening, dict) else 6))
        base_queue = read_json(workspace_path / "data" / "paper_base_queue.json", [])
        if not isinstance(base_queue, list):
            base_queue = []
        push_queue = [item for item in base_queue if bool(item.get("Accepted")) and int(item.get("Score", 0) or 0) >= min_score]
        save_queues(workspace_path, base_queue, push_queue, processed_count=0, total_to_screen=0, write_legacy=write_legacy)
        log(f"Push queue rebuilt | baseQueue={len(base_queue)}; pushQueue={len(push_queue)}; minPushScore={min_score}")
        return sort_push_queue(push_queue)

    papers = load_normalized_papers(input_file, source="wos_minimax_items")
    if limit > 0:
        papers = papers[:limit]

    if dry_run:
        log(f"Dry run | input={len(papers)}")
        for paper in papers[:20]:
            log(f"{paper['Key']} | {paper['Journal']} | {paper['Title']}")
        return []

    return screen_papers_to_queues(
        papers,
        workspace_path,
        paper_watch_config=paper_watch_config,
        min_push_score=min_push_score,
        write_legacy=write_legacy,
        log=log,
    )


TOOL_SPEC = {
    "name": "screen_papers_with_minimax",
    "description": "Screen normalized paper records with MiniMax, produce rich tags, and update base/push queues.",
    "inputs": {
        "papers": "List of normalized paper records with Key, Title, Journal, DateObject, DOI, Abstract.",
        "workspace": "Project root containing automation/minimax.config.json and automation/paper-watch.config.json.",
    },
    "output": "Updated paper_push_queue.json list sorted by recommendation score.",
}

#!/usr/bin/env python3
"""Run a small sequential MiniMax health test.

The script sends exactly 20 lightweight chat completion requests and writes a
checkpoint after every request, so partial results remain available if the run
is interrupted.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any

import requests


REQUEST_COUNT = 20
REQUEST_INTERVAL_SEC = 0.5


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    workspace = Path(__file__).resolve().parents[1]
    config_path = workspace / "automation" / "minimax.config.json"
    config = read_json(config_path)

    api_key = str(config.get("apiKey") or os.environ.get("MINIMAX_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError(f"Missing MiniMax API key in {config_path} or MINIMAX_API_KEY.")

    base_url = str(config.get("baseUrl", "https://api.minimaxi.com/v1")).rstrip("/")
    model = str(config.get("model", "MiniMax-M2.7"))
    timeout_sec = int(config.get("timeoutSec", 120))
    url = f"{base_url}/chat/completions"

    started_at = dt.datetime.now().astimezone()
    report_path = workspace / "reports" / f"minimax_health_test_{started_at.strftime('%Y%m%d_%H%M%S')}.json"
    report: dict[str, Any] = {
        "startedAt": started_at.isoformat(),
        "finishedAt": None,
        "model": model,
        "baseUrl": base_url,
        "requestCount": REQUEST_COUNT,
        "successCount": 0,
        "failureCount": 0,
        "results": [],
    }
    write_json(report_path, report)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8",
    }

    for index in range(1, REQUEST_COUNT + 1):
        body = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "Return strict JSON only."},
                {
                    "role": "user",
                    "content": f'Return exactly this JSON object: {{"ok": true, "request": {index}}}',
                },
            ],
        }

        request_started = time.perf_counter()
        item: dict[str, Any] = {
            "index": index,
            "startedAt": dt.datetime.now().astimezone().isoformat(),
            "ok": False,
            "statusCode": None,
            "elapsedSec": None,
            "content": "",
            "error": "",
        }
        try:
            response = requests.post(url, headers=headers, json=body, timeout=timeout_sec)
            item["statusCode"] = response.status_code
            item["elapsedSec"] = round(time.perf_counter() - request_started, 3)
            if response.status_code >= 400:
                item["error"] = response.text
            else:
                data = response.json()
                item["content"] = str(data["choices"][0]["message"]["content"])
                item["ok"] = True
        except Exception as exc:
            item["elapsedSec"] = round(time.perf_counter() - request_started, 3)
            item["error"] = str(exc)

        report["results"].append(item)
        report["successCount"] = len([result for result in report["results"] if result.get("ok")])
        report["failureCount"] = len(report["results"]) - int(report["successCount"])
        report["finishedAt"] = dt.datetime.now().astimezone().isoformat()
        write_json(report_path, report)

        status = "OK" if item["ok"] else "FAIL"
        print(f"[{index:02d}/{REQUEST_COUNT}] {status} elapsed={item['elapsedSec']}s status={item['statusCode']}")
        if index < REQUEST_COUNT and REQUEST_INTERVAL_SEC > 0:
            time.sleep(REQUEST_INTERVAL_SEC)

    print(f"MiniMax health test report: {report_path}")


if __name__ == "__main__":
    main()

"""Shared CDP WebSocket client for Chrome DevTools Protocol communication.

Used by both wos_cdp_workflow.py (search/export) and paper_downloader.py (PDF Fetch).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from websocket import create_connection

DEFAULT_PORT = 9224


def extract_doi(key: str) -> str | None:
    """Extract bare DOI from a paper Key (e.g. 'doi:10.xxx/yyy' → '10.xxx/yyy')."""
    if key.startswith("doi:"):
        return key[4:]
    return None


class CDPClient:
    """Connect to Chrome's remote debugging port and send CDP commands."""

    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port
        try:
            targets = json.loads(
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5).read())
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"Chrome not reachable on port {port}: {e}") from e

        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            for t in targets:
                if "webSocketDebuggerUrl" in t:
                    pages = [t]
                    break
        if not pages:
            raise RuntimeError("No page targets found")

        self.ws = create_connection(
            pages[0]["webSocketDebuggerUrl"],
            timeout=10,
            origin="http://localhost",
            suppress_origin=True,
        )
        self.ws.settimeout(3)
        self._next_id = 1
        self._send("Runtime.enable")
        self._recv(self._next_id - 1)

    def _send(self, method: str, params: dict | None = None) -> int:
        msg_id = self._next_id
        self._next_id += 1
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        return msg_id

    def _recv(self, expected_id: int, timeout: float = 15) -> dict:
        """Wait for a CDP response with the given id. Events (no id) are skipped."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.ws.settimeout(min(3, max(0.5, deadline - time.time() + 0.5)))
                obj = json.loads(self.ws.recv())
                if obj.get("id") == expected_id:
                    return obj
                # Events (e.g. Fetch.requestPaused) have no id — skip them
            except Exception:
                # WebSocket timeout or decode error during idle periods (e.g.
                # SSO redirect chain) — just keep waiting
                pass
        raise TimeoutError("CDP response timeout")

    def request(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        """Send a command and return the result dict."""
        msg_id = self._send(method, params)
        return self._recv(msg_id, timeout=timeout).get("result", {})

    def eval_js(self, code: str, timeout: float = 10) -> str:
        result = self.request(
            "Runtime.evaluate",
            {"expression": code, "returnByValue": True},
            timeout=timeout,
        )
        val = result.get("result", {}).get("value", "")
        return "" if val is None else val

    def eval_js_async(self, code: str, timeout: float = 60) -> str:
        result = self.request(
            "Runtime.evaluate",
            {"expression": code, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        val = result.get("result", {}).get("value", "")
        return "" if val is None else val

    def close(self):
        self.ws.close()

    def _http_api(self, path: str, method: str = "GET") -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def open_page(self, url: str) -> dict:
        from urllib.parse import quote
        return self._http_api(
            f"/json/new?{quote(url, safe=':/?&=%')}", method="PUT")

    def close_page(self, page_id: str) -> None:
        try:
            self._http_api(f"/json/close/{page_id}")
        except Exception:
            pass

    @staticmethod
    def evaluate_on_page(ws_url: str, expression: str, *,
                         await_promise: bool = False,
                         timeout: float = 60) -> str | None:
        ws = create_connection(ws_url, timeout=timeout, suppress_origin=True)
        try:
            ws.send(json.dumps({
                "id": 1, "method": "Runtime.evaluate",
                "params": {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": await_promise,
                },
            }))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    if "error" in msg:
                        return None
                    return msg.get("result", {}).get("result", {}).get("value")
        finally:
            ws.close()

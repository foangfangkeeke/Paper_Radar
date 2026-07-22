#!/usr/bin/env python3
"""Download a single paper PDF from targeted journal/publisher.

Supports ScienceDirect (Elsevier, via CDP Fetch) and INFORMS (via EBSCOhost, since Jan 2026).
Springer, Wiley, Nature — coming soon.

Usage:
  python skills/download/paper_downloader.py --doi "10.xxx/yyy" --title "Paper Title" --journal "European Journal of Operational Research"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills"))

from cdp_client import CDPClient, extract_doi
PUSH_QUEUE_PATH = ROOT / "data" / "paper_push_queue.json"
CONFIG_PATH = ROOT / "skills" / "download" / "download.config.json"
CREDENTIALS_PATH = ROOT / "data" / "credentials.json"

CDP_PORT = 9224
CDP_TIMEOUT = 60

CHROME_PATHS = [
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]


_PDF_METADATA_RE = re.compile(
    r'"pdfDownload":\{"isPdfFullText":(?:true|false),'
    r'"urlMetadata":\{"queryParams":\{"md5":"([^"]+)","pid":"([^"]+)"\},'
    r'"pii":"([^"]+)","pdfExtension":"([^"]+)","path":"([^"]+)"\}\}'
)
_SIGNED_PDF_RE = re.compile(
    r"https://pdf\.sciencedirectassets\.com/[^\s\"'<>]+", flags=re.I
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


# URL pattern → publisher (DOI redirect reveals the publisher)
_PUBLISHER_URL_PATTERNS = [
    ("sciencedirect.com", "sciencedirect"),
    ("linkinghub.elsevier.com", "sciencedirect"),  # Elsevier also redirects here
    ("pubsonline.informs.org", "informs"),
    ("link.springer.com", "springer"),
    ("nature.com", "nature"),
    ("onlinelibrary.wiley.com", "wiley"),
]


def detect_publisher_from_doi(doi: str) -> str:
    """Resolve DOI via HTTP GET and detect publisher from redirect URL.

    Uses a single HTTP request (no browser needed), so it's fast and doesn't
    interfere with any active Chrome tab on CDP_PORT.

    Returns publisher key (e.g. 'sciencedirect', 'informs') or '' if unknown.
    """
    normalized_doi = doi.strip().lower()
    if normalized_doi.startswith("10.1287/"):
        print("  [Auto] Detected publisher: informs (from DOI prefix)")
        return "informs"
    if normalized_doi.startswith("10.1016/"):
        print("  [Auto] Detected publisher: sciencedirect (from DOI prefix)")
        return "sciencedirect"

    # Try HTTP first (fast, no browser needed)
    import urllib.request as _ur
    req = _ur.Request(f"https://doi.org/{doi}")
    req.add_header("User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    try:
        resp = _ur.urlopen(req, timeout=10)
        location = (resp.geturl() or "").lower()
        for pattern, publisher in _PUBLISHER_URL_PATTERNS:
            if pattern in location:
                print(f"  [Auto] Detected publisher: {publisher} (from DOI redirect)")
                return publisher
    except Exception:
        pass

    # Fallback: HTTP blocked (e.g. INFORMS 403) → use browser navigation
    try:
        client = CDPClient(port=CDP_PORT)
    except RuntimeError:
        return ""
    try:
        client.request("Page.enable")
        client.request("Page.navigate", {"url": f"https://doi.org/{doi}"}, timeout=15)
        for _ in range(10):
            time.sleep(0.5)
            url = (client.eval_js("location.href") or "").lower()
            for pattern, publisher in _PUBLISHER_URL_PATTERNS:
                if pattern in url:
                    print(f"  [Auto] Detected publisher: {publisher} (from DOI redirect, via CDP)")
                    return publisher
    except Exception:
        pass
    finally:
        client.close()
    return ""


def load_push_queue():
    return json.loads(PUSH_QUEUE_PATH.read_text(encoding="utf-8"))


def slugify(text: str, max_len: int = 80) -> str:
    text = re.sub(r'[<>:"/\\|?*]', "", text)
    text = re.sub(r"\s+", "_", text)
    return text[:max_len].rstrip("_.")


def build_filename(paper: dict) -> str:
    title = paper.get("Title", "Unknown")
    first_word = title.split()[0].rstrip(",:;")
    if len(first_word) < 3:
        first_word = title.split()[1] if len(title.split()) > 1 else "Paper"
    first_word = re.sub(r"[^a-zA-Z0-9_-]", "", first_word)
    year_match = re.search(r"(20\d{2})", title)
    year = year_match.group(1) if year_match else "2025"
    short = slugify(title, 60)
    return f"{first_word}_{year}_{short}.pdf"


# ---------------------------------------------------------------------------
# ScienceDirect page-context fetch helpers
# Adapted from sciencedirect-live-session-fetcher by Given-Dream
# ---------------------------------------------------------------------------

def _extract_pdf_metadata_from_html(html: str) -> str | None:
    match = _PDF_METADATA_RE.search(html)
    if not match:
        return None
    md5, pid, pii, pdf_ext, path = match.groups()
    return f"https://www.sciencedirect.com/{path}/{pii}{pdf_ext}?md5={md5}&pid={pid}"


def _extract_signed_pdf_url(text: str) -> str | None:
    import html as _html
    import urllib.parse as _up
    decoded = _html.unescape(text or "").replace("\\/", "/").replace("\\u0026", "&")
    match = _SIGNED_PDF_RE.search(decoded)
    if match:
        return match.group(0)
    decoded = _up.unquote(decoded)
    match = _SIGNED_PDF_RE.search(decoded)
    return match.group(0) if match else None


def _build_page_fetch_js(pdf_url: str) -> str:
    return f"""
new Promise(resolve => {{
  fetch({json.dumps(pdf_url)}, {{ credentials: 'include' }})
    .then(resp => {{
      if (!resp.ok) return resolve('ERR:HTTP ' + resp.status);
      return resp.arrayBuffer();
    }})
    .then(buf => {{
      if (typeof buf === 'string') return;
      const data = new Uint8Array(buf);
      const chunk = 0x8000;
      let binary = '';
      for (let i = 0; i < data.length; i += chunk)
        binary += String.fromCharCode.apply(null, data.subarray(i, i + chunk));
      resolve(btoa(binary));
    }})
    .catch(err => resolve('ERR:' + String(err)));
}})
    """.strip()


def _page_context_fetch_on_ws(ws_url: str, pdf_url: str, timeout: float = 90) -> bytes | None:
    import base64 as _b64
    value = CDPClient.evaluate_on_page(
        ws_url, _build_page_fetch_js(pdf_url), await_promise=True, timeout=timeout)
    if not value or (isinstance(value, str) and value.startswith("ERR:")):
        if value:
            print(f"  [CDP] Page-context fetch failed: {value}")
        return None
    try:
        data = _b64.b64decode(value)
    except Exception:
        return None
    return data if data.startswith(b"%PDF-") else None


def _http_get_pdf(url: str, timeout: float = 60, *,
                  cookies: list[dict] | None = None,
                  referer: str = "",
                  user_agent: str = "") -> bytes | None:
    cookie_header = ""
    if cookies:
        cookie_header = "; ".join(
            f"{c['name']}={c['value']}" for c in cookies
            if c.get("name") and c.get("value"))
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/pdf,*/*",
    })
    if referer:
        req.add_header("Referer", referer)
    if cookie_header:
        req.add_header("Cookie", cookie_header)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return data if data.startswith(b"%PDF-") else None
    except Exception as e:
        print(f"  [CDP] HTTP GET failed: {e}")
        return None


def _get_cookies_on_ws(ws_url: str, url: str, timeout: float = 10) -> list[dict]:
    from websocket import create_connection
    ws = create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({
            "id": 1,
            "method": "Network.getCookies",
            "params": {"urls": [url]},
        }))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return msg.get("result", {}).get("cookies", [])
    finally:
        ws.close()


def _save_pdf_bytes(dest_path: Path, data: bytes, source: str) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(data)
    print(f"  Downloaded via {source}: {len(data)//1024} KB -> {dest_path}")


def _extract_pdf_bytes_from_viewer(ws_url: str, timeout: float = 30) -> bytes | None:
    import base64 as _b64
    js = """
new Promise(resolve => {
  const deadline = Date.now() + 15000;
  const tick = () => {
    const app = window.PDFViewerApplication;
    if (app && app.pdfDocument) {
      app.pdfDocument.getData().then(data => {
        const chunk = 0x8000;
        let binary = '';
        for (let i = 0; i < data.length; i += chunk)
          binary += String.fromCharCode.apply(null, data.subarray(i, i + chunk));
        resolve(btoa(binary));
      }).catch(err => resolve('ERR:' + String(err)));
    } else if (Date.now() > deadline) {
      resolve('ERR:timeout_waiting_for_pdf_viewer');
    } else {
      setTimeout(tick, 1000);
    }
  };
  tick();
})
    """.strip()
    value = CDPClient.evaluate_on_page(
        ws_url, js, await_promise=True, timeout=timeout)
    if not value or (isinstance(value, str) and value.startswith("ERR:")):
        if value:
            print(f"  [CDP] PDF viewer extract failed: {value}")
        return None
    try:
        data = _b64.b64decode(value)
    except Exception:
        return None
    return data if data.startswith(b"%PDF-") else None


# ---------------------------------------------------------------------------
# Chrome launcher
# ---------------------------------------------------------------------------

def _find_chrome_exe() -> str | None:
    for p in CHROME_PATHS:
        if Path(p).exists():
            return p
    return None


def _ensure_chrome() -> bool:
    """Make sure Chrome is running with --remote-debugging-port=CDP_PORT.

    Uses a persistent Chrome profile (same as EBSCO keep-alive) so that
    real browser cookies, localStorage, and fingerprint survive across
    sessions — this is critical to avoid bot detection on Elsevier/ScienceDirect.

    Chrome is NEVER closed automatically — session cookies would be lost.
    """
    import subprocess

    try:
        urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=3)
        print(f"  [Chrome] Already running on port {CDP_PORT}")
        return True
    except (urllib.error.URLError, OSError):
        pass

    chrome = _find_chrome_exe()
    if not chrome:
        print("  [Chrome] Chrome.exe not found")
        return False

    profile = ROOT / "data" / "chrome_profile"
    if profile.exists():
        print(f"  [Chrome] Using persistent profile: {profile}")
    else:
        print(f"  ⚠ Persistent profile not found: {profile}")
        print(f"  ⚠ Falling back to temp profile — bot detection likely!")
        print(f"  ⚠ See skills/download/download.md for profile setup.")
        profile = Path(__import__('tempfile').gettempdir()) / "chrome_pdf_download"
        profile.mkdir(parents=True, exist_ok=True)

    print(f"  [Chrome] Launching: {chrome}")
    subprocess.Popen(
        [chrome, f"--remote-debugging-port={CDP_PORT}",
         f"--user-data-dir={profile}", "--no-first-run",
         "--no-default-browser-check"],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(15):
        time.sleep(1)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=3)
            print(f"  [Chrome] Ready on port {CDP_PORT}")
            return True
        except (urllib.error.URLError, OSError):
            pass
    print("  [Chrome] Timed out waiting for debug port")
    return False


# ---------------------------------------------------------------------------
# CDP-based PDF download (multi-publisher)
# ---------------------------------------------------------------------------


def _cdp_fetch_and_download(client: CDPClient, article_ws: str, pdfft_url: str,
                            dest_path: Path, timeout: int = 120) -> str | None:
    """Download a ScienceDirect PDF using the live authorized browser session.

    Reference flow:
      article page -> pdfDownload metadata -> pdfft tab -> signed CDN URL
      -> immediate fetch from the same live session.
    """
    pdf_page = client.open_page(pdfft_url)
    pdf_ws = pdf_page["webSocketDebuggerUrl"]
    try:
        time.sleep(6)
        viewer_url = CDPClient.evaluate_on_page(pdf_ws, "location.href") or ""
        viewer_html = CDPClient.evaluate_on_page(
            pdf_ws, "document.documentElement.outerHTML") or ""
        signed_url = _extract_signed_pdf_url(viewer_url) or _extract_signed_pdf_url(viewer_html)
        print(f"  [CDP] PDF tab: {(viewer_url or pdfft_url)[:140]}")

        if signed_url:
            print("  [CDP] Found signed ScienceDirect CDN URL")
            cookies = _get_cookies_on_ws(pdf_ws, signed_url)
            user_agent = CDPClient.evaluate_on_page(pdf_ws, "navigator.userAgent") or ""
            data = _http_get_pdf(
                signed_url,
                timeout=min(timeout, 60),
                cookies=cookies,
                referer=pdfft_url,
                user_agent=user_agent,
            )
            if data:
                _save_pdf_bytes(dest_path, data, "signed-url HTTP GET with browser cookies")
                return signed_url

        viewer_fetch_url = signed_url or viewer_url
        if viewer_fetch_url:
            data = _page_context_fetch_on_ws(
                pdf_ws, viewer_fetch_url, timeout=max(timeout, 90))
            if data:
                _save_pdf_bytes(dest_path, data, "PDF-tab page fetch")
                return viewer_fetch_url

        data = _page_context_fetch_on_ws(
            article_ws, pdfft_url, timeout=max(timeout, 90))
        if data:
            _save_pdf_bytes(dest_path, data, "article-page fetch")
            return pdfft_url

        data = _extract_pdf_bytes_from_viewer(pdf_ws)
        if data:
            _save_pdf_bytes(dest_path, data, "PDF viewer extract")
            return viewer_fetch_url or pdfft_url

        print("  [CDP] PDF bytes were not available from the live session")
        return None
    finally:
        client.close_page(pdf_page["id"])


def cdp_download(doi: str, dest_path: Path, pdf_url_fallback: str | None = None,
                 timeout: int = 120) -> bool:
    """Download a ScienceDirect PDF through a live browser session."""
    if dest_path.exists():
        print(f"  Already exists: {dest_path}")
        return True

    print("  [CDP] Using live ScienceDirect session; keep Chrome open.")
    try:
        client = CDPClient(port=CDP_PORT)
    except RuntimeError as e:
        print(f"  [CDP] {e}")
        return False

    article_page = None
    try:
        print(f"  [CDP] Opening https://doi.org/{doi}")
        article_page = client.open_page(f"https://doi.org/{doi}")
        article_ws = article_page["webSocketDebuggerUrl"]
        article_id = article_page.get("id", "")
        time.sleep(6)
        current_url = (CDPClient.evaluate_on_page(article_ws, "location.href") or "").lower()
        html = CDPClient.evaluate_on_page(article_ws, "document.documentElement.outerHTML") or ""
        title = (CDPClient.evaluate_on_page(article_ws, "document.title") or "").lower()

        if "/abs/" in current_url and "sciencedirect" in current_url:
            client.close_page(article_id)
            article_page = client.open_page(current_url.replace("/abs/", "/"))
            article_ws = article_page["webSocketDebuggerUrl"]
            article_id = article_page.get("id", "")
            time.sleep(6)
            current_url = (CDPClient.evaluate_on_page(article_ws, "location.href") or "").lower()
            html = CDPClient.evaluate_on_page(article_ws, "document.documentElement.outerHTML") or ""
            title = (CDPClient.evaluate_on_page(article_ws, "document.title") or "").lower()

        html_lower = (html or "").lower()
        is_error = any(kw in html_lower[:3000] for kw in ("problem providing", "cpe00001"))
        is_tdm = "tdm-reservation" in html_lower[:5000] and "pdfdownload" not in html_lower
        is_challenge = any(kw in title + html_lower[:2000]
                           for kw in ("please wait", "sign in", "login", "challenge",
                                      "cloudflare", "verify you are human"))

        if is_error or is_tdm or is_challenge:
            print("  [CDP] Elsevier verification or sign-in is needed.")
            print("  [CDP] In the opened Chrome window: finish verification, open the article,")
            print("  [CDP] click View PDF once if needed, then return here and press Enter.")
            try:
                input("  >>> Press Enter after manual verification...")
            except EOFError:
                pass
            client.close_page(article_id)
            article_page = client.open_page(f"https://doi.org/{doi}")
            article_ws = article_page["webSocketDebuggerUrl"]
            article_id = article_page.get("id", "")
            time.sleep(6)
            current_url = (CDPClient.evaluate_on_page(article_ws, "location.href") or "").lower()
            if "/abs/" in current_url and "sciencedirect" in current_url:
                client.close_page(article_id)
                article_page = client.open_page(current_url.replace("/abs/", "/"))
                article_ws = article_page["webSocketDebuggerUrl"]
                article_id = article_page.get("id", "")
                time.sleep(6)

        deadline = time.time() + 30
        pdfft_url = ""
        while time.time() < deadline:
            html = CDPClient.evaluate_on_page(
                article_ws, "document.documentElement.outerHTML") or ""
            pdfft_url = _extract_pdf_metadata_from_html(html) or ""
            if pdfft_url:
                break
            time.sleep(2)

        if not pdfft_url and pdf_url_fallback:
            pdfft_url = pdf_url_fallback
        if not pdfft_url:
            print("  [CDP] No ScienceDirect pdfDownload metadata found.")
            print("  [CDP] Keep the same Chrome session, open the article manually,")
            print("  [CDP] click View PDF once, then rerun this DOI only.")
            return False

        print(f"  [CDP] Found pdfft URL: {pdfft_url[:120]}")
        result = _cdp_fetch_and_download(
            client, article_ws, pdfft_url, dest_path, timeout=timeout)
        return result is not None
    except Exception as exc:
        print(f"  [CDP] {exc}")
        return False
    finally:
        if article_page:
            try:
                client.close_page(article_page["id"])
            except Exception:
                pass
        client.close()


# ---------------------------------------------------------------------------
# EBSCO PDF download via CDP (INFORMS papers since Jan 2026 migration)
#
# Connects to your existing Chrome (must be launched with --remote-debugging-port).
# Chrome handles proxy + cookies → CDP navigates, opens "PDF Full Text",
# captures the PDF API URL, and Python downloads it with the live cookies.
# ---------------------------------------------------------------------------

def _dechunk(body: bytes) -> bytes:
    """Decode an HTTP/1.1 chunked response body."""
    decoded = bytearray()
    offset = 0
    while True:
        line_end = body.index(b'\r\n', offset)
        size = int(body[offset:line_end].split(b';', 1)[0], 16)
        offset = line_end + 2
        if size == 0:
            return bytes(decoded)
        chunk_end = offset + size
        if body[chunk_end:chunk_end + 2] != b'\r\n':
            raise ValueError("Malformed chunked HTTP response")
        decoded.extend(body[offset:chunk_end])
        offset = chunk_end + 2


def _https_get_raw(host: str, path: str, cookies: dict,
                    retries: int = 15, base_delay: float = 3.0,
                    referer: str = "") -> bytes | None:
    """Single HTTP GET with aggressive retry + exponential backoff.

    Uses Python raw sockets (works through GFW when curl_cffi/Chrome fail).
    Each retry opens a fresh TCP+TLS connection to work around IP-based blocking.
    """
    import random as _random
    for attempt in range(retries):
        try:
            sock = __import__('socket').socket()
            sock.settimeout(30)
            ctx = __import__('ssl').create_default_context()
            ss = ctx.wrap_socket(sock, server_hostname=host)
            ss.connect((host, 443))

            cookie_str = '; '.join(f'{k}={v}' for k, v in cookies.items())
            referer_header = f'Referer: {referer}\r\n' if referer else ''
            req = (
                f'GET {path} HTTP/1.1\r\n'
                f'Host: {host}\r\n'
                f'Connection: close\r\n'
                f'Accept: application/pdf,*/*\r\n'
                f'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36\r\n'
                f'Accept-Language: zh-CN,zh;q=0.9\r\n'
                f'{referer_header}'
                f'Cookie: {cookie_str}\r\n\r\n'
            )
            ss.sendall(req.encode())
            chunks = []
            while True:
                d = ss.recv(65536)
                if not d:
                    break
                chunks.append(d)
            ss.close()

            resp = b''.join(chunks)
            header_block, body = resp.split(b'\r\n\r\n', 1)
            header_lines = header_block.split(b'\r\n')
            status = int(header_lines[0].split(b' ', 2)[1])
            headers = {}
            for line in header_lines[1:]:
                name, value = line.split(b':', 1)
                headers[name.strip().lower()] = value.strip().lower()
            if b'chunked' in headers.get(b'transfer-encoding', b''):
                body = _dechunk(body)
            content_length = headers.get(b'content-length')
            if content_length is not None and len(body) != int(content_length):
                raise ConnectionError(
                    f"Incomplete HTTP body: expected {int(content_length)}, got {len(body)}")
            if status == 200 and len(body) > 0:
                return body
            if attempt % 3 == 0 and attempt > 0:
                status_line = header_lines[0].decode(errors='replace')
                print(f"    retry {attempt+1}/{retries}: {status_line}")
        except Exception as e:
            if attempt % 4 == 0 and attempt > 0:
                print(f"    attempt {attempt+1}: {type(e).__name__}")
        # Exponential backoff with jitter
        delay = base_delay * (1.5 ** min(attempt, 8)) + _random.random() * 3
        time.sleep(delay)
    return None


def _load_cookies_dict(cookies_path: Path | None = None) -> dict:
    """Load EBSCO cookies from JSON file, return as {name: value} dict."""
    if cookies_path is None:
        cookies_path = ROOT / "data" / "ebsco_cookies.json"
    if not cookies_path.exists():
        return {}
    try:
        items = json.loads(cookies_path.read_text(encoding="utf-8"))
        return {c['name']: c['value'] for c in items if c.get('name')}
    except (json.JSONDecodeError, OSError, KeyError):
        return {}


def _get_cdp_cookies(client: CDPClient, url: str) -> dict:
    """Return Chrome cookies applicable to one URL."""
    result = client.request("Network.getCookies", {"urls": [url]}, timeout=10)
    return {cookie["name"]: cookie["value"] for cookie in result["cookies"]}


def _ebsco_load_credentials():
    """Load CARSI credentials from data/credentials.json."""
    if not CREDENTIALS_PATH.exists():
        return None, None
    try:
        cfg = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
        carsi = cfg.get("carsi", {})
        user = (carsi.get("username") or "").strip()
        pwd = (carsi.get("password") or "").strip()
        return (user, pwd) if (user and pwd) else (None, None)
    except Exception:
        return None, None


def _ebsco_auto_login(client: CDPClient) -> bool:
    """Auto-fill CARSI login form. Returns True if we reach EBSCO."""
    user, pwd = _ebsco_load_credentials()
    if not user or not pwd:
        return False

    escaped_user = json.dumps(user)
    client.eval_js(f"""(function(){{
    var el=document.querySelector('input[name="username"]');
    if(!el)return;
    var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
    s.call(el,{escaped_user});
    el.dispatchEvent(new Event('input',{{bubbles:true}}));
    el.dispatchEvent(new Event('change',{{bubbles:true}}));
}})()""")
    time.sleep(0.3)

    escaped_pwd = json.dumps(pwd)
    client.eval_js(f"""(function(){{
    var el=document.querySelector('input[name="password"]');
    if(!el)return;
    var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
    s.call(el,{escaped_pwd});
    el.dispatchEvent(new Event('input',{{bubbles:true}}));
    el.dispatchEvent(new Event('change',{{bubbles:true}}));
}})()""")
    time.sleep(0.3)

    client.eval_js("""(function(){
    var el=document.querySelector('input[name="submit"]');
    if(el){el.click();return;}
    var btns=document.querySelectorAll('button[type="submit"],input[type="submit"]');
    for(var i=0;i<btns.length;i++){btns[i].click();return;}
})()""")

    for _ in range(15):
        time.sleep(2)
        try:
            url = (client.eval_js("location.href") or "").lower()
            if "ebsco" in url:
                print("  [EBSCO] Auto-login OK")
                return True
            # Still on login page — wait or check for errors
            if client.eval_js("!!document.querySelector('input[name=\"username\"]')"):
                continue
        except Exception:
            pass
    print("  [EBSCO] Auto-login timed out")
    return False


def ebsco_download(doi: str, dest_path: Path, timeout: int = 120,
                   pdf_content_url: str = "",
                   proxy: str | None = None,
                   cookies_path: Path | None = None) -> bool:
    """Download INFORMS paper PDF via EBSCO — fully automated via CDP.

    Prerequisite:
      Chrome must be running with debug port:
        chrome --remote-debugging-port=9224
               --user-data-dir="<copy-of-your-profile>"

    Flow (automated):
      1. Navigate to EBSCO search by DOI
      2. Wait for search results (SPA, JS-loaded)
      3. Click the matching article
      4. Find & click "PDF Full Text"
      5. Capture the EBSCO fulltext/pdf or cds/retrieve API URL
      6. Export live Chrome cookies and download the PDF via raw HTTPS
    """
    import urllib.parse as _up

    if dest_path.exists():
        print(f"  Already exists: {dest_path}")
        return True

    # --- Shortcut: direct PDF URL (content.ebscohost.com) ---
    if pdf_content_url:
        cookies = _load_cookies_dict(cookies_path)
        if cookies:
            pu = _up.urlparse(pdf_content_url)
            pdf_body = _https_get_raw(pu.netloc, pu.path + '?' + pu.query, cookies, retries=8)
            if pdf_body and pdf_body[:4] == b'%PDF':
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_bytes(pdf_body)
                print(f"  OK: {len(pdf_body)//1024} KB -> {dest_path}")
                return True
        # Fall through to CDP if direct fails

    # --- CDP-based fully automated download ---
    print("  [CDP] Connecting to Chrome (port 9224)...")
    try:
        client = CDPClient(port=CDP_PORT)
    except RuntimeError as e:
        print(f"  [CDP] Cannot connect: {e}")
        print("  [CDP] Start Chrome with: chrome --remote-debugging-port=9224")
        return False

    try:
        client.request("Page.enable")

        # === Step 1: Search by DOI ===
        search_url = (
            f"https://research.ebsco.com/c/j45x2k/search/results"
            f"?q=DO+{doi}&db=asn,bsu,bth,nlebk,bwh,nfh"
        )
        print(f"  [CDP] Search: {search_url[:120]}")
        try:
            client.request("Page.navigate", {"url": search_url}, timeout=20)
        except Exception:
            pass  # SSO redirects may cause timeout — page still loads
        time.sleep(6)

        # Check if we're stuck on a login page (not just SSO redirect passing through).
        # Only trigger auto-login when there's an actual username+password form visible.
        for _ in range(3):
            r = client.request("Runtime.evaluate", {
                "expression": (
                    "window.location.href + '|||' + document.title + '|||' + "
                    "String(!!document.querySelector('input[name=\"username\"]')) + '|||' + "
                    "String(!!document.querySelector('input[name=\"password\"]'))"
                ),
                "returnByValue": True,
            }, timeout=10)
            info = r.get("result", {}).get("value", "") or ""
            parts = info.split("|||")
            url_part = parts[0] if len(parts) > 0 else info
            has_user = (parts[2] if len(parts) > 2 else "") == "true"
            has_pwd  = (parts[3] if len(parts) > 3 else "") == "true"
            on_login = has_user and has_pwd  # real login form, not SSO redirect
            if on_login:
                print("  [EBSCO] Login form detected — attempting auto-login...")
                if _ebsco_auto_login(client):
                    break
                else:
                    print("  [EBSCO] Auto-login failed — manual intervention needed")
                    print("  [EBSCO] Please log in via CARSI in the Chrome window")
                    try:
                        input("  >>> ")
                    except EOFError:
                        pass
                    time.sleep(4)
            elif "ebsco" in url_part.lower() and "login" not in url_part.lower():
                break  # already on EBSCO — session is valid
            else:
                # SSO redirect in progress, wait a bit
                time.sleep(2)

        # === Step 2: Wait for search results (SPA loads via JS) ===
        print("  [CDP] Waiting for search results...")
        detail_url = None
        for i in range(20):
            time.sleep(1.5)
            js = (
                "(function(){var a=document.querySelectorAll('a');"
                "for(var i=0;i<a.length;i++){"
                "var h=a[i].href||'';"
                "if(h.includes('search/details/'))return h;}"
                "return'';})()"
            )
            r = client.request("Runtime.evaluate", {
                "expression": js, "returnByValue": True,
            }, timeout=5)
            detail_url = r.get("result", {}).get("value", "") or ""
            if detail_url:
                print(f"  [CDP] Found article: {detail_url[:120]}")
                break

        if not detail_url:
            print("  [CDP] Search results did not load in time")
            return False

        # === Step 3: Navigate to article detail page ===
        print("  [CDP] Opening article detail page...")
        try:
            client.request("Page.navigate", {"url": detail_url}, timeout=15)
        except Exception:
            pass
        time.sleep(5)

        # Verify we're on detail page
        r = client.request("Runtime.evaluate", {
            "expression": "document.title",
            "returnByValue": True,
        }, timeout=5)
        title = r.get("result", {}).get("value", "") or ""
        print(f"  [CDP] Article: {title[:100]}")

        # === Step 4: Find PDF Full Text / viewer link ===
        print("  [CDP] Looking for PDF Full Text...")
        pdf_viewer_url = None
        for i in range(10):
            time.sleep(1)
            js = (
                "(function(){var a=document.querySelectorAll('a');"
                "for(var i=0;i<a.length;i++){"
                "var t=(a[i].textContent||'').trim();"
                "var h=a[i].href||'';"
                "if(t==='PDF Full Text'||h.includes('viewer/pdf/'))return h;"
                "}return'';})()"
            )
            r = client.request("Runtime.evaluate", {
                "expression": js, "returnByValue": True,
            }, timeout=5)
            pdf_viewer_url = r.get("result", {}).get("value", "") or ""
            if pdf_viewer_url:
                print(f"  [CDP] PDF viewer: {pdf_viewer_url[:120]}")
                break

        if not pdf_viewer_url:
            print("  [CDP] PDF Full Text link not found")
            return False

        # === Step 5: Capture the EBSCO PDF API URL via Fetch interception ===
        print("  [CDP] Fetch interception to capture PDF URL...")
        client.request("Fetch.enable", {"patterns": [
            {"urlPattern": "*ebsco*", "requestStage": "Response"},
        ]})

        # Use _send (not request) — request() calls _recv() which consumes
        # Fetch.requestPaused events while waiting for the response id.
        client._send("Runtime.evaluate", {
            "expression": f"window.location.href = {json.dumps(pdf_viewer_url)};",
            "returnByValue": False,
        })

        # Collect the PDF API URL, then download it with live Chrome cookies.
        content_url = None
        deadline = time.time() + 300
        while time.time() < deadline and content_url is None:
            try:
                client.ws.settimeout(5)
                raw = client.ws.recv()
                obj = json.loads(raw)
                if obj.get("method") == "Fetch.requestPaused":
                    params = obj.get("params", {})
                    rid = params.get("requestId", "")
                    req_url = params.get("request", {}).get("url", "")
                    status = params.get("responseStatusCode", 0)
                    print(f"  [CDP] Fetch: {status} {req_url[:130]}")
                    is_pdf_api = "/fulltext/pdf" in req_url or "/cds/retrieve" in req_url
                    if is_pdf_api and status == 200:
                        content_url = req_url
                        print(f"  [CDP] Captured PDF API URL, downloading via raw socket...")
                    # Use _send — request() would consume subsequent Fetch events
                    client._send("Fetch.continueResponse", {"requestId": rid})
                    if content_url:
                        break
            except Exception:
                pass  # WebSocket timeout, decode error, etc. — keep polling

        if not content_url:
            print("  [CDP] Could not capture EBSCO PDF API URL")
            return False

        client.request("Fetch.disable", timeout=10)
        client.request("Page.stopLoading", timeout=10)

        # === Step 6: Download PDF from captured API URL via raw socket ===
        # EBSCO's 2026 PDF endpoint returns JSON containing a signed
        # content.ebscohost.com/cds/retrieve URL when an intent is supplied.
        # Older behavior returned the PDF bytes directly, so keep both paths.
        pu = _up.urlparse(content_url)
        if "/fulltext/pdf" in pu.path and "intent=" not in pu.query:
            sep = "&" if pu.query else ""
            content_url = _up.urlunparse((
                pu.scheme, pu.netloc, pu.path, pu.params,
                pu.query + sep + "intent=view", pu.fragment))
            pu = _up.urlparse(content_url)

        cookies = _get_cdp_cookies(client, content_url)
        request_path = pu.path + (f'?{pu.query}' if pu.query else '')
        pdf_body = _https_get_raw(
            pu.netloc, request_path, cookies, retries=5, referer=pdf_viewer_url)
        if pdf_body and pdf_body[:4] != b"%PDF":
            try:
                payload = json.loads(pdf_body.decode("utf-8"))
                signed_url = payload.get("url", "")
                if signed_url:
                    signed = _up.urlparse(signed_url)
                    signed_path = signed.path + (f'?{signed.query}' if signed.query else '')
                    pdf_body = _https_get_raw(
                        signed.netloc, signed_path, {}, retries=5,
                        referer=content_url)
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                pass

        if pdf_body and pdf_body[:4] == b"%PDF":
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(pdf_body)
            print(f"  OK: {len(pdf_body)//1024} KB -> {dest_path}")
            return True

        print("  [CDP] PDF download failed")
        return False

    except Exception as exc:
        print(f"  [CDP] Error: {exc}")
        return False
    finally:
        client.close()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Download a single paper PDF from target journal")
    p.add_argument("--doi", required=True, default="", help="Paper DOI")
    p.add_argument("--title", required=True, default="", help="Paper title")
    p.add_argument("--journal", default="", help="Journal name (optional — publisher auto-detected from DOI)")
    p.add_argument("--download-dir", default="", help="PDF download directory (default: data/pdfs/)")
    p.add_argument("--cookies", default="", help="Path to EBSCO cookies JSON (default: data/ebsco_cookies.json)")
    p.add_argument("--proxy", default="", help="HTTP proxy for EBSCO access (e.g. http://127.0.0.1:7890)")
    p.add_argument("--pdf-url", default="", help="Direct PDF URL from EBSCO (content.ebscohost.com/cds/retrieve?content=...)")
    return p.parse_args()


def main():
    args = parse_args()

    doi = args.doi.strip()
    title = args.title.strip()
    journal = args.journal.strip()

    publisher = detect_publisher_from_doi(doi) if doi else ""

    cfg_dir = load_config().get("downloadDir", "")
    if args.download_dir:
        download_dir = Path(args.download_dir).resolve()
    elif cfg_dir:
        download_dir = (ROOT / cfg_dir).resolve()
    else:
        download_dir = (ROOT / "data" / "pdfs").resolve()
    download_dir.mkdir(parents=True, exist_ok=True)

    print(f"DOI: {doi}")
    print(f"Title: {title[:100]}")
    if journal:
        print(f"Journal: {journal}")
    if publisher:
        print(f"Publisher: {publisher}")

    # Enrich from push_queue if available
    full_paper = {"Key": f"doi:{doi}", "Title": title}
    try:
        queue = load_push_queue()
        for p in queue:
            if extract_doi(p.get("Key", "")) == doi:
                full_paper = p
                print(f"Found in push_queue (Score: {p.get('Score', '?')})")
                break
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    if publisher == "sciencedirect":
        print("Downloading from ScienceDirect via CDP ...")
        if not _ensure_chrome():
            print("Cannot start Chrome for CDP download")
            sys.exit(1)
        filename = build_filename(full_paper)
        dest = download_dir / filename
        ok = cdp_download(doi, dest)
    elif publisher == "informs":
        print("Downloading from INFORMS via EBSCO (2026 migration) ...")
        if not _ensure_chrome():
            print("Cannot start Chrome for CDP download")
            sys.exit(1)
        filename = build_filename(full_paper)
        dest = download_dir / filename
        proxy = args.proxy.strip() if args.proxy.strip() else ""
        cookies_path = Path(args.cookies.strip()) if args.cookies else None
        pdf_url = args.pdf_url.strip() if args.pdf_url else ""
        ok = ebsco_download(doi, dest, proxy=proxy, cookies_path=cookies_path,
                            pdf_content_url=pdf_url)
    elif publisher in ("springer", "wiley", "nature"):
        print(f"Publisher '{publisher}' not yet supported (coming soon)")
        sys.exit(1)
    else:
        if journal:
            print(f"Journal '{journal}' — publisher not recognized")
        else:
            print("Could not detect publisher from DOI or --journal")
        print("Currently supported: ScienceDirect, INFORMS PubsOnLine")
        sys.exit(1)

    if ok:
        print(f"Download: OK ({dest})")
    else:
        print("Download: FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()

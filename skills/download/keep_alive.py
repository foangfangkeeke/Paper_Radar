#!/usr/bin/env python3
"""Keep publisher and WoS sessions alive with end-to-end health checks.
Runs daily before dawn (3-7 AM) to prevent session expiry. Chrome is started
with the shared persistent profile for each phase and closed afterward.

If redirected to CARSI/SSO login, auto-fills credentials from
data/credentials.json.

Logs to data/logs/keep_alive_YYYYMMDD.log (one file per day).
Health-check PDFs are kept under data/healthchecks/pdfs.
WoS health-check exports are created under data/healthchecks/wos_exports and
deleted after validation.
"""

import argparse, sys, time, json, random, logging, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills"))
from cdp_client import CDPClient
from download.paper_downloader import (
    cdp_download,
    ebsco_download,
)

FETCH_MERGE_DIR = ROOT / "skills" / "fetch-merge"
sys.path.insert(0, str(FETCH_MERGE_DIR))
import wos_cdp_workflow as wos

CDP_PORT = 9224
WOS_CDP_PORT = 9225
CHROME_PATHS = [
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]
CREDENTIALS_PATH = ROOT / "data" / "credentials.json"
LOG_DIR = ROOT / "data" / "logs"
HEALTHCHECK_DIR = ROOT / "data" / "healthchecks"
HEALTHCHECK_PDF_DIR = HEALTHCHECK_DIR / "pdfs"
HEALTHCHECK_WOS_DIR = HEALTHCHECK_DIR / "wos_exports"
EBSCO_URL = "https://research.ebsco.com/c/j45x2k/search/advanced"
SCIENCEDIRECT_URL = "https://www.sciencedirect.com/"
EBSCO_HEALTHCHECK_DOI = "10.1287/ijoc.2023.0255"
EBSCO_HEALTHCHECK_TITLE = "A Graph-Based Approach for Relating Integer Programs"
EBSCO_HEALTHCHECK_PREFIX = "A_Graph-Based_Approach_for_Relating_Integer_Programs_keepalive"
SD_HEALTHCHECK_DOI = "10.1016/j.tre.2026.104709"
SD_HEALTHCHECK_TITLE = (
    "Integrated and shared charging optimization of electric buses and shared "
    "micromobility incorporating solar photovoltaic"
)
SD_HEALTHCHECK_PREFIX = "Integrated_shared_charging_optimization_keepalive"
WOS_HEALTHCHECK_START_DATE = "2026-06-01"
WOS_HEALTHCHECK_END_DATE = "2026-07-01"

_logger = None


def _get_logger():
    """Lazy-init file + stderr logger. One log file per day."""
    global _logger
    if _logger is not None:
        return _logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"keep_alive_{datetime.date.today().strftime('%Y%m%d')}.log"

    _logger = logging.getLogger("keep_alive")
    _logger.setLevel(logging.DEBUG)
    _logger.handlers.clear()

    # File handler — append so multiple wake-ups on same day don't clobber
    fh = logging.FileHandler(str(log_file), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    _logger.addHandler(fh)

    # Also print to stderr so Windows Task Scheduler captures it
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.DEBUG)
    sh.setFormatter(logging.Formatter(
        "%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    _logger.addHandler(sh)

    return _logger


log = lambda msg, *args: _get_logger().info(msg, *args)


def _run_independent_stage(name: str, action) -> bool:
    """Run one health-check stage without blocking later stages on failure."""
    log("--- %s stage ---", name)
    try:
        ok = bool(action())
    except Exception:
        _get_logger().exception("%s stage raised an exception", name)
        ok = False
    log("%s stage: %s", name, "OK" if ok else "FAILED")
    return ok


def _load_credentials():
    """Load CARSI credentials from data/credentials.json."""
    if not CREDENTIALS_PATH.exists():
        log(f"Credentials file not found: {CREDENTIALS_PATH}")
        return None, None

    try:
        cfg = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
        carsi = cfg.get("carsi", {})
        user = (carsi.get("username") or "").strip()
        pwd = (carsi.get("password") or "").strip()
        if user and pwd:
            return user, pwd
        log("Credentials not configured (username/password empty)")
        return None, None
    except Exception as e:
        log(f"Failed to load credentials: {e}")
        return None, None


def _ensure_chrome() -> bool:
    """Start Chrome with the publisher persistent profile if needed."""
    import urllib.request, subprocess

    try:
        urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=3)
        log("Chrome already running on port %d", CDP_PORT)
        return True
    except Exception:
        log("Chrome not running — attempting to start...")

    profile = ROOT / "data" / "chrome_profile"
    if not profile.exists():
        log(f"Profile not found: {profile}")
        return False

    for p in CHROME_PATHS:
        if Path(p).exists():
            chrome = p
            break
    else:
        log("Chrome.exe not found")
        return False

    subprocess.Popen(
        [chrome, f"--remote-debugging-port={CDP_PORT}",
         f"--user-data-dir={profile}", "--no-first-run",
         "--no-default-browser-check"],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    for i in range(20):
        time.sleep(1)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=3)
            log("Chrome started after %ds", i + 1)
            return True
        except Exception:
            pass
    log("Chrome failed to start within 20s")
    return False


def _on_login_page(client: CDPClient) -> bool:
    """Check whether the current page is a CARSI/CAS/SSO login form."""
    try:
        url = (client.eval_js("location.href") or "").lower()
        title = (client.eval_js("document.title") or "").lower()

        if any(k in url for k in ("sso.", "/login", "carsi", "cas/login")):
            return True
        if any(k in title for k in ("sign in", "login", "统一身份认证", "身份认证", "单点登录")):
            return True

        # Also check for login form elements
        has_user = client.eval_js(
            '!!document.querySelector(\'input[name="username"]\')'
        )
        has_pwd = client.eval_js(
            '!!document.querySelector(\'input[name="password"]\')'
        )
        if has_user == "true" or has_user is True:
            if has_pwd == "true" or has_pwd is True:
                return True
    except Exception:
        pass
    return False


def _is_shibboleth_page(client: CDPClient) -> bool:
    """Check whether we're on a Shibboleth IdP intermediary page (not EBSCO yet)."""
    try:
        url = (client.eval_js("location.href") or "").lower()
        # Shibboleth IdP pages — still in the auth redirect chain
        if "idp." in url:
            return True
        body = (client.eval_js("document.body ? document.body.innerText : ''") or "").lower()
        if "problem finding your session" in body:
            return True
        if "shibboleth" in body:
            return True
    except Exception:
        pass
    return False


def _auto_login(client: CDPClient, username: str, password: str) -> bool:
    """Fill and submit the CAS/SSO login form. Returns True ONLY if we reach EBSCO."""
    log("Auto-filling login form...")

    # Fill username
    escaped_user = json.dumps(username)
    client.eval_js(f"""
(function() {{
    var el = document.querySelector('input[name="username"]');
    if (!el) return;
    var setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    setter.call(el, {escaped_user});
    el.dispatchEvent(new Event('input', {{bubbles: true}}));
    el.dispatchEvent(new Event('change', {{bubbles: true}}));
}})()
""")
    time.sleep(0.3)

    # Fill password
    escaped_pwd = json.dumps(password)
    client.eval_js(f"""
(function() {{
    var el = document.querySelector('input[name="password"]');
    if (!el) return;
    var setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    setter.call(el, {escaped_pwd});
    el.dispatchEvent(new Event('input', {{bubbles: true}}));
    el.dispatchEvent(new Event('change', {{bubbles: true}}));
}})()
""")
    time.sleep(0.3)

    # Click submit
    client.eval_js("""
(function() {
    var el = document.querySelector('input[name="submit"]');
    if (el) { el.click(); return 'clicked'; }
    // Fallback: find any submit button
    var btns = document.querySelectorAll('button[type="submit"], input[type="submit"]');
    for (var i = 0; i < btns.length; i++) {
        btns[i].click(); return 'fallback-click';
    }
    return 'not-found';
})()
""")

    # Wait for the full redirect chain to complete: CAS → Shibboleth → EBSCO
    # Must NOT return early on intermediate pages (idp.*, Shibboleth)
    for _ in range(15):
        time.sleep(2)
        try:
            url = (client.eval_js("location.href") or "").lower()
            if "ebsco" in url:
                log("Login OK — reached EBSCO")
                return True
            if _on_login_page(client):
                # Still on login page — wrong password or form validation
                log(f"Still on login page (URL: {url[:80]})")
                continue
            if _is_shibboleth_page(client):
                # On Shibboleth IdP — could be processing or errored
                body = (client.eval_js("document.body ? document.body.innerText : ''") or "").lower()
                if "problem finding your session" in body:
                    log("Shibboleth session expired — need fresh redirect chain")
                    return False
                # Otherwise, Shibboleth is still processing, keep waiting
                log("Waiting for Shibboleth redirect...")
                continue
            # Unknown page — might be a transitional page, keep waiting
            log(f"Waiting... (URL: {url[:80]})")
        except Exception:
            pass

    log("Login redirect timed out — did not reach EBSCO")
    return False


def _retry_from_fresh_cas(client: CDPClient, ebsco_url: str) -> bool:
    """Navigate to EBSCO again with fresh CAS cookies (after successful login).
    The CAS login should have set cookies, so this time it should go straight through."""
    log("Retrying with fresh CAS cookies...")
    try:
        client.request("Page.navigate", {"url": ebsco_url}, timeout=30)
        time.sleep(8)
        url = (client.eval_js("location.href") or "").lower()
        if "ebsco" in url:
            log("Retry OK — reached EBSCO")
            return True
        if _on_login_page(client):
            log("Still redirected to login — session cookies may be invalid")
            return False
        if _is_shibboleth_page(client):
            log("Still on Shibboleth page")
            return False
        # Unknown page, give it more time
        for _ in range(5):
            time.sleep(2)
            url = (client.eval_js("location.href") or "").lower()
            if "ebsco" in url:
                log("Retry OK — reached EBSCO")
                return True
        log(f"Retry ended at: {url[:80]}")
        return "ebsco" in url
    except Exception as e:
        log(f"Retry error: {e}")
        return False


def _healthcheck_download_dir() -> Path:
    """Return the dedicated health-check PDF directory."""
    HEALTHCHECK_PDF_DIR.mkdir(parents=True, exist_ok=True)
    return HEALTHCHECK_PDF_DIR


def _remove_previous_healthchecks(
    download_dir: Path, prefix: str, current: Path
) -> None:
    """Delete older successful health-check PDFs for one publisher."""
    previous_files = set(download_dir.glob(f"{prefix}_*.pdf"))
    previous_files.update(download_dir.glob(f"*_{prefix}.pdf"))
    for previous in previous_files:
        if previous != current:
            previous.unlink()
            log("Deleted previous health-check PDF: %s", previous.name)


def _download_ebsco_healthcheck_pdf() -> bool:
    """Download one known INFORMS paper and retain only the newest check PDF."""
    download_dir = _healthcheck_download_dir()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = download_dir / f"{timestamp}_{EBSCO_HEALTHCHECK_PREFIX}.pdf"
    log("Downloading EBSCO health-check paper: %s", EBSCO_HEALTHCHECK_TITLE)
    log("EBSCO health-check destination: %s", destination.name)

    if not ebsco_download(EBSCO_HEALTHCHECK_DOI, destination):
        log("EBSCO health-check PDF download failed")
        return False

    _remove_previous_healthchecks(
        download_dir, EBSCO_HEALTHCHECK_PREFIX, destination
    )
    log("EBSCO health-check PDF download OK: %s", destination)
    return True


def _download_sciencedirect_healthcheck_pdf() -> bool:
    """Download one known ScienceDirect paper and retain only the newest check PDF."""
    download_dir = _healthcheck_download_dir()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = download_dir / f"{timestamp}_{SD_HEALTHCHECK_PREFIX}.pdf"
    log("Downloading ScienceDirect health-check paper: %s", SD_HEALTHCHECK_TITLE)
    log("ScienceDirect health-check destination: %s", destination.name)

    if not cdp_download(SD_HEALTHCHECK_DOI, destination):
        log("ScienceDirect health-check PDF download failed")
        return False

    _remove_previous_healthchecks(
        download_dir, SD_HEALTHCHECK_PREFIX, destination
    )
    log("ScienceDirect health-check PDF download OK: %s", destination)
    return True


def _run_wos_healthcheck() -> bool:
    """Run one WoS search/export check and remove its exported TXT."""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    export_dir = HEALTHCHECK_WOS_DIR / f"keep_alive_wos_{timestamp}"
    export_dir.mkdir(parents=True)
    wos.EXPORT_DIR = export_dir
    wos.PORT = WOS_CDP_PORT

    client = None
    try:
        log("--- Web of Science keep-alive ---")
        log(
            "WoS health-check date range: %s to %s",
            WOS_HEALTHCHECK_START_DATE,
            WOS_HEALTHCHECK_END_DATE,
        )
        wos.launch_chrome()
        client = CDPClient(port=wos.PORT)

        cfg = json.loads(wos.CONFIG_PATH.read_text(encoding="utf-8"))
        keyword = cfg["keywords"][0]
        exported = wos.run_single_search(
            client,
            keyword,
            cfg["topJournals"],
            WOS_HEALTHCHECK_START_DATE,
            WOS_HEALTHCHECK_END_DATE,
        )
        if len(exported) != 1:
            log("WoS health-check failed: expected 1 export, got %d", len(exported))
            return False

        export_path = Path(exported[0])
        records = wos.parse_wos_plain_text(export_path)
        if not records:
            log("WoS health-check failed: exported file contained no records")
            return False

        log(
            "WoS health-check export OK: %s (%d bytes, %d records)",
            export_path.name,
            export_path.stat().st_size,
            len(records),
        )
        return True
    finally:
        if client is not None:
            client.close()
        for generated in export_dir.iterdir():
            generated.unlink()
        export_dir.rmdir()
        log("Deleted WoS health-check export directory: %s", export_dir.name)


def _close_browser(port: int = CDP_PORT) -> bool:
    """Close the Chrome instance attached to the CDP port."""
    import urllib.error
    import urllib.request

    try:
        client = CDPClient(port=port)
    except RuntimeError:
        log("Chrome already closed")
        return True

    try:
        client._send("Browser.close")
    finally:
        try:
            client.close()
        except Exception:
            pass

    for _ in range(10):
        time.sleep(1)
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json", timeout=1
            )
        except (urllib.error.URLError, OSError):
            log("Chrome closed after keep-alive")
            return True

    log("Chrome did not close within 10s")
    return False


def main(*, no_delay: bool = False) -> bool:
    log("=" * 50)
    log("Keep-alive started")

    # Random delay 0-4 hours so actual request lands between 3:00-7:00
    if no_delay:
        log("Random delay disabled for manual run")
    else:
        delay = random.randint(0, 4 * 3600)
        log("Sleeping %d min before keep-alive...", delay // 60)
        time.sleep(delay)

    log("Refreshing EBSCO session...")

    if not _ensure_chrome():
        log("FATAL: Cannot start Chrome — skipping keep-alive")
        return False

    try:
        client = CDPClient(port=CDP_PORT)
    except RuntimeError as e:
        log(f"FATAL: CDP error: {e}")
        return False

    result = "UNKNOWN"

    try:
        client.request("Page.enable")
        # Navigate to EBSCO search page
        client.request("Page.navigate", {"url": EBSCO_URL}, timeout=30)
        time.sleep(8)

        # Check if redirected to login page
        if _on_login_page(client):
            log("CARSI/SSO login page detected")

            username, password = _load_credentials()
            if username and password:
                logged_in = _auto_login(client, username, password)

                if not logged_in and _is_shibboleth_page(client):
                    # CAS login succeeded but Shibboleth session expired.
                    # CAS cookies are now set — retry from fresh to get a new
                    # Shibboleth conversation that uses the valid CAS session.
                    log("CAS login succeeded, Shibboleth session expired — retrying from fresh")
                    if not _retry_from_fresh_cas(client, EBSCO_URL):
                        log("Retry also failed — may need manual intervention")
                        result = "FAILED"
                    else:
                        result = "OK"
                elif not logged_in:
                    log("Auto-login failed — check credentials or login page changes")
                    result = "FAILED"
                else:
                    result = "OK"
            else:
                log("No credentials configured — manual login required")
                result = "NO_CREDENTIALS"
        else:
            log("Not on login page — session may be still valid")
            result = "OK"

        # Verify
        title = client.eval_js("document.title") or ""
        url = client.eval_js("location.href") or ""
        log(f"Final page: {title[:80]}")
        log(f"Final URL:  {url[:120]}")

        if any(s in title.lower() for s in ("sign in", "login", "统一身份认证")):
            log("RESULT: FAILED — still on login page, auto-login may have failed")
            result = "FAILED"
        elif "ebsco" in url.lower():
            log("RESULT: OK — session refreshed")
            result = "OK"
        else:
            log(f"RESULT: {result} (unexpected page, but not login)")

        # --- Phase 2: ScienceDirect keep-alive ---
        # Visiting ScienceDirect with the same browser profile prevents
        # Elsevier from flagging the browser as a bot (no history → suspicious).
        # If the user has institutional access (IP/VPN), this also keeps
        # any ScienceDirect session cookies alive.
        log("--- ScienceDirect keep-alive ---")
        try:
            client.request("Page.navigate", {"url": SCIENCEDIRECT_URL}, timeout=30)
            time.sleep(6)
            sd_url = (client.eval_js("location.href") or "").lower()
            sd_title = (client.eval_js("document.title") or "")[:80]
            log(f"ScienceDirect final: {sd_title}")
            log(f"ScienceDirect URL:   {sd_url[:120]}")
            if "sciencedirect" in sd_url:
                log("ScienceDirect: OK")
            elif any(s in sd_url for s in ("login", "signin", "sso", "idp", "carsi")):
                log("ScienceDirect: redirected to login (institutional access may be needed)")
            else:
                log(f"ScienceDirect: visited (unexpected URL but not login)")
        except Exception as e:
            log(f"ScienceDirect: error — {e}")

    except Exception as e:
        log(f"FATAL: {e}")
        result = "ERROR"
    finally:
        client.close()

    ebsco_download_ok = False
    sd_download_ok = False
    try:
        ebsco_download_ok = _run_independent_stage(
            "EBSCO",
            lambda: result == "OK" and _download_ebsco_healthcheck_pdf(),
        )
        sd_download_ok = _run_independent_stage(
            "ScienceDirect",
            _download_sciencedirect_healthcheck_pdf,
        )
    finally:
        publisher_browser_closed = _close_browser()

    try:
        wos_ok = _run_independent_stage("Web of Science", _run_wos_healthcheck)
    finally:
        wos_browser_closed = _close_browser(WOS_CDP_PORT)

    final_ok = (
        result == "OK"
        and ebsco_download_ok
        and sd_download_ok
        and publisher_browser_closed
        and wos_ok
        and wos_browser_closed
    )
    log(
        "Keep-alive finished: session=%s ebsco_download=%s sd_download=%s "
        "publisher_browser_closed=%s wos_export=%s wos_browser_closed=%s",
        result,
        "OK" if ebsco_download_ok else "FAILED",
        "OK" if sd_download_ok else "FAILED",
        "OK" if publisher_browser_closed else "FAILED",
        "OK" if wos_ok else "FAILED",
        "OK" if wos_browser_closed else "FAILED",
    )
    log("=" * 50)
    return final_ok


def parse_args():
    parser = argparse.ArgumentParser(
        description="Refresh publisher/WoS sessions and verify downloads/exports"
    )
    parser.add_argument(
        "--no-delay",
        action="store_true",
        help="Run immediately instead of applying the scheduled random delay",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(0 if main(no_delay=args.no_delay) else 1)

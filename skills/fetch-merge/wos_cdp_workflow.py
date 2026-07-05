#!/usr/bin/env python3
"""
Complete WoS CDP Workflow: Search + Batch Export with Abstracts.

Performs 4 searches (one per keyword group from paper-watch.config.json),
each combined with 18 target journals and date range 2023-2026.
Exports results via WoS Plain Text File with Author, Title, Source, Abstract.

Usage:
  python skills/fetch-merge/wos_cdp_workflow.py              # Full workflow
  python skills/fetch-merge/wos_cdp_workflow.py --test       # Single test search
  python skills/fetch-merge/wos_cdp_workflow.py --launch     # Just launch Chrome
"""

from __future__ import annotations

import datetime as dt, json, sys, time, os, glob, shutil, socket, subprocess
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE / "skills"))

from merge_exports import parse_wos_plain_text
from cdp_client import CDPClient

# ============================================================================
# Configuration
# ============================================================================

PORT = 9224
CHROME_PATHS = [
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]
WOS_BASIC_SEARCH = "https://webofscience.clarivate.cn/wos/alldb/basic-search"
CONFIG_PATH = WORKSPACE / "skills" / "fetch-merge" / "paper-watch.config.json"
CREDENTIALS_PATH = WORKSPACE / "data" / "credentials.json"
DATA_DIR = WORKSPACE / "data"
EXPORT_DIR = DATA_DIR / "source_exports" / "runs" / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
CHROME_PROFILE = str(DATA_DIR / "chrome_profile")  # persistent cookies, verify once

DEFAULT_START_DATE = "2023-01-01"
DEFAULT_END_DATE = dt.date.today().isoformat()


# ============================================================================
# CARSI / CAS auto-login (adapted from keep_alive.py)
# ============================================================================

def _load_credentials():
    """Load CARSI credentials from data/credentials.json."""
    if not CREDENTIALS_PATH.exists():
        return None, None
    try:
        cfg = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
        carsi = cfg.get("carsi", {})
        user = (carsi.get("username") or "").strip()
        pwd = (carsi.get("password") or "").strip()
        if user and pwd:
            return user, pwd
        return None, None
    except Exception:
        return None, None


def _on_login_page(client: CDPClient) -> bool:
    """Check whether the current page is a CARSI/CAS/SSO login form."""
    try:
        url = (client.eval_js("location.href") or "").lower()
        title = (client.eval_js("document.title") or "").lower()
        if any(k in url for k in ("sso.", "/login", "carsi", "cas/login")):
            return True
        if any(k in title for k in ("sign in", "login", "统一身份认证", "身份认证", "单点登录")):
            return True
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


def _auto_login_wos(client: CDPClient, username: str, password: str) -> bool:
    """Fill and submit CAS/SSO login form. Returns True if we reach WoS."""
    print("  Auto-filling login form...")

    # Fill username (use HTMLInputElement setter to bypass React bindings)
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

    # Click submit button
    client.eval_js("""
(function() {
    var el = document.querySelector('input[name="submit"]');
    if (el) { el.click(); return 'clicked'; }
    var btns = document.querySelectorAll('button[type="submit"], input[type="submit"]');
    for (var i = 0; i < btns.length; i++) {
        btns[i].click(); return 'fallback-click';
    }
    return 'not-found';
})()
""")

    # Wait for redirect: CAS → WoS
    for _ in range(15):
        time.sleep(2)
        try:
            url = (client.eval_js("location.href") or "").lower()
            if "webofscience" in url:
                print("  Login OK — reached WoS")
                return True
            if _on_login_page(client):
                print("  Still on login page — wrong credentials?")
                continue
            print(f"  Waiting... (URL: {url[:80]})")
        except Exception:
            pass

    print("  Login redirect timed out — did not reach WoS")
    return False


def _on_wos_logged_out(client: CDPClient) -> bool:
    """Check if WoS page shows 'Sign In' button (not authenticated)."""
    signin = client.eval_js('''
(function() {
    var btns = document.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) {
        var t = (btns[i].textContent || '').trim().toLowerCase();
        if (t === 'sign in') return true;
    }
    return false;
})()
''')
    return signin == "true" or signin is True


def _click_wos_sign_in(client: CDPClient):
    """Click the WoS 'Sign In' button to trigger CARSI redirect."""
    client.eval_js('''
(function() {
    var btns = document.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) {
        if ((btns[i].textContent || '').trim().toLowerCase() === 'sign in') {
            btns[i].click();
            return;
        }
    }
})()
''')


# ============================================================================
# Chrome Lifecycle
# ============================================================================

def _find_chrome_exe() -> str | None:
    for p in CHROME_PATHS:
        if Path(p).exists():
            return p
    return None


def launch_chrome():
    """Launch Chrome with persistent profile and debug port."""
    s = socket.socket()
    try:
        s.connect(('127.0.0.1', PORT))
        s.close()
        print('  Chrome already running on port', PORT)
        return
    except OSError:
        pass

    chrome = _find_chrome_exe()
    if not chrome:
        raise RuntimeError('Chrome.exe not found')

    print('  Launching Chrome...')
    subprocess.Popen([
        chrome,
        f'--remote-debugging-port={PORT}',
        '--remote-allow-origins=http://localhost',
        '--disable-notifications',
        f'--user-data-dir={CHROME_PROFILE}',
        'about:blank',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + 20
    while time.time() < deadline:
        time.sleep(0.5)
        s = socket.socket()
        try:
            s.connect(('127.0.0.1', PORT))
            s.close()
            print('  Chrome ready')
            return
        except OSError:
            pass
    raise RuntimeError('Chrome did not start')


# ============================================================================
# Consent & Helpers
# ============================================================================

def handle_consent(client: CDPClient):
    """Handle Cross Border Data Transfer and Cookie consent dialogs."""
    body_text = (client.eval_js('(document.body?.innerText || "").substring(0, 500)') or '')

    if 'Cross Border' in body_text:
        client.eval_js('''
        (function() {
            var cbs = document.querySelectorAll('input[type="checkbox"]');
            for (var i = 0; i < cbs.length; i++) {
                if (!cbs[i].checked) {
                    cbs[i].click();
                    cbs[i].dispatchEvent(new Event('change', {bubbles: true}));
                }
            }
            var btns = document.querySelectorAll('button');
            for (var j = 0; j < btns.length; j++) {
                if ((btns[j].textContent || '').trim().indexOf('Confirm') >= 0) {
                    btns[j].click(); return;
                }
            }
        })()
        ''')
        time.sleep(1.5)

    client.eval_js('''
    (function() {
        var ot = document.querySelector('#onetrust-consent-sdk');
        if (ot) ot.remove();
        document.body.style.overflow = '';
    })()
    ''')
    time.sleep(0.3)


def click_button_by_text(client: CDPClient, text: str, exact: bool = False) -> str:
    """Click a button by its visible text."""
    escaped = json.dumps(text)
    cond = f't === {escaped}' if exact else f't.indexOf({escaped}) >= 0'
    return client.eval_js(f'''
(function() {{
    var btns = document.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) {{
        var t = (btns[i].textContent || '').trim();
        if ({cond}) {{
            btns[i].click();
            return 'clicked: ' + t.substring(0, 60);
        }}
    }}
    return 'not found';
}})()
''')


def click_search_button(client: CDPClient) -> str:
    """Click the actual search button (has class 'search')."""
    return client.eval_js('''
(function() {
    var btns = document.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) {
        var t = (btns[i].textContent || '').trim();
        var cls = (btns[i].className || '');
        if (cls.indexOf('search') >= 0 && t.indexOf('Search') >= 0) {
            btns[i].click();
            return 'clicked: ' + t;
        }
    }
    return 'not found';
})()
''')


# ============================================================================
# Search Form Setup
# ============================================================================

def setup_search_form(client: CDPClient):
    """Reset form and add rows for journal + date fields. Result: 3 rows: Topic, Publication/Source Titles, Publication Date."""
    # Click Clear to reset
    click_button_by_text(client, 'Clear')
    time.sleep(1.5)

    # Add row for journal
    click_button_by_text(client, 'Add row', exact=True)
    time.sleep(1.5)
    _change_row_field(client, 1, 'Publication/Source Titles')

    # Add row for date
    click_button_by_text(client, 'Add row', exact=True)
    time.sleep(1.5)
    _change_row_field(client, 1, 'Publication Date')


def _change_row_field(client: CDPClient, row_index: int, field_name: str):
    """Click the row_index-th 'Topic' dropdown (0-based), then select field_name."""
    escaped = json.dumps(field_name)
    # Click the Nth Topic dropdown
    client.eval_js(f'''
(function() {{
    var btns = document.querySelectorAll('button');
    var count = 0;
    for (var j = 0; j < btns.length; j++) {{
        var t = (btns[j].textContent || '').trim();
        if (t === 'Topic') {{
            if (count === {row_index}) {{
                btns[j].click(); return;
            }}
            count++;
        }}
    }}
}})()
''')
    time.sleep(1)

    # Select the field from dropdown
    client.eval_js(f'''
(function() {{
    var opts = document.querySelectorAll('[role="option"], .mat-mdc-option, mat-option');
    for (var i = 0; i < opts.length; i++) {{
        if ((opts[i].textContent || '').trim().indexOf({escaped}) >= 0) {{
            opts[i].click(); return;
        }}
    }}
}})()
''')
    time.sleep(0.5)


def fill_topic_field(client: CDPClient, query: str):
    """Fill the Topic input (#search-option-0). The element IS an <input>."""
    _fill_input_direct(client, '#search-option-0', query)


def fill_journal_field(client: CDPClient, journals: list[str]):
    """Fill the Publication/Source Titles input (#search-option-1)."""
    journal_query = ' OR '.join(f'"{j.strip()}"' for j in journals if j.strip())
    _fill_input_direct(client, '#search-option-1', journal_query)


def fill_date_range(client: CDPClient, start: str = DEFAULT_START_DATE, end: str = DEFAULT_END_DATE):
    """Fill the Publication Date row (two mat-input fields for from/to)."""
    client.eval_js(f'''
(function() {{
    var inputs = document.querySelectorAll('input:not([type="hidden"])');
    var dateInputs = [];
    for (var i = 0; i < inputs.length; i++) {{
        // mat-input-X fields that are NOT search-option-X
        if (inputs[i].id.indexOf('search-option') < 0 &&
            inputs[i].id.indexOf('mat-input') >= 0) {{
            dateInputs.push(inputs[i]);
        }}
    }}
    if (dateInputs.length < 2) return;

    var setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;

    setter.call(dateInputs[0], {json.dumps(start)});
    dateInputs[0].dispatchEvent(new Event('input', {{bubbles: true}}));
    dateInputs[0].dispatchEvent(new Event('change', {{bubbles: true}}));
    dateInputs[0].dispatchEvent(new FocusEvent('blur', {{bubbles: true}}));

    setter.call(dateInputs[1], {json.dumps(end)});
    dateInputs[1].dispatchEvent(new Event('input', {{bubbles: true}}));
    dateInputs[1].dispatchEvent(new Event('change', {{bubbles: true}}));
    dateInputs[1].dispatchEvent(new FocusEvent('blur', {{bubbles: true}}));
}})()
''')
    time.sleep(0.3)


def _fill_input_direct(client: CDPClient, selector: str, text: str):
    """Fill an input element directly. selector must point to the <input> itself."""
    escaped = json.dumps(text)
    client.eval_js(f'''
(function() {{
    var el = document.querySelector({json.dumps(selector)});
    if (!el) return;
    var setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    setter.call(el, {escaped});
    el.dispatchEvent(new Event('input', {{bubbles: true}}));
    el.dispatchEvent(new Event('change', {{bubbles: true}}));
    el.dispatchEvent(new FocusEvent('blur', {{bubbles: true}}));
}})()
''')
    time.sleep(0.3)


# ============================================================================
# Search
# ============================================================================

def submit_search(client: CDPClient) -> bool:
    """Submit the search form and wait for results.
    Returns True if search was submitted (URL changed from basic-search)."""
    click_search_button(client)
    time.sleep(8)

    url = client.eval_js('location.href') or ''
    # Chinese WoS result pages may not contain 'summary'; check we left basic-search
    if 'summary' in url:
        return True
    if 'basic-search' not in url and 'login' not in url:
        return True
    return False


def get_result_count(client: CDPClient) -> int:
    """Extract result count from the search results page."""
    count_str = client.eval_js('''
(function() {
    var body = (document.body || {}).innerText || '';
    var m = body.match(/(\\d[\\d,]*)\\s*results?/i);
    return m ? m[1].replace(/,/g, '') : '0';
})()
''')
    try:
        return int(count_str) if count_str else 0
    except (ValueError, TypeError):
        return 0


# ============================================================================
# Export
# ============================================================================

def setup_download_dir(client: CDPClient):
    """Configure CDP to allow downloads to export directory."""
    os.makedirs(str(EXPORT_DIR), exist_ok=True)
    client.request('Browser.setDownloadBehavior', dict(
        behavior='allow',
        downloadPath=str(EXPORT_DIR)
    ))


def open_export_dialog(client: CDPClient) -> bool:
    """Open Export menu -> Plain text file, handle consent. Returns True if dialog opened."""
    # Click export trigger
    client.eval_js('document.querySelector("#export-trigger-btn")?.click()')
    time.sleep(1)

    # Click "Plain text file" menu item
    client.eval_js('''
    (function() {
        var items = document.querySelectorAll('[role="menuitem"]');
        for (var i = 0; i < items.length; i++) {
            var t = (items[i].textContent || '').trim();
            if (t.startsWith('Plain text file')) { items[i].click(); return; }
        }
    })()
    ''')
    time.sleep(3)

    handle_consent(client)

    url = client.eval_js('location.href') or ''
    return 'overlay:export' in url


def configure_export_range(client: CDPClient, start: int, end: int):
    """Select 'Records from:' radio, enable and fill range inputs."""

    # Click the "Records from:" radio button and check its input
    client.eval_js(f'''
(function() {{
    var radios = document.querySelectorAll('mat-radio-button');
    for (var i = 0; i < radios.length; i++) {{
        var text = (radios[i].textContent || '').trim();
        if (text.indexOf('Records from') >= 0) {{
            radios[i].click();
            var input = radios[i].querySelector('input[type="radio"]');
            if (input) {{
                input.checked = true;
                input.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}
            break;
        }}
    }}

    // Find range number inputs (exclude pagination inputs snNextPage*)
    var numInputs = document.querySelectorAll('input[type="number"]');
    var rangeInputs = [];
    for (var i = 0; i < numInputs.length; i++) {{
        if (numInputs[i].id.indexOf('snNextPage') < 0) {{
            rangeInputs.push(numInputs[i]);
        }}
    }}

    if (rangeInputs.length >= 2) {{
        var setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value'
        ).set;

        rangeInputs[0].disabled = false;
        setter.call(rangeInputs[0], '{start}');
        rangeInputs[0].dispatchEvent(new Event('input', {{bubbles: true}}));
        rangeInputs[0].dispatchEvent(new Event('change', {{bubbles: true}}));

        rangeInputs[1].disabled = false;
        setter.call(rangeInputs[1], '{end}');
        rangeInputs[1].dispatchEvent(new Event('input', {{bubbles: true}}));
        rangeInputs[1].dispatchEvent(new Event('change', {{bubbles: true}}));
    }}
}})()
''')
    time.sleep(0.5)


def configure_export_content(client: CDPClient):
    """Select 'Author, Title, Source, Abstract' in Record Content dropdown."""
    # Click the Record Content dropdown - it's a <wos-select> with a
    # button[role="combobox"] whose aria-label starts with "Filter by"
    client.eval_js('''
(function() {
    var btns = document.querySelectorAll('button[role="combobox"]');
    for (var i = 0; i < btns.length; i++) {
        var label = btns[i].getAttribute('aria-label') || '';
        if (label.indexOf('Filter by') >= 0) {
            btns[i].click(); return;
        }
    }
})()
''')
    time.sleep(1)

    # Check if any dropdown options appeared and select the Abstract one
    result = client.eval_js('''
(function() {
    var opts = document.querySelectorAll('[role="option"], .mat-mdc-option, mat-option');
    for (var i = 0; i < opts.length; i++) {
        var text = (opts[i].textContent || '').trim();
        if (text.indexOf('Abstract') >= 0 && text.indexOf('Author') >= 0) {
            opts[i].click();
            return 'selected: ' + text;
        }
    }
    for (var j = 0; j < opts.length; j++) {
        var t = (opts[j].textContent || '').trim();
        if (t.indexOf('Abstract') >= 0) {
            opts[j].click();
            return 'selected(fallback): ' + t;
        }
    }
    return 'no options. count=' + opts.length;
})()
''')
    print(f'    Content select: {result}')
    time.sleep(0.5)


def click_export_button(client: CDPClient):
    """Click the Export button in the dialog."""
    client.eval_js('''
(function() {
    var btns = document.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) {
        var t = (btns[i].textContent || '').trim();
        var cls = (btns[i].className || '');
        if (t === 'Export' && cls.indexOf('mdc-button') >= 0) {
            btns[i].click(); return;
        }
    }
    // Fallback
    for (var j = 0; j < btns.length; j++) {
        if ((btns[j].textContent || '').trim() === 'Export') {
            btns[j].click(); return;
        }
    }
})()
''')


# ============================================================================
# Parsing
# ============================================================================

# ============================================================================
# Single Search + Export
# ============================================================================

def run_single_search(client: CDPClient, kw: dict, journals: list[str],
                      start_date: str = DEFAULT_START_DATE,
                      end_date: str = DEFAULT_END_DATE) -> list[str]:
    """Execute one search + export (in 1000-record batches). Returns list of exported file paths."""
    name = kw['name']
    query = kw['query']

    print(f'\n{"─" * 60}')
    print(f'Keyword: {name}')
    print(f'Topic len: {len(query)} chars')
    print(f'{"─" * 60}')

    # Navigate to basic search
    print('  Navigating to basic search...')
    client.request('Page.navigate', dict(url=WOS_BASIC_SEARCH), timeout=25)
    time.sleep(5)
    handle_consent(client)

    # Check if we need to sign in first
    if _on_wos_logged_out(client):
        print('  Not signed in — initiating CARSI login...')
        _click_wos_sign_in(client)
        time.sleep(3)
        if _on_login_page(client):
            username, password = _load_credentials()
            if username and password:
                if not _auto_login_wos(client, username, password):
                    print('  Auto-login did not reach WoS — aborting search.')
                    return []
            else:
                print(f'  Login required but no credentials in {CREDENTIALS_PATH}')
                return []
        # After login, navigate back to basic search
        print('  Re-navigating to basic search after login...')
        client.request('Page.navigate', dict(url=WOS_BASIC_SEARCH), timeout=25)
        time.sleep(5)
        handle_consent(client)

    # Setup form: Clear + Add 2 rows + change field types
    print('  Setting up form (Clear + 2 rows)...')
    setup_search_form(client)

    # Fill fields
    print('  Filling Topic...')
    fill_topic_field(client, query)

    print('  Filling Journals...')
    fill_journal_field(client, journals)

    print(f'  Filling Date range: {start_date} to {end_date}...')
    fill_date_range(client, start_date, end_date)

    # Submit
    print('  Submitting search...')
    if not submit_search(client):
        time.sleep(3)
        url = client.eval_js('location.href') or ''
        if 'login' in url.lower() and _on_login_page(client):
            username, password = _load_credentials()
            if username and password:
                if _auto_login_wos(client, username, password):
                    # After CAS login, re-navigate to basic search and redo
                    print('  Re-running search after auto-login...')
                    client.request('Page.navigate', dict(url=WOS_BASIC_SEARCH), timeout=25)
                    time.sleep(4)
                    handle_consent(client)
                    setup_search_form(client)
                    fill_topic_field(client, query)
                    fill_journal_field(client, journals)
                    fill_date_range(client, start_date, end_date)
                    if not submit_search(client):
                        time.sleep(3)
                        url = client.eval_js('location.href') or ''
                        if 'login' in url.lower():
                            print(f'  Search still redirecting to login after auto-login.')
                            return []
                else:
                    print('  Auto-login did not reach WoS.')
                    return []
            else:
                print(f'  Login required but no credentials in {CREDENTIALS_PATH}')
                print('  Please log in manually via persistent Chrome profile first.')
                return []
        elif 'basic-search' in url or 'login' in url:
            print(f'  Search failed. URL: {url[:120]}')
            return []

    count = get_result_count(client)
    print(f'  Results: {count}')

    if count == 0:
        print('  No results, skipping export.')
        return []

    # Export in batches of 1000 from the same search results
    setup_download_dir(client)
    num_batches = (count + 999) // 1000
    exported: list[str] = []

    for batch in range(num_batches):
        batch_start = batch * 1000 + 1
        batch_end = min((batch + 1) * 1000, count)

        if num_batches > 1:
            print(f'  Exporting batch {batch+1}/{num_batches}: {batch_start}-{batch_end}...')
        else:
            print(f'  Exporting {batch_start}-{batch_end}...')

        if not open_export_dialog(client):
            print('  Retrying export dialog...')
            time.sleep(1)
            handle_consent(client)
            if not open_export_dialog(client):
                print('  Could not open export dialog.')
                break

        configure_export_range(client, batch_start, batch_end)

        print('  Selecting Author, Title, Source, Abstract...')
        configure_export_content(client)

        existing = set(glob.glob(str(EXPORT_DIR / '*')))
        click_export_button(client)
        time.sleep(10)

        # Find new file
        new_files = set(glob.glob(str(EXPORT_DIR / '*'))) - existing
        txt_files = [f for f in new_files if f.endswith('.txt')]

        if not txt_files:
            savedrecs = EXPORT_DIR / 'savedrecs.txt'
            if savedrecs.exists() and time.time() - os.path.getmtime(str(savedrecs)) < 60:
                txt_files = [str(savedrecs)]

        if txt_files:
            src = txt_files[0]
            suffix = f'_{batch+1}' if num_batches > 1 else ''
            dest = EXPORT_DIR / f'{name}{suffix}.txt'
            if os.path.exists(str(dest)):
                os.remove(str(dest))
            shutil.move(src, str(dest))
            size = os.path.getsize(str(dest))
            records = parse_wos_plain_text(Path(dest))
            print(f'  Exported: {dest.name} ({size:,} bytes, {len(records)} records)')
            exported.append(str(dest))
        else:
            print(f'  No file downloaded for batch {batch+1}!')

    return exported


# ============================================================================
# Main
# ============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="WoS CDP search + export workflow.")
    parser.add_argument("--test", action="store_true", help="Search only the first keyword group.")
    parser.add_argument("--launch", action="store_true", help="Only launch Chrome and exit.")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE, help="Publication date start, YYYY-MM-DD.")
    parser.add_argument("--end-date", default=DEFAULT_END_DATE, help="Publication date end, YYYY-MM-DD.")
    parser.add_argument("--export-dir", default="", help="Export directory (overrides default).")
    args = parser.parse_args()

    global EXPORT_DIR
    if args.export_dir:
        EXPORT_DIR = Path(args.export_dir).resolve()

    if args.launch:
        launch_chrome()
        print("Chrome launched on port 9224")
        return

    cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    keywords = cfg['keywords']
    journals = cfg['topJournals']

    print('=' * 60)
    print('WoS CDP Workflow')
    print('=' * 60)
    print(f'Keywords: {len(keywords)} groups')
    print(f'Journals: {len(journals)}')
    print(f'Date: {args.start_date} to {args.end_date}')
    print(f'Export dir: {EXPORT_DIR}')

    launch_chrome()
    client = CDPClient(port=PORT)

    exported = []
    kw_list = keywords[:1] if args.test else keywords

    for kw in kw_list:
        result = run_single_search(client, kw, journals, args.start_date, args.end_date)
        if result:
            exported.extend(result)

    print(f'\n{"=" * 60}')
    print(f'Done. {len(exported)} files exported:')
    for f in exported:
        records = parse_wos_plain_text(Path(f))
        print(f'  {os.path.basename(f)}: {len(records)} records')

    client.close()


if __name__ == '__main__':
    main()

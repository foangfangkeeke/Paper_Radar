#!/usr/bin/env python3
"""Web of Science browser automation tool.

Scope:
- Open WoS with a persistent Chrome profile.
- Run fielded searches over Topic, Publication/Source Titles, and Publication Date.
- Export Plain Text files.

This file intentionally does not parse papers, deduplicate papers, or call MiniMax.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable


DEFAULT_WOS_URL = "https://www.webofscience.com/wos/alldb/basic-search"
LogFn = Callable[[str], None]


def clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def resolve_workspace_path(workspace: Path, value: str | Path | None) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    path = Path(value)
    return path if path.is_absolute() else workspace / path


def normalize_wos_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url:
        return DEFAULT_WOS_URL
    if url in {"https://www.webofscience.com", "https://www.webofscience.com/"}:
        return DEFAULT_WOS_URL
    if "/wos/woscc/basic-search" in url:
        return DEFAULT_WOS_URL
    return url


def parse_iso_date(value: dt.date | str, name: str) -> dt.date:
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}")
    return dt.date.fromisoformat(text)


def list_text_exports(export_dir: Path, since: float | None = None) -> list[Path]:
    files: list[Path] = []
    for path in export_dir.glob("*.txt"):
        if since is not None and path.stat().st_mtime < since:
            continue
        if path.stat().st_size > 0:
            files.append(path)
    return sorted(files, key=lambda p: p.stat().st_mtime)


def parse_debugger_address(address: str) -> tuple[str, int]:
    text = clean_text(address) or "127.0.0.1:9222"
    if ":" not in text:
        return text, 9222
    host, port_text = text.rsplit(":", 1)
    return host.strip() or "127.0.0.1", int(port_text.strip() or "9222")


def is_tcp_port_open(host: str, port: int, timeout_sec: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True
    except OSError:
        return False


def find_chrome_binary(configured_path: str = "") -> str:
    if configured_path and Path(configured_path).exists():
        return configured_path

    candidates = [
        os.environ.get("CHROME_PATH", ""),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
        shutil.which("chrome") or "",
        shutil.which("chrome.exe") or "",
        shutil.which("google-chrome") or "",
        shutil.which("chromium") or "",
        shutil.which("chromium-browser") or "",
    ]

    for path in candidates:
        if path and Path(path).exists():
            return str(path)

    raise RuntimeError(
        "Could not find Chrome binary. Set chromeBinary in automation/wos.config.json."
    )


def wait_for_debugger(address: str, timeout_sec: float = 12.0) -> None:
    host, port = parse_debugger_address(address)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if is_tcp_port_open(host, port):
            return
        time.sleep(0.25)
    raise RuntimeError(f"Chrome debugger not reachable at {address} after {timeout_sec:.0f}s")


@dataclasses.dataclass
class WosToolConfig:
    start_url: str = DEFAULT_WOS_URL
    basic_search_url: str = DEFAULT_WOS_URL
    download_dir: Path = Path("data/wos_exports")
    browser_profile_dir: Path = Path("data/browser_profiles/wos")
    account: str = ""
    password: str = ""
    attach_existing_browser: bool = True
    auto_launch_debug_chrome: bool = True
    debugger_address: str = "127.0.0.1:9222"
    chrome_binary: str = ""

    auto_search: bool = True
    auto_export: bool = True
    open_each_keyword_in_new_tab: bool = False
    close_browser_when_done: bool = True

    navigation_wait_sec: float = 25
    login_form_wait_sec: float = 3
    basic_search_load_wait_sec: float = 1.0
    result_count_stable_checks: int = 2
    result_count_stable_interval_sec: float = 0.75
    before_search_click_wait_sec: float = 0.5

    wos_wait_timeout_sec: int = 60
    export_wait_timeout_sec: int = 120

    export_format_label: str = "Plain Text File"
    export_record_content: str = "Author, Title, Source, Abstract"
    date_field_label: str = "Publication Date"

    export_chunk_size: int = 1000
    export_max_total_records: int = 0

    @classmethod
    def from_file(cls, workspace: Path, path: Path | None = None) -> "WosToolConfig":
        path = path or (workspace / "automation" / "wos.config.json")
        cfg = read_json(path, {})
        if not isinstance(cfg, dict):
            raise ValueError(f"WoS config must be a JSON object: {path}")

        return cls(
            start_url=normalize_wos_url(cfg.get("startUrl")),
            basic_search_url=normalize_wos_url(cfg.get("basicSearchUrl")),
            download_dir=resolve_workspace_path(workspace, cfg.get("downloadDir") or "data/wos_exports")
            or workspace / "data" / "wos_exports",
            browser_profile_dir=resolve_workspace_path(
                workspace,
                cfg.get("browserProfileDir") or "data/browser_profiles/wos",
            )
            or workspace / "data" / "browser_profiles" / "wos",
            account=str(cfg.get("account") or ""),
            password=str(cfg.get("password") or ""),
            attach_existing_browser=bool(cfg.get("attachExistingBrowser", True)),
            auto_launch_debug_chrome=bool(cfg.get("autoLaunchDebugChrome", True)),
            debugger_address=str(cfg.get("debuggerAddress") or "127.0.0.1:9222"),
            chrome_binary=str(cfg.get("chromeBinary") or ""),
            auto_search=bool(cfg.get("autoSearch", True)),
            auto_export=bool(cfg.get("autoExport", True)),
            open_each_keyword_in_new_tab=bool(cfg.get("openEachKeywordInNewTab", False)),
            close_browser_when_done=bool(cfg.get("closeBrowserWhenDone", True)),
            navigation_wait_sec=float(cfg.get("navigationWaitSec") or 25),
            login_form_wait_sec=float(cfg.get("loginFormWaitSec") or 3),
            basic_search_load_wait_sec=float(cfg.get("basicSearchLoadWaitSec") or 1.0),
            result_count_stable_checks=int(cfg.get("resultCountStableChecks") or 2),
            result_count_stable_interval_sec=float(cfg.get("resultCountStableIntervalSec") or 0.75),
            before_search_click_wait_sec=float(cfg.get("beforeSearchClickWaitSec") or 0.5),
            wos_wait_timeout_sec=int(cfg.get("wosWaitTimeoutSec") or 60),
            export_wait_timeout_sec=int(cfg.get("exportWaitTimeoutSec") or 120),
            export_format_label=str(cfg.get("exportFormatLabel") or "Plain Text File"),
            export_record_content=str(cfg.get("exportRecordContent") or "Author, Title, Source, Abstract"),
            date_field_label=str(cfg.get("dateFieldLabel") or cfg.get("dateField") or "Publication Date"),
            export_chunk_size=int(cfg.get("exportChunkSize") or 1000),
            export_max_total_records=int(cfg.get("exportMaxTotalRecords") or 0),
        )


@dataclasses.dataclass
class WosKeyword:
    name: str
    query: str


def normalize_topic_query(query: str) -> str:
    text = str(query or "").strip()

    match = re.fullmatch(r"(?is)\s*TS\s*=\s*\((.*)\)\s*", text)
    if match:
        return match.group(1).strip()

    match = re.fullmatch(r"(?is)\s*TS\s*=\s*(.*)\s*", text)
    if match:
        return match.group(1).strip()

    return text


def build_publication_source_titles_query(journals: Iterable[str]) -> str:
    names = [clean_text(name) for name in journals if clean_text(name)]
    return " OR ".join(f'"{name}"' for name in names)


def build_publication_date_query(start_date: dt.date, end_date: dt.date) -> str:
    if end_date < start_date:
        raise ValueError(f"end_date must be >= start_date: {start_date}..{end_date}")
    return f"{start_date.isoformat()} to {end_date.isoformat()}"


def keywords_from_watch_config(config: dict[str, Any]) -> list[WosKeyword]:
    keywords: list[WosKeyword] = []

    for idx, entry in enumerate(config.get("keywords", []), start=1):
        if isinstance(entry, str):
            name = f"keyword_{idx}"
            query = entry
        elif isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("key") or f"keyword_{idx}")
            query = str(entry.get("query") or entry.get("text") or entry.get("name") or "")
        else:
            continue

        query = query.strip()
        if query:
            keywords.append(WosKeyword(name=clean_text(name), query=query))

    return keywords


def journals_from_watch_config(config: dict[str, Any]) -> list[str]:
    journals: list[str] = []

    for entry in config.get("topJournals", []):
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("title") or "")
        else:
            continue

        name = clean_text(name)
        if name:
            journals.append(name)

    return journals


class WosBrowserTool:
    def __init__(self, config: WosToolConfig, log: LogFn = print) -> None:
        self.config = config
        self.log = log
        self.driver: Any | None = None
        self._download_started_at = 0.0

    def fetch_exports(
        self,
        keywords: list[WosKeyword],
        journals: list[str],
        start_date: dt.date | str,
        end_date: dt.date | str,
    ) -> list[Path]:
        if not keywords:
            raise ValueError("No WoS keyword query configured.")
        if not journals:
            raise ValueError("No WoS target journal configured.")

        resolved_start = parse_iso_date(start_date, "start_date")
        resolved_end = parse_iso_date(end_date, "end_date")
        build_publication_date_query(resolved_start, resolved_end)

        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        self.config.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        self._download_started_at = time.time()

        driver = self.open_browser()

        try:
            self._ensure_basic_search_page(driver, label="initial")
            self.login_if_needed(driver)
            self._ensure_basic_search_page(driver, label="after-login")

            if not self.config.auto_search:
                return []

            return self.run_keyword_searches(driver, keywords, journals, resolved_start, resolved_end)
        finally:
            if self.config.close_browser_when_done:
                self.close()

    def open_browser(self) -> Any:
        from selenium import webdriver

        cfg = self.config
        cfg.download_dir.mkdir(parents=True, exist_ok=True)
        cfg.browser_profile_dir.mkdir(parents=True, exist_ok=True)

        options = webdriver.ChromeOptions()

        if cfg.attach_existing_browser:
            # Full-auto stable mode:
            # 1) Start a normal Chrome process with remote debugging if 9222 is not open.
            # 2) Attach Selenium to that Chrome.
            # This avoids webdriver-created sessions while still requiring no manual browser launch.
            host, port = parse_debugger_address(cfg.debugger_address)
            if not is_tcp_port_open(host, port):
                if not cfg.auto_launch_debug_chrome:
                    raise RuntimeError(
                        f"Chrome debugger is not reachable at {cfg.debugger_address}. "
                        "Either start Chrome manually with --remote-debugging-port, "
                        "or set autoLaunchDebugChrome=true."
                    )

                chrome_binary = find_chrome_binary(cfg.chrome_binary)
                cmd = [
                    chrome_binary,
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={str(cfg.browser_profile_dir.resolve())}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-notifications",
                    "--start-maximized",
                    "about:blank",
                ]
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.log(
                    f"Chrome launched for attach mode | debuggerAddress={cfg.debugger_address} | "
                    f"profileDir={cfg.browser_profile_dir}"
                )
                wait_for_debugger(cfg.debugger_address, timeout_sec=15)

            # Attach mode: do not pass user-data-dir/prefs/excludeSwitches/useAutomationExtension
            # to chromedriver; Chrome has already been launched above.
            options.add_experimental_option("debuggerAddress", cfg.debugger_address)
            driver = webdriver.Chrome(options=options)
            self.driver = driver

            try:
                driver.execute_cdp_cmd(
                    "Page.setDownloadBehavior",
                    {
                        "behavior": "allow",
                        "downloadPath": str(cfg.download_dir.resolve()),
                    },
                )
            except Exception as exc:
                self.log(f"Warning: failed to set Chrome download dir via CDP: {exc}")

            self.log(
                f"WoS browser attached | downloadDir={cfg.download_dir} | "
                f"debuggerAddress={cfg.debugger_address}"
            )
            return driver

        options.add_argument(f"--user-data-dir={cfg.browser_profile_dir}")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-notifications")
        options.add_experimental_option(
            "prefs",
            {
                "download.default_directory": str(cfg.download_dir.resolve()),
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "safebrowsing.enabled": True,
            },
        )

        driver = webdriver.Chrome(options=options)
        self.driver = driver

        self.log(
            f"WoS browser opened | downloadDir={cfg.download_dir} | "
            f"profileDir={cfg.browser_profile_dir}"
        )
        return driver

    def _ensure_basic_search_page(self, driver: Any, label: str) -> None:
        """Go to WoS basic-search only when necessary.

        This avoids the old behavior:
        open_browser() -> goto basic-search
        fielded_search() -> goto basic-search again
        """

        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        current_url = str(getattr(driver, "current_url", "") or "")

        already_on_basic = (
            "/wos/alldb/basic-search" in current_url
            and driver.find_elements(By.CSS_SELECTOR, "form[data-ta='search-form'], #snSearchType")
        )

        if already_on_basic and not self._page_has_server_error(driver):
            self.log(f"WoS basic-search already ready | label={label} | url={current_url}")
            return

        self._navigate_with_retry(driver, self.config.basic_search_url, label=label)

    def _navigate_with_retry(self, driver: Any, url: str, label: str) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        last_error: Exception | None = None

        for attempt in range(1, 3):
            try:
                driver.get(url)
                wait = WebDriverWait(driver, self.config.navigation_wait_sec)

                wait.until(lambda d: d.current_url and not str(d.current_url).startswith("about:blank"))

                wait.until(
                    lambda d: d.find_elements(By.CSS_SELECTOR, "form[data-ta='search-form'], #snSearchType")
                    or d.find_elements(By.ID, "unPassword")
                    or clean_text(d.find_element(By.TAG_NAME, "body").text)
                )

                if self._page_has_server_error(driver):
                    raise RuntimeError("WoS Server.unexpectedError detected after navigation.")

                self.log(f"WoS page loaded | label={label} | url={driver.current_url}")
                return

            except Exception as exc:
                last_error = exc
                self.log(f"WoS navigation retry | label={label} | attempt={attempt}/2 | reason={exc}")
                time.sleep(2.0)

        raise RuntimeError(f"WoS navigation failed | label={label} | url={url}") from last_error

    def _page_has_server_error(self, driver: Any) -> bool:
        from selenium.webdriver.common.by import By

        try:
            body_text = clean_text(driver.find_element(By.TAG_NAME, "body").text)
        except Exception:
            return False

        lowered = body_text.lower()
        return "server.unexpectederror" in lowered or "unexpectederror" in lowered

    def login_if_needed(self, driver: Any) -> None:
        cfg = self.config

        if not cfg.account.strip() or not cfg.password:
            self.log("WoS login skipped: account/password are not configured.")
            return

        self._login_buaa_sso(driver, cfg.account.strip(), cfg.password)

    def _login_buaa_sso(self, driver: Any, account: str, password: str) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, self.config.login_form_wait_sec)

        try:
            username_box = wait.until(EC.element_to_be_clickable((By.ID, "unPassword")))
        except Exception:
            self.log("BUAA login skipped: already logged in or no password form detected.")
            return

        password_box = wait.until(EC.element_to_be_clickable((By.ID, "pwPassword")))

        username_box.clear()
        username_box.send_keys(account)

        password_box.clear()
        password_box.send_keys(password)

        if self._visible_by_id(driver, "captchaPasswor"):
            input("\n检测到验证码。请在浏览器中手动完成登录，然后回到这里按 Enter 继续...\n")
            return

        wait.until(
            EC.element_to_be_clickable(
                (
                    By.CSS_SELECTOR,
                    "input.submit-btn[onclick='loginPassword()'], input.submit-btn[value='登录']",
                )
            )
        ).click()

        self.log("BUAA username/password submitted.")
        time.sleep(2.0)

        if self._visible_by_id(driver, "captchaPasswor") or self._visible_by_id(driver, "captchaSmsToken"):
            input("\n登录后出现验证码/短信验证。请在浏览器中手动完成，然后回到这里按 Enter 继续...\n")

    def _visible_by_id(self, driver: Any, element_id: str) -> bool:
        from selenium.webdriver.common.by import By

        elements = driver.find_elements(By.ID, element_id)
        return bool(elements and elements[0].is_displayed())

    def run_keyword_searches(
        self,
        driver: Any,
        keywords: list[WosKeyword],
        journals: list[str],
        start_date: dt.date,
        end_date: dt.date,
    ) -> list[Path]:
        journal_query = build_publication_source_titles_query(journals)
        date_query = build_publication_date_query(start_date, end_date)

        downloaded: list[Path] = []

        for index, keyword in enumerate(keywords, start=1):
            if index > 1 and self.config.open_each_keyword_in_new_tab:
                driver.execute_script("window.open('about:blank', '_blank');")
                driver.switch_to.window(driver.window_handles[-1])

            self.log(f"WoS keyword search start | {index}/{len(keywords)} | keyword={keyword.name}")

            downloaded.extend(
                self.fielded_search(
                    driver=driver,
                    topic_query=normalize_topic_query(keyword.query),
                    journal_query=journal_query,
                    date_query=date_query,
                    keyword_name=keyword.name,
                    index=index,
                    total=len(keywords),
                )
            )

        return downloaded

    def fielded_search(
        self,
        driver: Any,
        topic_query: str,
        journal_query: str,
        date_query: str,
        keyword_name: str,
        index: int,
        total: int,
    ) -> list[Path]:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, self.config.wos_wait_timeout_sec)

        self._ensure_basic_search_page(driver, label=f"search-{index}")
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "form[data-ta='search-form'], #snSearchType")))

        if self.config.basic_search_load_wait_sec > 0:
            time.sleep(self.config.basic_search_load_wait_sec)

        if self._page_has_server_error(driver):
            self.log(f"WoS server error before search; reloading basic-search | keyword={keyword_name}")
            self._navigate_with_retry(driver, self.config.basic_search_url, label=f"recover-search-{index}")

        self._ensure_search_row_count(driver, 3)

        self._set_search_row(driver, 0, "Topic", topic_query)
        self._set_search_row(driver, 1, "Publication/Source Titles", journal_query)
        self._set_search_row(driver, 2, self.config.date_field_label, date_query)

        button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-ta='run-search']")))
        self._scroll_into_view(driver, button)
        time.sleep(self.config.before_search_click_wait_sec)
        button.click()

        self.log(f"WoS fielded search submitted | {index}/{total} | keyword={keyword_name}")

        result_count = self._wait_for_result_count(driver, keyword_name)

        if result_count == 0:
            self.log(f"WoS export skipped: zero results | keyword={keyword_name}")
            return []

        if not self.config.auto_export:
            return []

        return self.export_plain_text(driver, keyword_name, index, total, result_count)

    def _wait_for_result_count(self, driver: Any, keyword_name: str) -> int:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, self.config.wos_wait_timeout_sec)

        wait.until(
            lambda d: self._read_result_count(d) is not None
            or d.find_elements(By.CSS_SELECTOR, "#export-trigger-btn, button[id='export-trigger-btn']")
            or self._page_has_server_error(d)
        )

        if self._page_has_server_error(driver):
            raise RuntimeError(f"WoS Server.unexpectedError after submitting search: keyword={keyword_name}")

        stable_needed = max(1, int(self.config.result_count_stable_checks))
        interval = max(0.2, float(self.config.result_count_stable_interval_sec))
        deadline = time.time() + self.config.wos_wait_timeout_sec

        last_count: int | None = None
        stable = 0

        while time.time() < deadline:
            if self._page_has_server_error(driver):
                raise RuntimeError(f"WoS Server.unexpectedError while reading result count: keyword={keyword_name}")

            count = self._read_result_count(driver)

            if count is not None and count == last_count:
                stable += 1
                if stable >= stable_needed:
                    self.log(f"WoS result page ready | keyword={keyword_name} | resultCount={count}")
                    if count > 0:
                        wait.until(
                            EC.element_to_be_clickable(
                                (By.CSS_SELECTOR, "#export-trigger-btn, button[id='export-trigger-btn']")
                            )
                        )
                    return count
            else:
                last_count = count
                stable = 0

            time.sleep(interval)

        raise RuntimeError(
            f"Could not read stable WoS result count: keyword={keyword_name}; lastCount={last_count}"
        )

    def export_plain_text(self, driver: Any, keyword_name: str, index: int, total: int, result_count: int) -> list[Path]:
        ranges = self._build_export_ranges(result_count)

        self.log(
            f"WoS export plan | {index}/{total} | keyword={keyword_name} | "
            f"resultCount={result_count} | batches={len(ranges)}"
        )

        files: list[Path] = []

        for batch_index, (record_from, record_to) in enumerate(ranges, start=1):
            files.append(
                self._export_plain_text_range(
                    driver=driver,
                    keyword_name=keyword_name,
                    keyword_index=index,
                    keyword_total=total,
                    batch_index=batch_index,
                    batch_total=len(ranges),
                    record_from=record_from,
                    record_to=record_to,
                )
            )

        return files

    def _build_export_ranges(self, result_count: int) -> list[tuple[int, int]]:
        chunk = min(max(1, int(self.config.export_chunk_size)), 1000)

        final_record = result_count
        if self.config.export_max_total_records > 0:
            final_record = min(final_record, self.config.export_max_total_records)

        return [(start, min(start + chunk - 1, final_record)) for start in range(1, final_record + 1, chunk)]

    def _export_plain_text_range(
        self,
        driver: Any,
        keyword_name: str,
        keyword_index: int,
        keyword_total: int,
        batch_index: int,
        batch_total: int,
        record_from: int,
        record_to: int,
    ) -> Path:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, self.config.export_wait_timeout_sec)
        before_names = {path.name for path in self.config.download_dir.glob("*.txt")}

        trigger = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "#export-trigger-btn, button[id='export-trigger-btn']"))
        )
        self._scroll_into_view(driver, trigger)
        trigger.click()

        option = self._find_visible_option_by_text(driver, self.config.export_format_label, self.config.export_wait_timeout_sec)
        self._scroll_into_view(driver, option)
        option.click()

        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "app-export-out-details, #exportButton")))

        self._set_export_range(driver, record_from, record_to)
        self._set_export_record_content(driver, self.config.export_record_content)

        export_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#exportButton")))
        self._scroll_into_view(driver, export_button)
        export_button.click()

        self.log(
            f"WoS export submitted | keyword={keyword_index}/{keyword_total} | "
            f"batch={batch_index}/{batch_total} | range={record_from}-{record_to} | keyword={keyword_name}"
        )

        path = self._wait_for_new_download(before_names)
        self._wait_for_export_dialog_closed(driver)
        return path

    def _read_result_count(self, driver: Any) -> int | None:
        from selenium.webdriver.common.by import By

        precise_selectors = [
            "mat-checkbox[data-ta='select-page-checkbox'] input[aria-label]",
            "[data-ta='select-page-checkbox'] input[aria-label]",
            "#snRecListTop",
        ]

        for selector in precise_selectors:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                text = clean_text(element.get_attribute("aria-label") or element.text or element.get_attribute("textContent"))
                count = self._parse_count_from_precise_text(text)
                if count is not None:
                    return count

        body_text = clean_text(driver.find_element(By.TAG_NAME, "body").text)
        return self._parse_count_from_body_text(body_text)

    @staticmethod
    def _parse_count_from_precise_text(text: str) -> int | None:
        for pattern in (
            r"\bof\s+([0-9][0-9,]*)\b",
            r"\b([0-9][0-9,]*)\s+(?:results?|records?)\b",
        ):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1).replace(",", ""))
        return None

    @staticmethod
    def _parse_count_from_body_text(text: str) -> int | None:
        if re.search(r"\b0\s+(?:results?|records?)\b", text, flags=re.IGNORECASE):
            return 0

        for pattern in (
            r"\b([0-9][0-9,]*)\s+results?\b",
            r"\b([0-9][0-9,]*)\s+records?\b",
        ):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1).replace(",", ""))
        return None

    def _ensure_search_row_count(self, driver: Any, count: int) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, self.config.wos_wait_timeout_sec)

        while len(driver.find_elements(By.CSS_SELECTOR, "app-search-row")) < count:
            add_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-ta='add-row']")))
            self._scroll_into_view(driver, add_button)
            add_button.click()
            time.sleep(0.25)

    def _set_search_row(self, driver: Any, row_index: int, field_name: str, value: str) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, self.config.wos_wait_timeout_sec)
        rows = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "app-search-row")))
        row = rows[row_index]

        self._scroll_into_view(driver, row)
        self._select_search_field(driver, row, field_name)

        if self._is_date_field(field_name):
            self._set_date_range_row(driver, row, value)
            return

        input_box = row.find_element(By.CSS_SELECTOR, "input[data-ta='search-criteria-input'], input[name='search-main-box']")
        wait.until(lambda _: input_box.is_displayed() and input_box.is_enabled())

        self._set_input_value(driver, input_box, value)
        input_box.send_keys(Keys.TAB)

        actual = clean_text(input_box.get_attribute("value"))
        expected = clean_text(value)

        if actual != expected:
            raise RuntimeError(
                f"WoS input mismatch: row={row_index + 1}; field={field_name}; "
                f"actual={actual!r}; expected={expected!r}"
            )

    @staticmethod
    def _is_date_field(field_name: str) -> bool:
        return clean_text(field_name).lower() in {"publication date", "publication data"}

    @staticmethod
    def _parse_date_range_value(value: str) -> tuple[str, str]:
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", clean_text(value))
        if len(dates) != 2:
            raise RuntimeError(
                f"Invalid WoS publication date range: {value!r}; expected YYYY-MM-DD to YYYY-MM-DD"
            )
        return dates[0], dates[1]

    def _set_date_range_row(self, driver: Any, row: Any, value: str) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, self.config.wos_wait_timeout_sec)
        start_value, end_value = self._parse_date_range_value(value)

        start_box = wait.until(
            lambda _: row.find_element(
                By.CSS_SELECTOR,
                "input[aria-label='Search from box'], input[data-ta='search-criteria-input']",
            )
        )
        end_box = wait.until(
            lambda _: row.find_element(
                By.CSS_SELECTOR,
                "input[aria-label='Search to box'], input[data-ta='search-criteria-input-2']",
            )
        )

        for box, box_value in ((start_box, start_value), (end_box, end_value)):
            wait.until(lambda _: box.is_displayed() and box.is_enabled())
            self._set_input_value(driver, box, box_value)
            box.send_keys(Keys.TAB)
            time.sleep(0.1)

        actual = (clean_text(start_box.get_attribute("value")), clean_text(end_box.get_attribute("value")))
        expected = (start_value, end_value)

        if actual != expected:
            raise RuntimeError(f"WoS publication date mismatch: actual={actual}; expected={expected}")

    def _set_input_value(self, driver: Any, input_box: Any, value: str) -> None:
        from selenium.webdriver.common.keys import Keys

        input_box.click()
        input_box.send_keys(Keys.CONTROL, "a")
        input_box.send_keys(Keys.BACKSPACE)
        input_box.send_keys(value)

        driver.execute_script(
            """
            const el = arguments[0];
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            """,
            input_box,
        )
        time.sleep(0.1)

    def _select_search_field(self, driver: Any, row: Any, field_name: str) -> None:
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, self.config.wos_wait_timeout_sec)
        dropdown = self._get_search_field_dropdown(row)

        if self._dropdown_has_field(dropdown, field_name):
            return

        self._scroll_into_view(driver, dropdown)

        try:
            dropdown.click()
        except Exception:
            driver.execute_script("arguments[0].click();", dropdown)

        wait.until(
            lambda _: clean_text(dropdown.get_attribute("aria-expanded")).lower() == "true"
            or len(
                driver.find_elements(
                    "css selector",
                    ".cdk-overlay-container [role='option'], "
                    ".cdk-overlay-container mat-option, "
                    ".cdk-overlay-container button, "
                    "[role='listbox'] [role='option'], "
                    "[role='listbox'] button",
                )
            )
            > 0
        )

        option = self._find_visible_option_by_text(driver, field_name, self.config.wos_wait_timeout_sec)
        self._scroll_into_view(driver, option)

        try:
            option.click()
        except Exception:
            driver.execute_script("arguments[0].click();", option)

        wait.until(lambda _: self._dropdown_has_field(self._get_search_field_dropdown(row), field_name))
        time.sleep(0.2)

    def _get_search_field_dropdown(self, row: Any) -> Any:
        from selenium.webdriver.common.by import By

        buttons = row.find_elements(
            By.CSS_SELECTOR,
            "wos-select[data-ta='search-field-dropdown'] button[role='combobox']",
        )
        if buttons:
            return buttons[0]

        for button in row.find_elements(By.CSS_SELECTOR, "button[role='combobox']"):
            label = clean_text(button.get_attribute("aria-label"))
            data_ta = clean_text(button.get_attribute("data-ta"))
            text = clean_text(button.text)
            merged = f"{label} {data_ta} {text}".lower()

            if "select search field" in merged or data_ta:
                return button

        raise RuntimeError("Could not find WoS search-field dropdown in row.")

    def _dropdown_has_field(self, dropdown: Any, field_name: str) -> bool:
        wanted = clean_text(field_name).lower()
        aliases = {wanted}

        if wanted == "publication/source titles":
            aliases.update(
                {
                    "publication/source titles",
                    "publication titles",
                    "source titles",
                    "publication title",
                    "source title",
                }
            )

        if wanted in {"publication date", "publication data"}:
            aliases.update(
                {
                    "publication date",
                    "publication data",
                    "date",
                    "date range",
                }
            )

        if wanted == "topic":
            aliases.update({"topic"})

        text = (
            f"{clean_text(dropdown.text)} "
            f"{clean_text(dropdown.get_attribute('aria-label'))} "
            f"{clean_text(dropdown.get_attribute('data-ta'))} "
            f"{clean_text(dropdown.get_attribute('title'))}"
        ).lower()

        return any(alias in text for alias in aliases)

    def _find_visible_option_by_text(self, driver: Any, text: str, timeout: int) -> Any:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        wanted = clean_text(text).lower()
        aliases = {wanted}

        if wanted == "publication/source titles":
            aliases.update(
                {
                    "publication/source titles",
                    "publication titles",
                    "source titles",
                    "publication title",
                    "source title",
                }
            )

        if wanted in {"publication date", "publication data"}:
            aliases.update(
                {
                    "publication date",
                    "publication data",
                    "date",
                    "date range",
                }
            )

        if wanted == "topic":
            aliases.update({"topic"})

        def norm(value: Any) -> str:
            return clean_text(value).lower()

        def locate(_: Any) -> Any:
            candidates = driver.find_elements(
                By.CSS_SELECTOR,
                ".cdk-overlay-container [role='option'], "
                ".cdk-overlay-container mat-option, "
                ".cdk-overlay-container button, "
                "[role='listbox'] [role='option'], "
                "[role='listbox'] button",
            )

            visible_candidates = [element for element in candidates if element.is_displayed()]

            for element in visible_candidates:
                label = norm(element.text)
                aria = norm(element.get_attribute("aria-label"))
                data_ta = norm(element.get_attribute("data-ta"))
                title = norm(element.get_attribute("title"))
                merged = " ".join([label, aria, data_ta, title]).strip()

                if any(alias == label or alias == aria or alias == data_ta or alias == title for alias in aliases):
                    return element

                if any(f" {alias} " in f" {merged} " for alias in aliases):
                    return element

            for element in visible_candidates:
                label = norm(element.text)
                aria = norm(element.get_attribute("aria-label"))
                data_ta = norm(element.get_attribute("data-ta"))
                title = norm(element.get_attribute("title"))
                merged = " ".join([label, aria, data_ta, title]).strip()

                if any(alias in merged for alias in aliases):
                    return element

            for alias in aliases:
                xpath = (
                    "//*[self::button or self::mat-option or @role='option']"
                    "[contains(translate(normalize-space(.), "
                    "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                    f"{alias!r})]"
                )
                for element in driver.find_elements(By.XPATH, xpath):
                    if element.is_displayed():
                        return element

            return False

        return WebDriverWait(driver, timeout).until(locate)

    def _set_export_range(self, driver: Any, record_from: int, record_to: int) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, self.config.export_wait_timeout_sec)

        radio = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "input[name='outputMethodType'][value='fromRange'], #radio3-input")
            )
        )
        driver.execute_script("arguments[0].click();", radio)

        start_box = wait.until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "input[name='markFrom'], input[aria-label='Input starting record range']")
            )
        )
        end_box = wait.until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "input[name='markTo'], input[aria-label*='Input ending record range']")
            )
        )

        for box, value in ((start_box, str(record_from)), (end_box, str(record_to))):
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
                """,
                box,
            )
            time.sleep(0.1)

        actual = (clean_text(start_box.get_attribute("value")), clean_text(end_box.get_attribute("value")))
        expected = (str(record_from), str(record_to))

        if actual != expected:
            raise RuntimeError(f"WoS export range mismatch: actual={actual}; expected={expected}")

    def _set_export_record_content(self, driver: Any, record_content: str) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, self.config.export_wait_timeout_sec)
        wanted = clean_text(record_content)

        def locate_dropdown(_: Any) -> Any:
            for button in driver.find_elements(
                By.CSS_SELECTOR,
                "app-export-out-details wos-select button[role='combobox'], "
                "button[role='combobox'][aria-label*='Filter by']",
            ):
                text = f"{clean_text(button.text)} {clean_text(button.get_attribute('aria-label'))}"
                if button.is_displayed() and ("Filter by" in text or "Author, Title" in text):
                    return button
            return False

        dropdown = wait.until(locate_dropdown)
        current = f"{clean_text(dropdown.text)} {clean_text(dropdown.get_attribute('aria-label'))}"

        if wanted.lower() in current.lower():
            return

        self._scroll_into_view(driver, dropdown)
        dropdown.click()

        option = self._find_visible_option_by_text(driver, wanted, self.config.export_wait_timeout_sec)
        self._scroll_into_view(driver, option)
        option.click()

        wait.until(
            lambda _: wanted.lower()
            in f"{clean_text(dropdown.text)} {clean_text(dropdown.get_attribute('aria-label'))}".lower()
        )

    def _wait_for_new_download(self, before_names: set[str]) -> Path:
        deadline = time.time() + self.config.export_wait_timeout_sec

        while time.time() < deadline:
            partials = list(self.config.download_dir.glob("*.crdownload")) + list(self.config.download_dir.glob("*.tmp"))

            new_files = [
                path
                for path in self.config.download_dir.glob("*.txt")
                if path.name not in before_names and path.stat().st_size > 0
            ]

            if new_files and not partials:
                path = max(new_files, key=lambda p: p.stat().st_mtime)
                time.sleep(0.5)
                self.log(f"WoS export downloaded | {path}")
                return path

            time.sleep(0.5)

        raise RuntimeError(f"Timed out waiting for a new WoS txt export under {self.config.download_dir}")

    def _wait_for_export_dialog_closed(self, driver: Any) -> None:
        from selenium.webdriver.common.by import By

        deadline = time.time() + 10

        while time.time() < deadline:
            if not driver.find_elements(By.CSS_SELECTOR, "#exportButton, app-export-out-details"):
                return
            time.sleep(0.25)

        raise RuntimeError("WoS export dialog did not close after export submission.")

    def _scroll_into_view(self, driver: Any, element: Any) -> None:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
        time.sleep(0.05)

    def close(self) -> None:
        if self.driver is not None:
            if getattr(self.config, "attach_existing_browser", False):
                # In attach mode, keep the user's Chrome alive. Just drop the driver handle.
                self.driver = None
                return
            self.driver.quit()
            self.driver = None


def fetch_wos_plain_text_exports(
    workspace: str | Path,
    keywords: list[WosKeyword],
    journals: list[str],
    start_date: dt.date | str,
    end_date: dt.date | str,
    config_path: str | Path | None = None,
    log: LogFn = print,
) -> list[Path]:
    workspace_path = Path(workspace).resolve()
    cfg = WosToolConfig.from_file(workspace_path, Path(config_path) if config_path else None)
    tool = WosBrowserTool(cfg, log=log)

    return tool.fetch_exports(
        keywords=keywords,
        journals=journals,
        start_date=start_date,
        end_date=end_date,
    )


def fetch_wos_from_project_configs(
    workspace: str | Path,
    start_date: dt.date | str,
    end_date: dt.date | str,
    log: LogFn = print,
) -> list[Path]:
    workspace_path = Path(workspace).resolve()
    paper_watch = read_json(workspace_path / "automation" / "paper-watch.config.json", {})

    if not isinstance(paper_watch, dict):
        raise ValueError("automation/paper-watch.config.json must be a JSON object.")

    return fetch_wos_plain_text_exports(
        workspace=workspace_path,
        keywords=keywords_from_watch_config(paper_watch),
        journals=journals_from_watch_config(paper_watch),
        start_date=start_date,
        end_date=end_date,
        log=log,
    )


TOOL_SPEC = {
    "name": "fetch_wos_plain_text_exports",
    "description": (
        "Open Web of Science, run fielded searches over target journals and Publication Date range, "
        "export Plain Text files in 1000-record batches, and return downloaded txt paths."
    ),
    "inputs": {
        "workspace": "Project root path containing automation/wos.config.json and automation/paper-watch.config.json.",
        "keywords": "List of WosKeyword(name, query). One keyword is one WoS search.",
        "journals": "Target journal/source titles. Combined with OR in Publication/Source Titles.",
        "start_date": "First publication date, YYYY-MM-DD.",
        "end_date": "Last publication date, YYYY-MM-DD.",
    },
    "output": "List[pathlib.Path] of downloaded WoS Plain Text export files.",
}

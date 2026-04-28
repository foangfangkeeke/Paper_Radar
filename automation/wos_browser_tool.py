#!/usr/bin/env python3
"""Web of Science browser automation tool.

This module is intentionally independent from the paper queue / MiniMax code.
It exposes one agent-friendly function, ``fetch_wos_plain_text_exports``:

    input: workspace path, keyword queries, target journal titles, date window
    output: downloaded WoS Plain Text export files

The implementation uses Selenium with a persistent Chrome profile and performs:
1. open WoS all-databases fielded search
2. optional BUAA SSO username/password login
3. run one fielded search per keyword
4. export each result set as Plain Text File

Captcha/SMS/QR steps are not bypassed. If they appear, the tool pauses for
manual completion.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable


DEFAULT_WOS_URL = "https://www.webofscience.com/wos/alldb/basic-search"


LogFn = Callable[[str], None]


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def resolve_workspace_path(workspace: Path, value: str | Path | None) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return workspace / path


def normalize_wos_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url:
        return DEFAULT_WOS_URL
    if url in {"https://www.webofscience.com", "https://www.webofscience.com/"}:
        return DEFAULT_WOS_URL
    if "/wos/woscc/basic-search" in url:
        return DEFAULT_WOS_URL
    return url


def stable_text_exports(export_dir: Path, since: float | None = None) -> list[Path]:
    files: list[Path] = []
    for path in export_dir.glob("*.txt"):
        if since is not None and path.stat().st_mtime < since:
            continue
        if path.stat().st_size > 0:
            files.append(path)
    return sorted(files)


def wait_for_wos_exports(export_dir: Path, timeout_sec: int, since: float, log: LogFn = print) -> list[Path]:
    deadline = time.time() + timeout_sec
    last_log = 0.0
    while time.time() < deadline:
        files = stable_text_exports(export_dir, since)
        partials = list(export_dir.glob("*.crdownload")) + list(export_dir.glob("*.tmp"))
        if files and not partials:
            time.sleep(1.0)
            return stable_text_exports(export_dir, since)
        if time.time() - last_log >= 30:
            log(f"Waiting for WoS plain text export in {export_dir}; found={len(files)}; partialDownloads={len(partials)}")
            last_log = time.time()
        time.sleep(2.0)
    raise RuntimeError(f"Timed out waiting for WoS plain text export under {export_dir}.")


@dataclasses.dataclass
class WosToolConfig:
    start_url: str = DEFAULT_WOS_URL
    basic_search_url: str = DEFAULT_WOS_URL
    download_dir: Path = Path("data/wos_exports")
    browser_profile_dir: Path = Path("data/browser_profiles/wos")
    account: str = ""
    password: str = ""
    auto_search: bool = True
    auto_export: bool = True
    open_each_keyword_in_new_tab: bool = True
    manual_export_timeout_sec: int = 900
    navigation_wait_sec: float = 20
    login_form_wait_sec: float = 3
    basic_search_load_wait_sec: float = 0.5
    after_search_wait_sec: float = 1
    blank_page_wait_sec: float = 1.2
    result_settle_wait_sec: float = 1.0
    result_count_stable_checks: int = 2
    result_count_stable_interval_sec: float = 0.75
    before_search_click_wait_sec: float = 0.25
    wos_wait_timeout_sec: int = 60
    export_wait_timeout_sec: int = 90
    export_format_label: str = "Plain Text File"
    export_record_content: str = "Author, Title, Source, Abstract"
    export_record_from: int = 1
    export_record_to: int = 1000
    date_field_label: str = "Publication Date"
    export_all_records: bool = True
    export_chunk_size: int = 1000
    export_max_total_records: int = 0

    @classmethod
    def from_file(cls, workspace: Path, path: Path | None = None) -> "WosToolConfig":
        path = path or (workspace / "automation" / "wos.config.json")
        cfg = read_json(path, {})
        if not isinstance(cfg, dict):
            cfg = {}
        return cls(
            start_url=normalize_wos_url(cfg.get("startUrl")),
            basic_search_url=normalize_wos_url(cfg.get("basicSearchUrl")),
            download_dir=resolve_workspace_path(workspace, cfg.get("downloadDir") or "data/wos_exports") or workspace / "data" / "wos_exports",
            browser_profile_dir=resolve_workspace_path(workspace, cfg.get("browserProfileDir") or "data/browser_profiles/wos") or workspace / "data" / "browser_profiles" / "wos",
            account=str(cfg.get("account") or ""),
            password=str(cfg.get("password") or ""),
            auto_search=bool(cfg.get("autoSearch", True)),
            auto_export=bool(cfg.get("autoExport", True)),
            open_each_keyword_in_new_tab=bool(cfg.get("openEachKeywordInNewTab", True)),
            manual_export_timeout_sec=int(cfg.get("manualExportTimeoutSec", 900)),
            navigation_wait_sec=float(cfg.get("navigationWaitSec") or 20),
            login_form_wait_sec=float(cfg.get("loginFormWaitSec") or 3),
            basic_search_load_wait_sec=float(cfg.get("basicSearchLoadWaitSec") or 0.5),
            after_search_wait_sec=float(cfg.get("afterSearchWaitSec") or 1),
            blank_page_wait_sec=float(cfg.get("blankPageWaitSec") or 1.2),
            result_settle_wait_sec=float(cfg.get("resultSettleWaitSec") or 1.0),
            result_count_stable_checks=int(cfg.get("resultCountStableChecks") or 2),
            result_count_stable_interval_sec=float(cfg.get("resultCountStableIntervalSec") or 0.75),
            before_search_click_wait_sec=float(cfg.get("beforeSearchClickWaitSec") or 0.25),
            wos_wait_timeout_sec=int(cfg.get("wosWaitTimeoutSec") or 60),
            export_wait_timeout_sec=int(cfg.get("exportWaitTimeoutSec") or 90),
            export_format_label=str(cfg.get("exportFormatLabel") or "Plain Text File"),
            export_record_content=str(cfg.get("exportRecordContent") or "Author, Title, Source, Abstract"),
            export_record_from=int(cfg.get("exportRecordFrom") or 1),
            export_record_to=int(cfg.get("exportRecordTo") or 1000),
            date_field_label=str(cfg.get("dateFieldLabel") or cfg.get("dateField") or "Publication Date"),
            export_all_records=bool(cfg.get("exportAllRecords", True)),
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
    names = [str(name).strip() for name in journals if str(name).strip()]
    return " OR ".join(f'"{name}"' for name in names)


def coerce_date(value: dt.date | str | None, default: dt.date) -> dt.date:
    if isinstance(value, dt.date):
        return value
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        return dt.date.fromisoformat(text[:10])
    except Exception:
        return default


def build_publication_date_query(start_date: dt.date, end_date: dt.date) -> str:
    # WoS Fielded Search uses plain text in the Publication Date field.
    return f"{start_date.isoformat()} to {end_date.isoformat()}"


def build_year_query(start_year: int, end_year: int) -> str:
    # Backward-compatible helper; the browser tool now prefers Publication Date.
    return " OR ".join(str(year) for year in range(int(start_year), int(end_year) + 1))


def keywords_from_watch_config(config: dict[str, Any]) -> list[WosKeyword]:
    def name_of(entry: Any, idx: int) -> str:
        if isinstance(entry, str):
            return f"keyword_{idx}"
        if isinstance(entry, dict):
            return str(entry.get("name") or entry.get("key") or entry.get("query") or f"keyword_{idx}")
        return f"keyword_{idx}"

    def query_of(entry: Any) -> str:
        if isinstance(entry, str):
            return entry
        if isinstance(entry, dict):
            return str(entry.get("query") or entry.get("text") or entry.get("name") or "")
        return str(entry or "")

    items: list[WosKeyword] = []
    for idx, entry in enumerate(config.get("keywords", []), start=1):
        query = query_of(entry).strip()
        if query:
            items.append(WosKeyword(name=name_of(entry, idx).strip() or f"keyword_{idx}", query=query))
    return items


def journals_from_watch_config(config: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for entry in config.get("topJournals", []):
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("title") or "")
        else:
            name = ""
        if name.strip():
            names.append(name.strip())
    return names


class WosBrowserTool:
    """Agent-friendly WoS browser tool.

    Public methods:
    - open_browser()
    - login_if_needed()
    - run_keyword_searches(...)
    - export_plain_text(...)
    - fetch_exports(...)
    """

    def __init__(self, config: WosToolConfig, log: LogFn = print) -> None:
        self.config = config
        self.log = log
        self.driver: Any | None = None

    def fetch_exports(
        self,
        keywords: list[WosKeyword],
        journals: list[str],
        start_year: int | None = None,
        end_year: int | None = None,
        start_date: dt.date | str | None = None,
        end_date: dt.date | str | None = None,
    ) -> list[Path]:
        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        self.config.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        started_at = time.time()
        driver = self.open_browser()
        self.login_if_needed(driver)
        if self.config.auto_search:
            resolved_start = coerce_date(start_date, dt.date(int(start_year or 2023), 1, 1))
            resolved_end = coerce_date(end_date, dt.date(int(end_year or dt.date.today().year), 12, 31))
            self.run_keyword_searches(driver, keywords, journals, resolved_start, resolved_end)
        return wait_for_wos_exports(self.config.download_dir, self.config.manual_export_timeout_sec, started_at, self.log)

    def open_browser(self) -> Any:
        from selenium import webdriver

        cfg = self.config
        cfg.download_dir.mkdir(parents=True, exist_ok=True)
        cfg.browser_profile_dir.mkdir(parents=True, exist_ok=True)

        options = webdriver.ChromeOptions()
        options.add_argument(f"--user-data-dir={cfg.browser_profile_dir}")
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
                "download.default_directory": str(cfg.download_dir.resolve()),
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "safebrowsing.enabled": True,
            },
        )
        driver = webdriver.Chrome(options=options)
        self.driver = driver
        self.navigate(driver, cfg.start_url, label="start")
        self.log(f"WoS browser opened | startUrl={cfg.start_url} | currentUrl={getattr(driver, 'current_url', '')} | downloadDir={cfg.download_dir} | profileDir={cfg.browser_profile_dir}")
        return driver

    def navigate(self, driver: Any, url: str, label: str = "wos") -> None:
        from selenium.webdriver.common.by import By

        timeout = self.config.navigation_wait_sec
        last_url = ""
        # Only use about:blank for the first browser startup. Calling it again
        # before every keyword search makes the browser visibly enter blank twice
        # and can slow down WoS' Angular bootstrapping. For normal searches, go
        # directly to the Basic Search URL.
        use_blank_first = label == "start"
        for attempt in range(1, 4):
            if attempt == 1 and use_blank_first:
                driver.get("about:blank")
                time.sleep(max(0.0, float(self.config.blank_page_wait_sec)))
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
            self.log(f"WARNING: WoS navigation retry | label={label}; attempt={attempt}; currentUrl={last_url}")
        raise RuntimeError(f"WoS navigation failed: label={label}; url={url}; lastUrl={last_url}")

    def login_if_needed(self, driver: Any) -> None:
        cfg = self.config
        account = cfg.account.strip()
        password = cfg.password
        if not account or not password:
            self.log("WoS login skipped: account/password are not configured.")
            return
        try:
            self._login_buaa_sso(driver, account, password, timeout=cfg.login_form_wait_sec)
        except Exception as exc:
            self.log(f"WARNING: automatic BUAA login did not complete: {exc}")
            self.log("If the browser is on a captcha/SMS/QR page, complete it manually. The program will keep waiting for export.")

    def _login_buaa_sso(self, driver: Any, account: str, password: str, timeout: float = 3) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, timeout)
        username_locator = (By.ID, "unPassword")
        password_locator = (By.ID, "pwPassword")
        login_button_locator = (By.CSS_SELECTOR, "input.submit-btn[onclick='loginPassword()'], input.submit-btn[value='登录']")

        try:
            username_box = wait.until(EC.element_to_be_clickable(username_locator))
        except Exception:
            self.log("BUAA login skipped: already logged in or no password form detected.")
            return
        password_box = wait.until(EC.element_to_be_clickable(password_locator))
        username_box.click(); username_box.clear(); username_box.send_keys(account)
        password_box.click(); password_box.clear(); password_box.send_keys(password)

        if self._is_visible_by_id(driver, "captchaPasswor"):
            input("\n检测到验证码登录框。请在浏览器中手动输入验证码并完成登录，然后回到这里按 Enter 继续...\n")
            return

        wait.until(EC.element_to_be_clickable(login_button_locator)).click()
        self.log("BUAA username/password submitted automatically.")
        time.sleep(3.0)
        if self._is_visible_by_id(driver, "captchaPasswor") or self._is_visible_by_id(driver, "captchaSmsToken"):
            input("\n登录后出现验证码/短信验证。请在浏览器中手动完成，然后回到这里按 Enter 继续...\n")

    def _is_visible_by_id(self, driver: Any, element_id: str) -> bool:
        from selenium.webdriver.common.by import By
        try:
            return bool(driver.find_element(By.ID, element_id).is_displayed())
        except Exception:
            return False

    def run_keyword_searches(self, driver: Any, keywords: list[WosKeyword], journals: list[str], start_date: dt.date, end_date: dt.date) -> None:
        if not keywords:
            self.log("WoS search skipped: no keyword query.")
            return
        journal_query = build_publication_source_titles_query(journals)
        if not journal_query:
            self.log("WoS search skipped: no target journals.")
            return
        date_query = build_publication_date_query(start_date, end_date)
        total = len(keywords)
        for index, keyword in enumerate(keywords, start=1):
            if index > 1 and self.config.open_each_keyword_in_new_tab:
                self._open_new_browser_tab(driver)
            self.log(f"WoS keyword search start | {index}/{total}; keyword={keyword.name}; publicationDate={date_query}")
            self.fielded_search(
                driver=driver,
                topic_query=normalize_topic_query(keyword.query),
                journal_query=journal_query,
                date_query=date_query,
                keyword_name=keyword.name,
                index=index,
                total=total,
            )
        self.log(f"WoS keyword searches submitted | total={total}")

    def fielded_search(self, driver: Any, topic_query: str, journal_query: str, date_query: str, keyword_name: str, index: int, total: int) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        cfg = self.config
        wait = WebDriverWait(driver, cfg.wos_wait_timeout_sec)
        self.navigate(driver, cfg.basic_search_url, label=f"search-{index}")
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "form[data-ta='search-form'], #snSearchType")))
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[data-ta='search-criteria-input'], input[name='search-main-box']")))
        if cfg.basic_search_load_wait_sec > 0:
            time.sleep(cfg.basic_search_load_wait_sec)
        self._ensure_search_row_count(driver, 3)
        self._set_search_row(driver, 0, "Topic", topic_query)
        self._set_search_row(driver, 1, "Publication/Source Titles", journal_query)
        self._set_search_row(driver, 2, self.config.date_field_label, date_query)
        self._verify_search_fields(driver, ["Topic", "Publication/Source Titles", self.config.date_field_label])
        try:
            driver.execute_script("if (document.activeElement) document.activeElement.blur();")
        except Exception:
            pass
        time.sleep(cfg.before_search_click_wait_sec)
        button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-ta='run-search']")))
        self._scroll_into_view(driver, button)
        button.click()
        self.log(f"WoS fielded search submitted | {index}/{total}; keyword={keyword_name}")
        self._wait_for_result_page_ready(driver, keyword_name=keyword_name)
        if cfg.auto_export:
            self.export_plain_text(driver, keyword_name, index, total)

    def _wait_for_result_page_ready(self, driver: Any, keyword_name: str = "") -> None:
        """Wait until the WoS result page has settled before exporting.

        WoS may update the result count asynchronously after Search is clicked.
        Exporting immediately can capture a stale/partial state, so we wait for
        the Export button and then require the parsed result count to stay stable
        for a short interval. If the count cannot be parsed, the Export button
        plus the fixed settle wait is still used as a safe fallback.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        cfg = self.config
        fixed_wait = max(float(cfg.after_search_wait_sec or 0), float(cfg.result_settle_wait_sec or 0))
        if fixed_wait > 0:
            time.sleep(fixed_wait)

        wait = WebDriverWait(driver, cfg.wos_wait_timeout_sec)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#export-trigger-btn, button[id='export-trigger-btn']")))

        stable_checks = max(1, int(cfg.result_count_stable_checks or 1))
        interval = max(0.2, float(cfg.result_count_stable_interval_sec or 0.75))
        previous = self._get_wos_result_count(driver)
        stable_seen = 0
        deadline = time.time() + max(3.0, min(float(cfg.wos_wait_timeout_sec), 20.0))
        while time.time() < deadline:
            time.sleep(interval)
            current = self._get_wos_result_count(driver)
            if current is not None and current == previous:
                stable_seen += 1
                if stable_seen >= stable_checks:
                    self.log(f"WoS result page ready | keyword={keyword_name}; resultCount={current}")
                    return
            else:
                stable_seen = 0
                previous = current

            # If WoS exposes Export but no count can be parsed, do not block too long.
            if previous is None and time.time() + interval >= deadline:
                break

        self.log(f"WARNING: WoS result count did not stabilize before export | keyword={keyword_name}; lastCount={previous}")

    def export_plain_text(self, driver: Any, keyword_name: str, index: int, total: int) -> None:
        """Export current WoS result set as one or more Plain Text files.

        WoS allows at most 1000 records per Plain Text export. When
        ``export_all_records`` is true, this method reads the result count from
        the result page and exports ranges like 1-1000, 1001-2000, ... .
        """
        result_count = self._get_wos_result_count(driver)
        ranges = self._build_export_ranges(result_count)
        if not ranges:
            self.log(
                f"WoS export skipped: no export range available | {index}/{total}; "
                f"keyword={keyword_name}; resultCount={result_count}"
            )
            return

        self.log(
            f"WoS export plan | {index}/{total}; keyword={keyword_name}; "
            f"resultCount={result_count if result_count is not None else 'UNKNOWN'}; batches={len(ranges)}"
        )
        for batch_index, (record_from, record_to) in enumerate(ranges, start=1):
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
    ) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        cfg = self.config
        wait = WebDriverWait(driver, cfg.export_wait_timeout_sec)
        trigger = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#export-trigger-btn, button[id='export-trigger-btn']")))
        self._scroll_into_view(driver, trigger)
        trigger.click()
        option = self._find_exact_visible_option_by_text(driver, cfg.export_format_label, timeout=cfg.export_wait_timeout_sec)
        self._scroll_into_view(driver, option)
        option.click()
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".window, app-export-out-details, #exportButton")))
        self._set_export_range_if_present(driver, record_from=record_from, record_to=record_to)
        self._set_export_record_content(driver, cfg.export_record_content)
        export_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#exportButton")))
        self._scroll_into_view(driver, export_button)
        export_button.click()
        self.log(
            f"WoS plain text export submitted | keyword={keyword_index}/{keyword_total}; "
            f"batch={batch_index}/{batch_total}; range={record_from}-{record_to}; keyword={keyword_name}"
        )
        self._wait_for_download_quiet()
        self._wait_for_export_dialog_closed(driver)

    def _get_wos_result_count(self, driver: Any) -> int | None:
        """Return current WoS result count when it can be read from the results page."""
        from selenium.webdriver.common.by import By

        candidates: list[str] = []
        selectors = [
            "mat-checkbox[data-ta='select-page-checkbox'] input[aria-label]",
            "[data-ta='select-page-checkbox'] input[aria-label]",
            "mat-checkbox[data-ta='select-page-checkbox'] label",
            "#snRecListTop",
            "body",
        ]
        for selector in selectors:
            try:
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    for attr in ("aria-label", "textContent", "innerText"):
                        try:
                            value = element.get_attribute(attr) if attr != "innerText" else element.text
                            value = clean_text(value)
                            if value:
                                candidates.append(value)
                        except Exception:
                            pass
            except Exception:
                pass

        patterns = [
            r"of\s+([0-9][0-9,]*)",
            r"0\s*/\s*([0-9][0-9,]*)",
            r"([0-9][0-9,]*)\s+results?",
            r"([0-9][0-9,]*)\s+records?",
        ]
        for text in candidates:
            for pattern in patterns:
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if match:
                    try:
                        return int(match.group(1).replace(",", ""))
                    except Exception:
                        continue
        return None

    def _build_export_ranges(self, result_count: int | None) -> list[tuple[int, int]]:
        cfg = self.config
        chunk = max(1, min(int(cfg.export_chunk_size or 1000), 1000))

        if not cfg.export_all_records or result_count is None:
            start = max(1, int(cfg.export_record_from or 1))
            end = max(start, int(cfg.export_record_to or start))
            return [(start, min(end, start + chunk - 1))]

        if result_count <= 0:
            return []
        max_total = int(cfg.export_max_total_records or 0)
        final_record = result_count if max_total <= 0 else min(result_count, max_total)
        ranges: list[tuple[int, int]] = []
        start = 1
        while start <= final_record:
            end = min(start + chunk - 1, final_record)
            ranges.append((start, end))
            start = end + 1
        return ranges

    def _ensure_search_row_count(self, driver: Any, count: int) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
        wait = WebDriverWait(driver, self.config.wos_wait_timeout_sec)
        for _ in range(count + 3):
            rows = driver.find_elements(By.CSS_SELECTOR, "app-search-row")
            if len(rows) >= count:
                return
            add_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-ta='add-row']")))
            self._scroll_into_view(driver, add_button)
            add_button.click()
            time.sleep(0.25)
        raise RuntimeError(f"Could not create enough WoS search rows: expected={count}; actual={len(driver.find_elements(By.CSS_SELECTOR, 'app-search-row'))}")

    def _set_search_row(self, driver: Any, row_index: int, field_name: str, value: str) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
        wait = WebDriverWait(driver, self.config.wos_wait_timeout_sec)
        rows = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "app-search-row")))
        if row_index >= len(rows):
            raise RuntimeError(f"WoS row index out of range: row_index={row_index}; rows={len(rows)}")
        row = rows[row_index]
        self._scroll_into_view(driver, row)
        self._select_search_field(driver, row, field_name)
        self._verify_single_search_field(row, field_name)

        # Publication Date / Publication Data is a split field with two inputs:
        # Search from box + Search to box. Do not paste a single "A to B" string.
        if self._is_wos_date_field(field_name):
            self._set_date_range_row(driver, row, value)
            return

        input_box = row.find_element(By.CSS_SELECTOR, "input[data-ta='search-criteria-input'], input[name='search-main-box']")
        wait.until(lambda _: input_box.is_displayed() and input_box.is_enabled())
        self._paste_input_value(driver, input_box, value)
        input_box.send_keys(Keys.TAB)
        time.sleep(0.15)
        actual = clean_text(input_box.get_attribute("value"))
        expected = clean_text(value)
        if actual != expected:
            raise RuntimeError(f"WoS input value mismatch: row={row_index + 1}; field={field_name}; actualLen={len(actual)}; expectedLen={len(expected)}")

    def _is_wos_date_field(self, field_name: str) -> bool:
        return clean_text(field_name).lower() in {"publication date", "publication data"}

    def _parse_date_range_value(self, value: str) -> tuple[str, str]:
        text = clean_text(value)
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", text)
        if len(dates) >= 2:
            return dates[0], dates[1]
        if len(dates) == 1:
            return dates[0], dates[0]
        raise RuntimeError(f"Invalid WoS publication date range: {value!r}; expected YYYY-MM-DD to YYYY-MM-DD")

    def _set_date_range_row(self, driver: Any, row: Any, value: str) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, self.config.wos_wait_timeout_sec)
        start_value, end_value = self._parse_date_range_value(value)

        start_box = wait.until(lambda _: row.find_element(
            By.CSS_SELECTOR,
            "input[aria-label='Search from box'], input[data-ta='search-criteria-input']",
        ))
        end_box = wait.until(lambda _: row.find_element(
            By.CSS_SELECTOR,
            "input[aria-label='Search to box'], input[data-ta='search-criteria-input-2']",
        ))

        for box, box_value in ((start_box, start_value), (end_box, end_value)):
            wait.until(lambda _: box.is_displayed() and box.is_enabled())
            self._paste_input_value(driver, box, box_value)
            box.send_keys(Keys.TAB)
            time.sleep(0.1)

        actual_start = clean_text(start_box.get_attribute("value"))
        actual_end = clean_text(end_box.get_attribute("value"))
        if actual_start != start_value or actual_end != end_value:
            raise RuntimeError(
                "WoS publication date value mismatch: "
                f"actual={actual_start!r}..{actual_end!r}; expected={start_value!r}..{end_value!r}"
            )

    def _paste_input_value(self, driver: Any, input_box: Any, value: str) -> None:
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

    def _select_search_field(self, driver: Any, row: Any, field_name: str) -> None:
        from selenium.webdriver.support.ui import WebDriverWait
        wait = WebDriverWait(driver, self.config.wos_wait_timeout_sec)
        dropdown = self._get_search_field_dropdown(row)
        if self._dropdown_has_field(dropdown, field_name):
            return
        self._scroll_into_view(driver, dropdown)
        dropdown.click()
        option = self._find_exact_visible_option_by_text(driver, field_name, timeout=self.config.wos_wait_timeout_sec)
        self._scroll_into_view(driver, option)
        option.click()
        wait.until(lambda _: self._dropdown_has_field(self._get_search_field_dropdown(row), field_name))
        time.sleep(0.15)

    def _verify_single_search_field(self, row: Any, expected: str) -> None:
        dropdown = self._get_search_field_dropdown(row)
        if not self._dropdown_has_field(dropdown, expected):
            actual = clean_text(dropdown.text) or clean_text(dropdown.get_attribute("aria-label")) or clean_text(dropdown.get_attribute("data-ta"))
            raise RuntimeError(f"WoS search field mismatch before input: expected={expected}; actual={actual}")

    def _get_search_field_dropdown(self, row: Any) -> Any:
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
        raise RuntimeError("Could not find WoS search-field dropdown in row.")

    def _dropdown_has_field(self, dropdown: Any, field_name: str) -> bool:
        wanted = field_name.strip().lower()
        aliases = {wanted}
        if wanted == "publication/source titles":
            aliases.update({"publication titles", "source titles"})
        if wanted in {"publication date", "publication data"}:
            aliases.update({"publication date", "publication data"})
        text = clean_text(dropdown.text).lower()
        label = clean_text(dropdown.get_attribute("aria-label")).lower()
        data_ta = clean_text(dropdown.get_attribute("data-ta")).lower()
        return any(alias in text or alias in label or alias == data_ta for alias in aliases)

    def _verify_search_fields(self, driver: Any, expected_fields: list[str]) -> None:
        from selenium.webdriver.common.by import By
        rows = driver.find_elements(By.CSS_SELECTOR, "app-search-row")
        for idx, expected in enumerate(expected_fields):
            if idx >= len(rows):
                raise RuntimeError(f"WoS search row missing: row={idx + 1}; expected={expected}")
            dropdown = self._get_search_field_dropdown(rows[idx])
            if not self._dropdown_has_field(dropdown, expected):
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
        if wanted in {"publication date", "publication data"}:
            aliases.update({"publication date", "publication data"})

        def locate(_: Any) -> Any:
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

    def _set_export_range_if_present(self, driver: Any, record_from: int | None = None, record_to: int | None = None) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        try:
            radio = driver.find_element(By.CSS_SELECTOR, "input[name='outputMethodType'][value='fromRange'], #radio3-input")
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
        start_value = str(int(record_from if record_from is not None else self.config.export_record_from))
        end_value = str(int(record_to if record_to is not None else self.config.export_record_to))
        for box, value in ((start_box, start_value), (end_box, end_value)):
            try:
                self._scroll_into_view(driver, box)
                box.click(); box.send_keys(Keys.CONTROL, "a"); box.send_keys(Keys.BACKSPACE); box.send_keys(value)
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

    def _set_export_record_content(self, driver: Any, record_content: str) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        wait = WebDriverWait(driver, self.config.export_wait_timeout_sec)
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
        option = self._find_exact_visible_option_by_text(driver, wanted, timeout=self.config.export_wait_timeout_sec)
        self._scroll_into_view(driver, option)
        option.click()
        wait.until(lambda _: wanted.lower() in (clean_text(dropdown.text) + " " + clean_text(dropdown.get_attribute("aria-label"))).lower())
        time.sleep(0.15)

    def _wait_for_download_quiet(self) -> None:
        export_dir = self.config.download_dir
        export_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.config.export_wait_timeout_sec
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
        self.log(f"WARNING: WoS export download was not observed within {self.config.export_wait_timeout_sec}s; continuing to main watcher.")

    def _wait_for_export_dialog_closed(self, driver: Any) -> None:
        from selenium.webdriver.common.by import By
        deadline = time.time() + min(10, max(2, self.config.export_wait_timeout_sec))
        while time.time() < deadline:
            try:
                if not driver.find_elements(By.CSS_SELECTOR, "#exportButton, app-export-out-details, .window"):
                    return
            except Exception:
                return
            time.sleep(0.25)

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

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None


def fetch_wos_plain_text_exports(
    workspace: str | Path,
    keywords: list[WosKeyword],
    journals: list[str],
    start_year: int | None = None,
    end_year: int | None = None,
    start_date: dt.date | str | None = None,
    end_date: dt.date | str | None = None,
    config_path: str | Path | None = None,
    log: LogFn = print,
) -> list[Path]:
    """Agent-facing WoS tool function.

    Returns the downloaded ``*.txt`` export files under the configured WoS export
    directory. It does not parse or score papers; those are separate tools.
    """
    workspace_path = Path(workspace).resolve()
    cfg = WosToolConfig.from_file(workspace_path, Path(config_path) if config_path else None)
    tool = WosBrowserTool(cfg, log=log)
    return tool.fetch_exports(
        keywords=keywords,
        journals=journals,
        start_year=start_year,
        end_year=end_year,
        start_date=start_date,
        end_date=end_date,
    )


def fetch_wos_from_project_configs(
    workspace: str | Path,
    start_year: int | None = None,
    end_year: int | None = None,
    start_date: dt.date | str | None = None,
    end_date: dt.date | str | None = None,
    log: LogFn = print,
) -> list[Path]:
    """Convenience entrypoint using automation/paper-watch.config.json and wos.config.json."""
    workspace_path = Path(workspace).resolve()
    paper_watch = read_json(workspace_path / "automation" / "paper-watch.config.json", {})
    if not isinstance(paper_watch, dict):
        paper_watch = {}
    keywords = keywords_from_watch_config(paper_watch)
    journals = journals_from_watch_config(paper_watch)
    return fetch_wos_plain_text_exports(
        workspace_path,
        keywords,
        journals,
        start_year=start_year,
        end_year=end_year,
        start_date=start_date,
        end_date=end_date,
        log=log,
    )


TOOL_SPEC = {
    "name": "fetch_wos_plain_text_exports",
    "description": "Open Web of Science, run one fielded search per keyword over target journals and a Publication Date range, export each result set as Plain Text File. If a result set has more than 1000 records, export it in 1000-record batches, then return downloaded txt paths.",
    "inputs": {
        "workspace": "Project root path containing automation/wos.config.json and optionally automation/paper-watch.config.json.",
        "keywords": "List of WosKeyword(name, query). One keyword is one WoS search.",
        "journals": "Target journal/source titles. They are combined by OR in Publication/Source Titles.",
        "start_date": "First publication date to search, ISO format YYYY-MM-DD.",
        "end_date": "Last publication date to search, ISO format YYYY-MM-DD.",
    },
    "output": "List[pathlib.Path] of downloaded WoS Plain Text export files.",
}

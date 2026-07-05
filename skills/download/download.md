# Download

Single-paper PDF download from targeted journals/publishers.

## Scripts

- **`paper_downloader.py`** — Targeted PDF download (single paper, given DOI + journal)

## Config Files

- **`download.config.json`** — download directory

## Publisher Detection

The publisher is **auto-detected from the DOI** via an HTTP redirect — no manual journal mapping needed. The `--journal` flag is optional (for logging only).

Supported publishers:

| Publisher | Method | Status |
|-----------|--------|--------|
| ScienceDirect | CDP live session + signed PDF URL with browser cookies | OK |
| INFORMS (via EBSCO) | CDP automation + Fetch interception | OK |
| Springer | — | 🔜 |
| Wiley | — | 🔜 |
| Nature | — | 🔜 |

---

## Usage

### ScienceDirect

```bash
# --journal is optional — publisher auto-detected from DOI
python skills/download/paper_downloader.py \
    --doi "10.1016/j.ejor.2024.xxx" \
    --title "Paper Title"
```

### INFORMS (since Jan 2026: EBSCO-hosted)

**One-time setup:**

1. Copy your Chrome profile so CDP can use it:
```
mkdir c:\faq_work\Agent_Radar\data\chrome_profile\Default
xcopy "C:\Users\<you>\AppData\Local\Google\Chrome\User Data\Default\*" ^
    "c:\faq_work\Agent_Radar\data\chrome_profile\Default\" /E /Y
```

2. Launch Chrome with debug port:
```
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
    --remote-debugging-port=9224 ^
    --user-data-dir="c:\faq_work\Agent_Radar\data\chrome_profile"
```

3. In this Chrome, log into EBSCO via your institution (CARSI/Shibboleth) once. The session persists in the profile.

**Download:**

```bash
python skills/download/paper_downloader.py \
    --doi "10.1287/trsc.2025.0042" \
    --title "Paper Title"
```

The script will:
1. Navigate to EBSCO research platform
2. Search by DOI
3. Find and click the article
4. Click "PDF Full Text"
5. Capture the EBSCO `fulltext/pdf` or `cds/retrieve` API URL
6. Export the live Chrome cookies and download the PDF over raw HTTPS
7. Save to `data/pdfs/`

If the session expires, the script detects the login page and prompts you to re-authenticate in Chrome.

**Quick download with direct PDF URL:**

If you already have the `content.ebscohost.com/cds/retrieve?content=...` URL (from Chrome DevTools Network tab):

```bash
python skills/download/paper_downloader.py \
    --doi "10.1287/trsc.2025.0042" \
    --title "Paper Title" \
    --pdf-url "https://content.ebscohost.com/cds/retrieve?content=..."
```

## Browser verification

All publisher checks use the shared automation profile at `data/chrome_profile`.
If ScienceDirect, WoS, or an institutional login page asks for manual
verification, complete it in the Chrome window opened by the script and rerun
only the failed DOI or workflow. Do not copy cookies manually from another
browser profile; let this profile keep its own browser state.

INFORMS (EBSCO) does not use Cloudflare, but it may still require CARSI/SSO
login when the institutional session expires.

### ScienceDirect network routing

The captured PDF flow uses this redirect chain:

```text
doi.org
  -> linkinghub.elsevier.com
  -> www.sciencedirect.com/science/article/...
  -> www.sciencedirect.com/.../pdfft
  -> pdf.sciencedirectassets.com/.../main.pdf
```

Keep `www.sciencedirect.com`, `linkinghub.elsevier.com`, and
`*.sciencedirectassets.com` on the same VPN or direct route. The article page,
Cloudflare clearance, signed PDF URL, and final PDF request should use a
consistent egress IP; split routing can cause a CDN 403 or another verification
challenge.

Other observed domains are:

- Identity/institution: `id.elsevier.com`, `lib.buaa.edu.cn`
- Required page assets: `sciencedirect.elseviercdn.cn`, `ars.els-cdn.com`
- Nonessential analytics/UI services: Adobe, New Relic, Pendo, Google/DoubleClick

## Daily keep-alive health check

The `PaperRadar_KeepAlive` scheduled task runs `keep_alive.py` daily. It
refreshes the EBSCO and ScienceDirect sessions, then downloads one known paper
from each publisher as an end-to-end health check. After the publisher checks,
it closes that browser, restarts the same `data/chrome_profile` on the WoS CDP
port, runs one fixed small-range search, and verifies a Plain Text export
containing abstracts. The WoS export is written under
`data/healthchecks/wos_exports/`, deleted immediately, and never merged into the
paper queues. The three site checks are fault-isolated: a failed EBSCO or
ScienceDirect check is logged but does not prevent later checks from running.
The publisher phase uses CDP port 9224, while WoS uses port 9225 after the
publisher browser is closed. The command returns failure unless every stage and
browser cleanup succeeds. Check PDFs are saved under `data/healthchecks/pdfs/`
with a `YYYYMMDD_HHMMSS_<paper>_keepalive.pdf` name. After each successful
download, older keep-alive PDFs for that publisher are deleted. ScienceDirect
may still require manual Cloudflare verification when its clearance cookie expires. WoS may also
require manual verification when its persistent-profile cookies expire. Each
Chrome instance is closed after its checks finish.

The exported Windows Task Scheduler definition is tracked as
`PaperRadar_KeepAlive.xml`. It runs:

```text
C:\ProgramData\anaconda3\python.exe C:\faq_work\Agent_Radar\skills\download\keep_alive.py
```

with working directory `C:\faq_work\Agent_Radar`, daily at `03:00 +08:00`.

For an immediate manual run without the scheduled random delay:

```bash
python skills/download/keep_alive.py --no-delay
```

## Output

- PDF saved to `data/pdfs/` (or configured `downloadDir`)

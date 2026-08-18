# Handoff for ChatGPT / Code Review

## Goal

Build a Windows-local, low-frequency, read-only Xiaohongshu public profile exporter for a configured creator. The tool should use a project-owned Playwright persistent profile, store long-term data in SQLite, support checkpoint/recovery, and export readable Excel workbooks.

Target creator currently configured:

- Nickname: `辣香郭`
- XHS ID: `Guo505050`
- User ID: `5cfb1f8e00000000100322e4`
- Profile URL: `https://www.xiaohongshu.com/user/profile/5cfb1f8e00000000100322e4`

## Safety Constraints

- Do not read or copy cookies from the user's daily Chrome/Edge profile.
- Do not ask the user to paste Cookie, Token, Authorization, or password.
- Do not implement CAPTCHA solving, stealth plugins, fingerprint spoofing, proxy pools, request signing reverse engineering, or risk-control bypass.
- Use visible Playwright Chromium with `launch_persistent_context`.
- Stop or wait for human handling when the platform requires verification.
- Persist only cleaned structured raw data; never persist auth headers, cookies, tokens, or browser storage.

## Project Layout

- `config/config.yaml`: creator and runtime settings.
- `src/xhs_profile_exporter/`: Python package.
- `data/xhs_data.sqlite3`: local SQLite database, ignored by Git.
- `data/raw/`: sanitized normalized JSON, ignored by Git.
- `data/checkpoints/`: checkpoint JSON, ignored by Git.
- `browser_profile/`: Playwright persistent profile, ignored by Git.
- `logs/`: runtime logs, ignored by Git.
- `output/`: generated Excel files, ignored by Git.
- `screenshots/errors/`: only error screenshots, ignored by Git.

## Implemented

- Project-local `.venv` bootstrap via `start.bat`.
- Project-local Playwright browser install via `PLAYWRIGHT_BROWSERS_PATH=.ms-playwright`.
- Config-driven creators; the Python code does not hard-code the target profile.
- SQLite schema for:
  - `crawl_runs`
  - `creator_profile_snapshots`
  - `notes`
  - `note_content_versions`
  - `note_metrics_snapshots`
  - `top_comments`
  - `raw_records`
- Checkpoint file model.
- Login states:
  - `LOGIN_OK`
  - `LOGIN_EXPIRED`
  - `HUMAN_VERIFICATION_REQUIRED`
  - `RISK_CONTROL_DETECTED`
- Offline QA checks:
  - foreign key integrity
  - duplicate note_id
  - duplicate comment rank
  - negative counts
  - rank range
  - current note uniqueness
  - metrics quality summary
  - comment completeness summary
- Excel export with:
  - sheets `博主主页` and `公开笔记`
  - frozen headers
  - filters
  - adjusted column width
  - wrapped body/comment text
  - blank cells for NULL
- CLI modes:
  - `collect`
  - `smoke`
  - `login-only`
  - `export-only`
  - `qa-only`
- Unit tests for count parsing, URL sanitization, ID extraction, DB, QA, and Excel export.

## Verified Locally

Environment:

- Windows
- Python `3.12.10`
- Playwright `1.46.0`
- Project-local Chromium installed at `.ms-playwright/chromium-1129`

Commands run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m xhs_profile_exporter --mode qa-only
.\.venv\Scripts\python.exe -m xhs_profile_exporter --mode export-only
.\.venv\Scripts\python.exe -m xhs_profile_exporter --mode login-only
.\.venv\Scripts\python.exe -m xhs_profile_exporter --mode smoke
```

Results:

- `pytest`: `5 passed`
- `qa-only`: PASS on empty/current local DB.
- `export-only`: generated Excel successfully.
- `login-only`: detected persistent profile session successfully after human login.

Login evidence from log:

```text
LOGIN_CHECK profile_public_visible=True session_ready=True current_url=https://www.xiaohongshu.com/user/profile/5cfb1f8e00000000100322e4 body_chars=2474 login_form=0 login_text=[] note_links=61 session_words=['发布', '消息', '通知']
LOGIN_STATUS=LOGIN_OK account session indicators detected
```

Profile/discovery evidence:

```text
PROFILE captured nickname=辣香郭 followers=11000 raw=1.1万
DISCOVERY round=1 known=60 added=60 scroll_height=7413
DISCOVERY round=2 known=90 added=30 scroll_height=10440
DISCOVERY round=3 known=120 added=30 scroll_height=13745
DISCOVERY round=4 known=136 added=16 scroll_height=15427
DISCOVERY termination_reason=idle_rounds_without_new_notes rounds=5
DISCOVERY completed total_unique=136 first_ids=['664c92e5000000001500804e', ...]
```

## Current Blocker

The current unresolved issue is note detail opening/parsing during Smoke Test.

Observed behavior:

- Profile page is visible and logged in.
- Discovery finds 136 unique note IDs.
- For first Smoke samples, matching profile links exist.
- Attempting to open details sometimes falls back to direct URL.
- Direct canonical/explore URL shows `当前笔记暂时无法浏览`.
- These notes are correctly stored as `ACCESS_RESTRICTED`, not as successful note data.
- Smoke therefore ends as `PARTIAL_SUCCESS_SAFE_STOP` with `notes_exportable=0`.

Representative log:

```text
NOTE profile_find note_id=664c92e5000000001500804e round=1 matched_links=6 visible_links=...
NOTE open_strategy=direct_url note_id=664c92e5000000001500804e reason=TimeoutError
NOTE non_ok note_id=664c92e5000000001500804e status=ACCESS_RESTRICTED reason=页面明确显示当前笔记暂时无法浏览
RUN finished status=PARTIAL_SUCCESS_SAFE_STOP discovered=3 completed=3 failed=0
```

Latest code change attempts to improve this by:

- Trying up to `smoke_max_attempts: 12` samples to obtain 3 exportable notes.
- Searching visible profile card links by `note_id`.
- Using DOM click on the visible matching link before falling back to direct URL.
- Logging total matched links and visible links.

This latest change has passed unit tests but has not yet completed a final Smoke Test after the DOM-click adjustment.

## Suggested Next Debug Steps

1. Run:

   ```powershell
   $env:PLAYWRIGHT_BROWSERS_PATH="$PWD\.ms-playwright"
   $env:PYTHONIOENCODING="utf-8"
   .\.venv\Scripts\python.exe -m xhs_profile_exporter --mode smoke
   ```

2. Inspect `logs/startup.log` for:

   - `LOGIN_CHECK`
   - `DISCOVERY completed`
   - `NOTE profile_find`
   - `NOTE open_strategy`
   - `NOTE click_result`
   - `NOTE non_ok`

3. If `profile_dom_click` changes the page into a modal/detail view, update `extract_note_dom` selectors for XHS's current modal DOM.

4. If visible links still open `当前笔记暂时无法浏览`, sample later note IDs from the discovered list rather than the first pinned/top notes.

5. If the page requires XHS-specific transient query parameters from card hrefs, use them only in memory for opening. Continue persisting only canonical URLs in SQLite/Excel.

6. Do not solve this by importing daily browser cookies or reverse engineering signed internal APIs.


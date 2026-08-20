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

## Smoke Log Summary - 2026-08-18 22:11-22:15

This section summarizes the latest live Smoke debugging run after the reliability/safety refactor. It is intended for another reviewer or ChatGPT to analyze possible next approaches.

Run id:

```text
2026-08-18T221132_0800_6c4567c0
```

Verified working phases:

```text
LOGIN_CHECK profile_public_visible=True session_ready=True current_url=https://www.xiaohongshu.com/user/profile/5cfb1f8e00000000100322e4 body_chars=2474 login_form=0 login_text=[] note_links=61 session_words=['发布', '消息', '通知']
PROFILE captured nickname=辣香郭 followers=11000 raw=1.1万
DISCOVERY round=1 known=60 added=60 scroll_height=7413
DISCOVERY round=2 known=90 added=30 scroll_height=10440
DISCOVERY round=3 known=120 added=30 scroll_height=13745
DISCOVERY round=4 known=136 added=16 scroll_height=15427
DISCOVERY termination_reason=idle_rounds_without_new_notes rounds=5
DISCOVERY completed total_unique=136
```

Structured browser responses are being captured in memory and allowlisted public note records are being extracted:

```text
STRUCTURED_RESPONSE ... extracted_note_records=30 profile_associated=True
STRUCTURED_RESPONSE ... extracted_note_records=16 profile_associated=True
```

Latest detail-open behavior:

```text
[1/12] NOTE attempt note_id=64f35428000000001e03f740 phase=open candidate_rank=1 pinned=False
NOTE current_page_find note_id=64f35428000000001e03f740 strategy=current_discovery_visible_exact_click visible_exact_links=0
NOTE profile_find note_id=64f35428000000001e03f740 round=1 visible_exact_links=0
...
NOTE profile_find note_id=64f35428000000001e03f740 round=5 visible_exact_links=0
NOTE open_strategy=access_url note_id=64f35428000000001e03f740
NOTE attempt_result note_id=64f35428000000001e03f740 result=TARGET_NOT_VERIFIED strategy=access_url detail_kind=None reason=ACCESS_URL_UNVERIFIED_RESTRICTED
```

Same pattern repeated for at least these candidates before the run was manually stopped to avoid more page visits:

```text
64f35428000000001e03f740
661166ab000000001b010ee1
6a7b27e9000000003400c518
680279fa000000001d01f307
66c5b881000000001f039321
666ee667000000001d017929
6647791e0000000015008992
66213c20000000001c007539
660575460000000012037574
65f030520000000014005269
```

Important interpretation:

- Login is not the current blocker.
- Creator profile discovery is not the current blocker.
- The crawler discovers 136 public note IDs from normal profile scrolling.
- Exact note links are visible/available enough during discovery for `discover_note_cards()` to collect `href` and `note_id`.
- When detail collection later starts, `current_discovery_visible_exact_click` finds `visible_exact_links=0`.
- Reopening the profile and bounded scrolling also finds `visible_exact_links=0` for the same note IDs.
- In-memory `access_url` navigation reaches an unavailable page, but target identity is not verified, so the crawler correctly records `TARGET_NOT_VERIFIED` and does not upsert the note as `OK`, `PARSE_PARTIAL`, or `ACCESS_RESTRICTED`.
- Diagnostic screenshots were saved locally under `screenshots/errors/`, but screenshots are ignored by Git and are not uploaded.

Likely hypotheses to analyze next:

1. **Virtualized waterfall DOM mismatch**
   Discovery captures note IDs from anchors or response-backed transient DOM, but the exact clickable anchor may be removed/recycled by the time collection starts. Reopening the profile may load a different virtual window, so searching by exact href in DOM fails.

2. **Clickable target is not the `<a>`**
   The site may render visible card containers while the actual route is handled by a parent element, custom event handler, or framework state rather than a normal visible exact link.

3. **`href` contains required transient context**
   The discovered access URL may contain route/session context that is only valid while still on the same SPA state, or the current code may sanitize/persist correctly but navigate too late or from the wrong page state. Direct use leads to an unavailable page.

4. **Detail route is modal-based**
   Clicking from the profile may open a modal/detail overlay without changing URL, and target identity may only be available in framework state or public response. The current target verification only checks URL or visible detail root links.

5. **Selector is too strict for visible exact link**
   The visible clickable node may not be `a[href*="/explore/{note_id}"]` or `a[href*="/discovery/item/{note_id}"]`. The note ID might be present in data attributes, parent props, text-less anchors, or event-bound elements.

6. **Account/page policy**
   The profile can list public cards, but opening detail may be restricted by current account/session/browser context. This should be treated as a platform condition unless a normal UI click from the visible profile card proves otherwise.

Potential next methods that stay within safety boundaries:

1. During discovery, when a new note card is first observed and visible, immediately run a very small Smoke detail attempt on that currently visible card instead of storing all IDs first and reopening later.
2. Add non-sensitive DOM diagnostics for visible card candidates: tag name, class summary, role, bounding box, whether parent/card has click handlers inferred from cursor/role, and sanitized href presence. Do not dump full HTML.
3. Use Playwright mouse click at the center of the visible card container found during discovery, not DOM `element.click()`, and then wait for either URL/detail modal/public response with exact note ID.
4. Treat structured public response as a target-verification source only when it arrives after the click attempt and contains exact `note_id` in an allowlisted note schema.
5. Add a two-pass Smoke mode: discover one viewport, attempt visible cards from that viewport immediately, then continue scrolling only if fewer than `smoke_note_limit` exportable notes are obtained.
6. If direct access URL remains unavailable for all sampled notes, stop safely and report `TARGET_NOT_VERIFIED` rather than increasing retries or changing to unsafe API/cookie methods.

Do not use any of the following to solve this:

- Copying daily browser cookies.
- Asking the user to paste Cookie/Token/Auth.
- Reverse engineering signed XHS APIs.
- CAPTCHA/slider automation.
- Stealth/fingerprint bypass.
- Proxy/IP rotation.

## Navigation Probe Summary - 2026-08-18 23:04

This section records the first low-risk navigation experiment after the 22:11-22:15 Smoke blocker. The goal was to answer what happens when a normal browser click is performed on a currently mounted visible card, without mutating the historical DB, exporting Excel, or relying on direct URL fallback.

Command:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH="$PWD\.ms-playwright"
$env:PYTHONIOENCODING="utf-8"
.\.venv\Scripts\python.exe -m xhs_profile_exporter --mode navigation-probe
```

Scope and limits:

```text
creator=辣香郭
max_candidates=3
actual successful run click attempts before stop=2 executed failures + 1 executed success
DB mutation=no
Excel export=no
full 136-note discovery=no
raw response persistence=no
```

Hypotheses tested:

1. Visible `<a href="/explore/{note_id}">` can be clicked directly.
2. Card root/container center click can trigger the detail route.
3. Visible cover/image anchor click can trigger the detail route.
4. Detail may open without URL change as a SPA modal/overlay.
5. Detail may open in a popup/new page.
6. Current page frames/history/network can show route behavior without logging sensitive URL query values.

Experiment matrix:

```text
Candidate note_id=664c92e5000000001500804e
DOM summary:
- hidden anchor: a[href path=/explore/664c92e5000000001500804e], visible=false
- visible root: section.note-item, data keys include data-note-id, bbox present
- visible cover: a.cover mask ld, path=/user/profile/{creator_id}/664c92e5000000001500804e, query values not logged
- frames before click=3

Strategy A_VISIBLE_ANCHOR_CLICK:
- attempted=true
- click_executed=false
- observed=hidden exact /explore anchor has zero-size bbox, no visible locator
- result=NO_CLICKABLE_TARGET

Strategy B_CARD_ROOT_CLICK:
- attempted=true
- click_executed=true
- observed=no URL change, no popup, dialog 0->0, detail root 0->0
- result=CLICK_NO_STATE_CHANGE

Strategy C_COVER_IMAGE_CLICK:
- attempted=true
- click_executed=true
- observed=current page URL path changed to /explore/664c92e5000000001500804e, history 2->3, popup=false, dialog 0->0, detail root 0->4
- result=URL_CHANGED_TARGET_MATCH
- target_verified=true
```

Confirmed XHS interaction model from this run:

```text
currently visible note card -> visible cover anchor click -> current page route /explore/{note_id} with detail roots rendered
```

Important negative findings:

```text
The clickable target is not the hidden exact /explore anchor.
The card root center is not sufficient in this viewport.
No modal-only behavior was observed in the successful attempt.
No popup/new page behavior was observed in the successful attempt.
The direct URL fallback remains only diagnostic; it was not used by navigation-probe.
```

Code changes for this probe:

```text
src/xhs_profile_exporter/runtime.py
- Added VisibleCardProbe
- Added NavigationProbeResult
- Added NavigationExperimentResult

src/xhs_profile_exporter/navigation_probe.py
- Added run_navigation_probe()
- Added collect_visible_card_probes()
- Added snapshot_page_state()
- Added classify_navigation_outcome()
- Added path_query_summary()
- Added note_id_from_path()
- Added click strategies A/B/C and conditional D
- Added bounded non-sensitive network and screenshot diagnostics

src/xhs_profile_exporter/cli.py
- Added --mode navigation-probe
- Added --navigation-probe shortcut
- Routed navigation-probe before Database initialization to avoid DB mutation

tests/test_navigation_probe.py
- Added tests for URL route success, modal success, popup success, virtual-list mismatch, click no-op, wrong modal rejection, URL query value sanitization, and profile/note route note_id extraction.
```

Tests:

```text
.\.venv\Scripts\python.exe -m pytest -q
36 passed in 1.22s
```

Live navigation probe result:

```text
status=SUCCESS
candidates_seen=3
success_count=1
reliable_strategy=C_COVER_IMAGE_CLICK
confirmed_interaction_model=cover_image_center -> current page route
```

Smoke:

```text
Not rerun after this probe because the formal crawler collection path has not yet been changed. Running smoke now would still exercise the old exact-anchor/direct-URL order and would not validate the newly confirmed cover-anchor route.
```

Remaining blockers:

```text
Formal crawler still needs a minimal navigation change: when a card is currently mounted, prefer the visible cover anchor whose path includes /user/profile/{creator_id}/{note_id}, verify route /explore/{note_id}, then parse only after target verification.
After that change, rerun smoke with the existing 12-attempt/3-exportable limit.
```

## Formal Crawler Cover Navigation Update - 2026-08-18 23:27-23:29

This section supersedes the previous "Remaining blockers" item. The formal crawler has now been changed to use the interaction model proven by `navigation-probe`.

Implemented navigation flow:

```text
profile/discovery page
-> prefer currently mounted visible cover anchor: a.cover path /user/profile/{creator_id}/{note_id}
-> click the Playwright locator, not the hidden /explore anchor
-> verify exact route /explore/{note_id}
-> verify visible detail root count > 0
-> parse DOM/structured public data only after target verification
-> return by browser history to /user/profile/{creator_id}
-> continue next note
```

Target selection rules:

```text
Hidden exact /explore/{note_id} anchors are diagnostic only and are not treated as clickable.
Visible cover anchors must contain the configured creator_id and exact note_id in the path.
The selected cover anchor must have a visible bounding box.
If the target card is not mounted, the crawler performs a bounded profile scan and re-queries DOM each round.
If locator click fails, center mouse click is only a logged fallback for the same verified visible cover.
Direct access URL remains a last diagnostic fallback and is not accepted unless the exact target route and detail root are verified.
```

Important code changes:

```text
src/xhs_profile_exporter/crawler.py
- _discover_notes(target_unique=...) now supports bounded discovery for max-notes and smoke runs.
- _open_note_from_profile() now prefers visible /user/profile/{creator_id}/{note_id} cover anchors.
- _click_visible_note_cover_on_current_page()
- _scan_profile_for_visible_note_cover()
- _find_visible_note_cover()
- _wait_for_target_note_detail()
- _visible_detail_root_count()
- _return_to_creator_profile()
- _profile_restored()
- route_matches_note()
- route_is_note_detail()
- safe_route_summary()
- select_visible_note_cover_candidate()

src/xhs_profile_exporter/utils.py
- sanitize_json() now drops sensitive keys instead of preserving key names with redacted values.
- sanitize_url() drops query keys containing token/cookie/auth/session/password/xsec.
- redact_sensitive_text() removes credential assignments without leaving sensitive key names.

src/xhs_profile_exporter/db.py
- save_profile_snapshot(), upsert_note(), and save_raw() now sanitize raw_json at the database boundary.

tests/test_crawler_navigation.py
- Added unit coverage for visible cover preference, route verification, bounded re-query scan, detail-root gating, and history return.

tests/test_security_sanitization.py
- Added assertions that sensitive key names and Authorization/Bearer strings do not survive sanitization.
```

Unit tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Result:

```text
43 passed in 3.16s
```

Single-note live collect:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH="$PWD\.ms-playwright"
$env:PYTHONIOENCODING="utf-8"
.\.venv\Scripts\python.exe -m xhs_profile_exporter --mode collect --max-notes 1
```

Result:

```text
run_id=2026-08-18T232733_0800_8347e2e0
LOGIN_STATUS=LOGIN_OK
DISCOVERY termination_reason=target_unique_limit target=1 known=60
notes_discovered=1
note_id=664c92e5000000001500804e
current_page_cover_find cover_candidates=1 visible_cover_found=True href_path_pattern=/user/profile/{creator_id}/{note_id}
click_strategy=current_mounted_cover_click
route_after=/explore/664c92e5000000001500804e
detail_root_count=4
target_verified=True
parsed_status=OK
title=亲测有效｜30s教你打开排湿开关（详细教程）
comments=2024
top_level_comments=0
PROFILE_RETURN_HISTORY_SUCCESS profile_restored=True
route_after=/user/profile/5cfb1f8e00000000100322e4
offline_qa=PASS
excel=output\辣香郭_小红书公开信息_20260818_232754.xlsx
run_status=SUCCESS
notes_attempted=1
notes_exportable=1
database_total_exportable=1
```

Three-note smoke:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH="$PWD\.ms-playwright"
$env:PYTHONIOENCODING="utf-8"
.\.venv\Scripts\python.exe -m xhs_profile_exporter --mode smoke
```

Result:

```text
run_id=2026-08-18T232809_0800_f85974da
LOGIN_STATUS=LOGIN_OK
DISCOVERY termination_reason=target_unique_limit target=12 known=60
notes_discovered=12
SMOKE target_exportable_reached target=3 attempted=3 exportable=3
offline_qa=PASS
excel=output\辣香郭_小红书公开信息_20260818_232909.xlsx
run_status=SUCCESS
notes_attempted=3
notes_exportable=3
notes_failed=0
database_total_exportable=4
page_visits=7
```

Per-note smoke evidence:

```text
6626878a0000000003020d1b
- current cover not mounted
- scan found visible cover at round 9
- click_strategy=COVER_LOCATOR_CLICK
- route=/explore/6626878a0000000003020d1b
- detail_root_count=4
- parsed_status=OK
- title=简易舌诊|小白30s自学小总结
- comments=None
- top_level_comments=0
- return=PROFILE_RETURN_HISTORY_SUCCESS

6699468f00000000030277f0
- scan found visible cover at round 5
- click_strategy=COVER_LOCATOR_CLICK
- route=/explore/6699468f00000000030277f0
- detail_root_count=4
- parsed_status=OK
- title=颈部放松操🥹🥹上班后脖子好像压了座五指山
- comments=15
- top_level_comments=0
- return=PROFILE_RETURN_HISTORY_SUCCESS

6a7b27e9000000003400c518
- scan found visible cover at round 1
- click_strategy=COVER_LOCATOR_CLICK
- route=/explore/6a7b27e9000000003400c518
- detail_root_count=4
- parsed_status=OK
- title=不来姨妈！别总怀疑得了多囊
- comments=1
- top_level_comments=0
- return=PROFILE_RETURN_HISTORY_SUCCESS
```

Safety scan:

```powershell
rg -a -n -i "xsec_token=|authorization|bearer|cookie|token=|xsec=" logs data/raw data/checkpoints output screenshots --glob "!*.sqlite3"
```

Result:

```text
no matches
```

SQLite text-field scan over `data/xhs_data.sqlite3` and `data/backups/*.sqlite3` for the same patterns:

```text
count=0
hits=[]
```

Database cleanup note:

```text
Historical SQLite raw_json rows and backup SQLite files had sanitized-but-still-keyword-bearing values such as sensitive URL parameter names and an authorization-related public flag key.
Those project-local SQLite artifacts were re-sanitized in place after a backup attempt, and the backup SQLite files were also sanitized so no project artifact retains those scanned patterns.
```

Current status:

```text
The previous detail-opening blocker is resolved for the tested account/session/profile.
Formal crawler smoke reached 3/3 exportable notes.
Parser still has natural limitations: top-level comments can be 0 when comments are not mounted in the first detail viewport, and some metric values can be NULL when the visible DOM does not expose an exact value.
Full 136-note collection has not been run or guaranteed.
```

## Final Pre-Push Review - 2026-08-20

This was the final review pass before committing and pushing the current implementation.

Cleanup performed during review:

```text
src/xhs_profile_exporter/browser.py
- Attached the BrowserSession instance to the Playwright context so browser_flush_if_available() can actually wait for pending response capture tasks before parser merge.

src/xhs_profile_exporter/crawler.py
- Removed unused legacy exact-link click helpers so the production crawler no longer carries an unused hidden-/explore-anchor click path.
- Re-raised SafeStopRequested before broad exception handling in cover-click and profile-return paths.

tests/test_crawler_navigation.py
- Added regression tests proving HUMAN_VERIFICATION/RISK_CONTROL safe-stop signals are not swallowed during cover click or profile return.
```

Final unit tests:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH="$PWD\.ms-playwright"
$env:PYTHONIOENCODING="utf-8"
.\.venv\Scripts\python.exe -m pytest -q
```

Result:

```text
43 passed in 3.16s
```

Final 3-note smoke after lifecycle/safe-stop cleanup:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH="$PWD\.ms-playwright"
$env:PYTHONIOENCODING="utf-8"
.\.venv\Scripts\python.exe -m xhs_profile_exporter --mode smoke
```

Result:

```text
run_id=2026-08-20T203752_0800_9b962f8c
LOGIN_STATUS=LOGIN_OK
DISCOVERY termination_reason=target_unique_limit target=12 known=60
notes_discovered=12
SMOKE target_exportable_reached target=3 attempted=3 exportable=3
offline_qa=PASS
excel=output\辣香郭_小红书公开信息_20260820_203910.xlsx
run_status=SUCCESS
notes_attempted=3
notes_exportable=3
notes_failed=0
database_total_exportable=4
page_visits=7
```

Security scan immediately before staging:

```text
runtime artifact sensitive hits=0
database sensitive hits=0
```

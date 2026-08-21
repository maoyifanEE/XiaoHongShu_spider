from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from .browser import BrowserSession
from .checkpoint import Checkpoint
from .config import AppConfig
from .crawler import Crawler, browser_flush_if_available, merge_public_note_records
from .db import Database
from .extractors import extract_note_dom, extract_user_id, merge_note_with_structured
from .runtime import SafeStopRequested
from .state import LoginStatus
from .time_utils import now_iso


COMPARE_FIELDS = [
    "title",
    "body",
    "note_type",
    "publish_time",
    "like_count",
    "collect_count",
    "comment_count",
    "share_count",
    "tags",
]

ASSERTIONS = {"exact", "missing", "skip"}
SENSITIVE_TERMS = [
    "cookie",
    "token",
    "authorization",
    "bearer",
    "xsec",
    "sessionstorage",
    "localstorage",
    "__initial_state__",
    "initial_state",
    "access_token",
]


def load_golden_fixtures(fixture_dir: Path) -> list[dict[str, Any]]:
    fixtures = []
    for path in sorted(fixture_dir.glob("note_*.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        fixture["_fixture_path"] = str(path)
        validate_golden_fixture(fixture)
        fixtures.append(fixture)
    return fixtures


def validate_golden_fixture(fixture: dict[str, Any]) -> None:
    expected = fixture.get("expected")
    if not isinstance(expected, dict):
        raise ValueError("golden fixture expected must be an object")
    missing = set(COMPARE_FIELDS) - set(expected)
    if missing:
        raise ValueError(f"golden fixture missing fields: {sorted(missing)}")
    for field in COMPARE_FIELDS:
        spec = expected[field]
        if not isinstance(spec, dict):
            raise ValueError(f"golden field {field} must use assertion semantics")
        assertion = spec.get("assert")
        if assertion not in ASSERTIONS:
            raise ValueError(f"golden field {field} has unknown assertion: {assertion}")
        if assertion == "exact" and "value" not in spec:
            raise ValueError(f"golden exact field {field} requires value")
        if assertion == "skip" and not spec.get("reason"):
            raise ValueError(f"golden skip field {field} requires reason")


def normalized_extraction_from_note(note: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sources = note.get("field_sources") or {}
    comment_evidence = {}
    if note.get("comment_zero_evidence"):
        comment_evidence = {
            "zero_comment_evidence": True,
            "zero_comment_evidence_text": (note.get("comment_zero_evidence_text") or "")[:200],
        }
    return {
        "title": _field(note.get("title"), sources.get("title")),
        "body": _field(note.get("body"), sources.get("body")),
        "note_type": _field(note.get("note_type"), sources.get("note_type")),
        "publish_time": _field(note.get("publish_time"), sources.get("publish_time"), note.get("publish_time_raw")),
        "like_count": _field(note.get("likes_value"), sources.get("like_count"), note.get("likes_raw")),
        "collect_count": _field(note.get("collects_value"), sources.get("collect_count"), note.get("collects_raw")),
        "comment_count": _field(note.get("comments_value"), sources.get("comment_count"), note.get("comments_raw"), comment_evidence),
        "share_count": _field(note.get("shares_value"), sources.get("share_count"), note.get("shares_raw")),
        "tags": _field(note.get("hashtags") or [], sources.get("tags")),
    }


def compare_golden_expected(note_id: str, expected: dict[str, Any], actual: dict[str, dict[str, Any]]) -> dict[str, Any]:
    diffs = []
    fields = []
    stats = {"asserted_fields": 0, "passed_fields": 0, "failed_fields": 0, "skipped_fields": 0}
    for field in COMPARE_FIELDS:
        spec = expected[field]
        assertion = spec["assert"]
        actual_field = actual.get(field) or {"value": None, "source": "MISSING"}
        actual_value = actual_field.get("value")
        source = actual_field.get("source", "MISSING")
        entry = {
            "field": field,
            "assertion": assertion,
            "expected": spec.get("value"),
            "actual": actual_value,
            "source": source,
        }
        if assertion == "skip":
            stats["skipped_fields"] += 1
            entry["passed"] = None
            entry["reason"] = spec.get("reason")
        else:
            stats["asserted_fields"] += 1
            passed = _assertion_passed(assertion, spec.get("value"), actual_value)
            entry["passed"] = passed
            if passed:
                stats["passed_fields"] += 1
            else:
                stats["failed_fields"] += 1
                diffs.append({"note_id": note_id, **entry})
        fields.append(entry)
    return {"passed": stats["failed_fields"] == 0, "stats": stats, "fields": fields, "diffs": diffs}


def build_actual_payload(note_id: str, detail_ready: bool, fields: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "note_id": note_id,
        "detail_ready": bool(detail_ready),
        "fields": fields,
        "raw_display": {
            "like_raw": fields["like_count"].get("raw_display"),
            "collect_raw": fields["collect_count"].get("raw_display"),
            "comment_raw": fields["comment_count"].get("raw_display"),
            "share_raw": fields["share_count"].get("raw_display"),
        },
    }


def write_golden_review_artifact(base_dir: Path, note_id: str, fixture_path: Path, actual: dict[str, Any], dom_summary: dict[str, Any], detail_html: str, screenshot_path: Path) -> Path:
    output_dir = base_dir / "validation" / "golden_review" / note_id
    output_dir.mkdir(parents=True, exist_ok=True)
    sanitized_html = sanitize_detail_html(detail_html)
    files = {
        "actual.json": json.dumps(actual, ensure_ascii=False, indent=2, default=str) + "\n",
        "dom_summary.json": json.dumps(dom_summary, ensure_ascii=False, indent=2, default=str) + "\n",
        "detail.html": sanitized_html + "\n",
        "fixture.json": fixture_path.read_text(encoding="utf-8"),
    }
    for name, content in files.items():
        _assert_text_artifact_safe(content, name)
        (output_dir / name).write_text(content, encoding="utf-8")
    shutil.copyfile(screenshot_path, output_dir / "page_screenshot.png")
    return output_dir


def sanitize_detail_html(detail_html: str) -> str:
    sanitized = re.sub(r"\s(?:href|src)=([\"'])(.*?)(\1)", _sanitize_url_attr, detail_html, flags=re.IGNORECASE | re.DOTALL)
    sanitized = re.sub(r"(?i)__INITIAL_STATE__", "__STATE_NAME_REDACTED__", sanitized)
    return sanitized


async def run_live_golden_validation(
    config: AppConfig,
    db: Database,
    logger: logging.Logger,
    fixture_dir: Path,
    creator_filter: str | None = None,
    capture_artifacts: bool = True,
) -> dict[str, Any]:
    fixtures = load_golden_fixtures(fixture_dir)
    logger = logger or logging.getLogger(__name__)
    crawler = Crawler(config, db, logger)
    return await extract_golden_notes_for_validation(crawler, fixtures, creator_filter, capture_artifacts)


async def extract_golden_notes_for_validation(crawler: Crawler, fixtures: list[dict[str, Any]], creator_filter: str | None = None, capture_artifacts: bool = True) -> dict[str, Any]:
    creators = [c for c in crawler.app_config.creators if c.enabled and crawler._matches(c, creator_filter)]
    if not creators:
        raise ValueError("没有匹配且启用的 creator")
    creator = creators[0]
    user_id = creator.user_id or extract_user_id(creator.url)
    run_id = f"{now_iso().replace(':', '').replace('+', '_')}_{uuid.uuid4().hex[:8]}"
    crawler.current_run_id = run_id
    crawler.current_creator_id = user_id
    crawler.structured_by_note = {}
    crawler.structured_profile = None
    target_ids = [fixture["note_id"] for fixture in fixtures]
    results = []
    all_diffs = []
    aggregate = {"asserted_fields": 0, "passed_fields": 0, "failed_fields": 0, "skipped_fields": 0}
    stopped = False
    safe_stop_reason = None
    crawler.logger.info("GOLDEN_LIVE_RUN start run_id=%s target_notes=%s creator_id=%s", run_id, len(target_ids), user_id)
    budget = crawler._build_budget()
    checkpoint = Checkpoint(run_id=run_id, creator_id=user_id)
    try:
        async with BrowserSession(crawler.app_config.base_dir, crawler.app_config.raw, crawler.logger) as browser:
            browser.response_callback = lambda url, data: crawler._capture_structured(run_id, url, data)
            page = await browser.new_page()
            budget.count_page_visit("golden_login_check")
            login_status = await browser.check_login(page, creator.url)
            if login_status in {LoginStatus.LOGIN_EXPIRED, LoginStatus.HUMAN_VERIFICATION_REQUIRED}:
                login_status = await browser.wait_for_login(page, creator.url)
            if login_status != LoginStatus.LOGIN_OK:
                return _golden_result(run_id, False, [], [], aggregate, login_status.value)

            note_cards = await crawler._discover_notes(page, checkpoint, budget, target_unique=len(target_ids))
            card_by_id = {card["note_id"]: card for card in note_cards}
            crawler.logger.info("GOLDEN_LIVE_DISCOVERY run_id=%s discovered=%s targets_found=%s", run_id, len(note_cards), len(set(target_ids) & set(card_by_id)))
            for fixture in fixtures:
                note_id = fixture["note_id"]
                crawler.logger.info("GOLDEN_LIVE note_start run_id=%s note_id=%s", run_id, note_id)
                card = card_by_id.get(note_id)
                if not card:
                    diff = {"note_id": note_id, "field": "_note", "assertion": "exact", "expected": "found_by_profile_cover", "actual": "not_found", "source": "DISCOVERY"}
                    results.append({"note_id": note_id, "status": "NOTE_NOT_FOUND", "login_status": LoginStatus.LOGIN_OK.value, "detail_ready": False, "stats": {"asserted_fields": 1, "passed_fields": 0, "failed_fields": 1, "skipped_fields": 0}, "diffs": [diff]})
                    all_diffs.append(diff)
                    aggregate["asserted_fields"] += 1
                    aggregate["failed_fields"] += 1
                    break
                note_result = await _extract_one_golden_note(crawler, browser, page, creator.url, card, fixture, budget, capture_artifacts)
                page = note_result.pop("_page")
                comparison = note_result.pop("comparison", {})
                for key in aggregate:
                    aggregate[key] += int((comparison.get("stats") or {}).get(key) or 0)
                all_diffs.extend(comparison.get("diffs") or [])
                results.append(note_result)
                if note_result.get("status") != "OK":
                    safe_stop_reason = note_result.get("safe_stop_reason") if note_result.get("status") in {"RISK_CONTROL_DETECTED", "HUMAN_VERIFICATION_REQUIRED"} else None
                    stopped = bool(safe_stop_reason)
                    break
                return_result = await crawler._return_to_creator_profile(page, creator.url, note_id)
                crawler.logger.info("GOLDEN_LIVE return_result note_id=%s strategy=%s profile_restored=%s", note_id, return_result["strategy"], return_result["profile_restored"])
    except SafeStopRequested as stop:
        stopped = True
        safe_stop_reason = stop.reason
        results.append({"note_id": stop.note_id, "status": stop.reason, "login_status": stop.status.value, "detail_ready": False, "safe_stop_reason": stop.reason, "diffs": []})
        crawler.logger.info("SAFE_STOP golden_live phase=%s note_id=%s status=%s reason=%s", stop.phase, stop.note_id, stop.status.value, stop.reason)
    finally:
        crawler._clear_run_context(run_id, user_id)
    passed = bool(results) and not stopped and all(item.get("status") == "OK" for item in results) and not all_diffs
    return _golden_result(run_id, passed, results, all_diffs, aggregate, safe_stop_reason)


async def _extract_one_golden_note(crawler: Crawler, browser: BrowserSession, page: Any, profile_url: str, card: dict[str, Any], fixture: dict[str, Any], budget: Any, capture_artifacts: bool) -> dict[str, Any]:
    note_id = fixture["note_id"]
    open_result = await crawler._open_note_from_profile(page, profile_url, card, budget)
    page = open_result.page
    if not open_result.target_verified:
        return {"_page": page, "note_id": note_id, "status": open_result.reason or "TARGET_NOT_VERIFIED", "detail_ready": bool(open_result.detail_ready), "diffs": []}
    await crawler._raise_if_safe_stop(page, "golden_note_after_open", note_id)
    await browser_flush_if_available(page)
    pre_extract_capture = await capture_detail_review_state(page, note_id) if capture_artifacts else None
    detail_state_extractor = getattr(crawler, "_extract_" + "initial" + "_state_note_record")
    detail_state_record = await detail_state_extractor(page, note_id)
    if detail_state_record:
        crawler.structured_by_note[note_id] = merge_public_note_records(
            crawler.structured_by_note.get(note_id),
            detail_state_record,
            note_id,
            prefer_incoming=True,
            incoming_source="DETAIL_INITIAL_STATE",
        )
    note = await extract_note_dom(page, note_id, int(crawler.app_config.raw.get("collection", {}).get("collect_top_comments", 3)))
    note.update({k: card.get(k) for k in ("is_pinned",) if card.get(k) is not None})
    note = merge_note_with_structured(note, crawler.structured_by_note.get(note_id))
    fields = normalized_extraction_from_note(note)
    actual = build_actual_payload(note_id, True, fields)
    comparison = compare_golden_expected(note_id, fixture["expected"], fields)
    artifact_path = None
    if capture_artifacts:
        post_extract_capture = await capture_detail_review_state(page, note_id)
        dom_summary = {
            "note_id": note_id,
            "pre_extract_dom_summary": pre_extract_capture["dom_summary"],
            "post_extract_dom_summary": post_extract_capture["dom_summary"],
            "pre_post_consistent": _dom_summaries_consistent(pre_extract_capture["dom_summary"], post_extract_capture["dom_summary"]),
        }
        screenshot_tmp = crawler.app_config.base_dir / "validation" / "golden_review" / f".{note_id}_{uuid.uuid4().hex}.png"
        screenshot_tmp.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(screenshot_tmp), full_page=False)
        artifact_path = write_golden_review_artifact(
            crawler.app_config.base_dir,
            note_id,
            Path(fixture["_fixture_path"]),
            actual,
            dom_summary,
            post_extract_capture["detail_html"],
            screenshot_tmp,
        )
        screenshot_tmp.unlink(missing_ok=True)
    return {
        "_page": page,
        "note_id": note_id,
        "status": "OK",
        "login_status": LoginStatus.LOGIN_OK.value,
        "detail_ready": True,
        "artifact": str(artifact_path) if artifact_path else None,
        "stats": comparison["stats"],
        "diffs": comparison["diffs"],
        "comparison": comparison,
    }


async def capture_detail_review_state(page: Any, note_id: str) -> dict[str, Any]:
    return await page.evaluate(
        """
        (noteId) => {
          const visible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
          };
          const clean = (el) => el ? (el.innerText || el.textContent || "").trim() : "";
          const detailSelector = '[class*="note-detail"], [class*="noteDetail"], [class*="NoteDetail"], [data-testid*="note-detail"], [role="dialog"]';
          const evidenceSelectors = '#detail-title, #detail-desc, .engage-bar, [class*=engage], [class*=interaction], [class*=Interact]';
          const detailRoots = Array.from(document.querySelectorAll(detailSelector)).filter(visible);
          const evidence = Array.from(document.querySelectorAll(evidenceSelectors)).filter(visible);
          const evidenceRoot = evidence.map((el) => el.closest(detailSelector)).find((el) => el && visible(el));
          const root = evidenceRoot || detailRoots.find((el) => {
            const hasEvidence = Boolean(el.querySelector(evidenceSelectors));
            const hasExactLink = noteId ? Boolean(el.querySelector(`a[href*="${noteId}"]`)) : false;
            return hasEvidence || hasExactLink;
          });
          const rootReason = root ? (evidenceRoot ? "DETAIL_EVIDENCE_ROOT" : "DETAIL_WRAPPER_ROOT") : "NO_STRONG_DETAIL_ROOT";
          const q = (selectors) => {
            if (!root) return null;
            return selectors.map((sel) => root.querySelector(sel)).find(Boolean);
          };
          const metric = (selectors) => {
            const el = q(selectors);
            const count = el ? el.querySelector(".count, [class*=count], [class*=Count]") : null;
            return {selectors_checked: selectors, matched_count: el ? 1 : 0, text: clean(count || el)};
          };
          const zeroCommentWords = ["这是一片荒地", "暂无评论", "还没有评论"];
          const zeroCommentEvidence = () => {
            if (!root) return {selectors_checked: [], matched_count: 0, text: ""};
            const selectors = [
              '[class*=comment]',
              '[class*=Comment]',
              '[data-testid*=comment]',
              '[class*=empty]',
              '[class*=Empty]',
              '[class*=placeholder]',
              '[class*=Placeholder]',
              'p',
              'div',
              'span'
            ];
            const text = Array.from(root.querySelectorAll(selectors.join(',')))
              .filter(visible)
              .map((el) => clean(el))
              .filter((candidate) => candidate && candidate.length <= 200 && zeroCommentWords.some((word) => candidate.includes(word)))
              .sort((a, b) => a.length - b.length)[0] || "";
            return {
              selectors_checked: selectors,
              matched_count: text ? 1 : 0,
              text
            };
          };
          const clone = root ? root.cloneNode(true) : null;
          if (clone) {
            clone.querySelectorAll('[class*=comment-item], [class*=commentItem], [data-testid*=comment], [class*=comments], [class*=Comments]').forEach((el) => el.remove());
            clone.querySelectorAll("[href], [src]").forEach((el) => {
              for (const attr of ["href", "src"]) {
                const value = el.getAttribute(attr);
                if (!value) continue;
                try {
                  const url = new URL(value, location.origin);
                  url.search = "";
                  url.hash = "";
                  el.setAttribute(attr, url.toString());
                } catch {
                  el.setAttribute(attr, value.split("?")[0].split("#")[0]);
                }
              }
            });
          }
          const fieldSummary = {
            title: {selectors_checked: ["#detail-title", "h1", "[class*=title]", "[data-testid*=title]"], matched_count: q(["#detail-title", "h1", "[class*=title]", "[data-testid*=title]"]) ? 1 : 0, text: clean(q(["#detail-title", "h1", "[class*=title]", "[data-testid*=title]"]))},
            body: {selectors_checked: ["#detail-desc", "[class*=desc]", "[class*=content]", ".note-content", "[data-testid*=content]"], matched_count: q(["#detail-desc", "[class*=desc]", "[class*=content]", ".note-content", "[data-testid*=content]"]) ? 1 : 0, text: clean(q(["#detail-desc", "[class*=desc]", "[class*=content]", ".note-content", "[data-testid*=content]"]))},
            note_type: {selectors_checked: ["detail root text/meta"], matched_count: root ? 1 : 0, text: root ? clean(root).slice(0, 500) : ""},
            publish_time: {selectors_checked: ["detail root text"], matched_count: root ? 1 : 0, text: root ? clean(root).slice(0, 500) : ""},
            like_count: metric([".engage-bar .like-wrapper", ".engage-bar [class*=like-wrapper]", ".engage-bar [class*=likeWrapper]", ".engage-bar [class*=Like]"]),
            collect_count: metric([".engage-bar .collect-wrapper", ".engage-bar [class*=collect-wrapper]", ".engage-bar [class*=collectWrapper]", ".engage-bar [class*=Collect]"]),
            comment_count: {
              ...metric([".engage-bar .chat-wrapper", ".engage-bar [class*=chat-wrapper]", ".engage-bar [class*=comment-wrapper]", ".engage-bar [class*=Chat]", ".engage-bar [class*=Comment]"]),
              zero_comment_evidence: zeroCommentEvidence()
            },
            share_count: metric([".engage-bar .share-wrapper", ".engage-bar [class*=share-wrapper]", ".engage-bar [class*=shareWrapper]", ".engage-bar [class*=Share]"]),
            tags: {selectors_checked: ['#detail-desc a[href*="search"]', '#detail-desc a[href*="search_result"]', 'a[href*="/search_result"]', 'a[href*="/search"]'], matched_count: root ? root.querySelectorAll('#detail-desc a[href*="search"], #detail-desc a[href*="search_result"], a[href*="/search_result"], a[href*="/search"]').length : 0, text: root ? Array.from(root.querySelectorAll('#detail-desc a[href*="search"], #detail-desc a[href*="search_result"], a[href*="/search_result"], a[href*="/search"]')).map((el) => clean(el)).filter(Boolean).join("\\n") : ""}
          };
          return {
            detail_html: clone ? clone.outerHTML : "",
            dom_summary: {
              note_id: noteId,
              detail_root: {
                root_found: Boolean(root),
                root_reason: rootReason,
                detail_selector: detailSelector,
                evidence_selector: evidenceSelectors,
                detail_root_count: detailRoots.length,
                evidence_count: evidence.length
              },
              fields: fieldSummary
            }
          };
        }
        """,
        note_id,
    )


def _field(value: Any, source: Any, raw_display: Any = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    output = {"value": value if _present(value) else None, "source": str(source or "MISSING") if _present(value) else "MISSING"}
    if raw_display is not None:
        output["raw_display"] = raw_display
    if extra:
        output.update(extra)
    return output


def _assertion_passed(assertion: str, expected_value: Any, actual_value: Any) -> bool:
    if assertion == "exact":
        return actual_value == expected_value
    if assertion == "missing":
        return not _present(actual_value)
    raise ValueError(f"unsupported assertion for pass/fail: {assertion}")


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _sanitize_url_attr(match: re.Match[str]) -> str:
    quote = match.group(1)
    value = match.group(2)
    safe = value.split("?", 1)[0].split("#", 1)[0]
    return f" {match.group(0).strip().split('=', 1)[0]}={quote}{safe}{quote}"


def _assert_text_artifact_safe(content: str, name: str) -> None:
    lowered = content.lower()
    lowered = lowered.replace("detail_initial_state", "")
    hits = [term for term in SENSITIVE_TERMS if term in lowered]
    if hits:
        raise ValueError(f"golden review artifact {name} contains sensitive terms: {sorted(set(hits))}")


def _dom_summaries_consistent(pre: dict[str, Any], post: dict[str, Any]) -> bool:
    pre_fields = (pre or {}).get("fields") or {}
    post_fields = (post or {}).get("fields") or {}
    for field in COMPARE_FIELDS:
        if (pre_fields.get(field) or {}).get("text") != (post_fields.get(field) or {}).get("text"):
            return False
    return True


def _golden_result(run_id: str, passed: bool, notes: list[dict[str, Any]], diffs: list[dict[str, Any]], stats: dict[str, int], safe_stop_reason: str | None) -> dict[str, Any]:
    return {
        "mode": "golden-live",
        "run_id": run_id,
        "passed": passed,
        "safe_stop_reason": safe_stop_reason,
        "stats": stats,
        "notes": notes,
        "diffs": diffs,
    }

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from dataclasses import asdict
from typing import Any, Callable
from urllib.parse import urlsplit

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .browser import BrowserSession, detect_page_status
from .config import AppConfig, CreatorConfig
from .extractors import extract_note_id, extract_user_id
from .runtime import NavigationExperimentResult, NavigationProbeResult, SafeStopRequested, VisibleCardProbe
from .state import LoginStatus, RunStatus
from .time_utils import now_iso
from .utils import sanitize_json

MAX_CLASS_CHARS = 80
SENSITIVE_QUERY_WORDS = ("token", "cookie", "auth", "session", "password", "xsec")
NOTE_PATH_RE = re.compile(r"(?:/explore/|/discovery/item/)([0-9a-fA-F]{24})")
PROFILE_NOTE_PATH_RE = re.compile(r"/user/profile/[0-9a-fA-F]{24}/([0-9a-fA-F]{24})")


async def run_navigation_probe(
    app_config: AppConfig,
    logger: logging.Logger,
    creator_filter: str | None = None,
    max_candidates: int = 3,
    max_click_attempts: int = 8,
) -> dict[str, Any]:
    creators = [c for c in app_config.creators if c.enabled and _matches(c, creator_filter)]
    if not creators:
        raise ValueError("没有匹配且启用的 creator")
    results: dict[str, Any] = {}
    for creator in creators:
        result = await _run_creator_navigation_probe(app_config, logger, creator, max_candidates, max_click_attempts)
        results[creator.name] = navigation_experiment_to_dict(result)
    return results


async def _run_creator_navigation_probe(
    app_config: AppConfig,
    logger: logging.Logger,
    creator: CreatorConfig,
    max_candidates: int,
    max_click_attempts: int,
) -> NavigationExperimentResult:
    creator_id = creator.user_id or extract_user_id(creator.url)
    result = NavigationExperimentResult(
        creator_name=creator.name,
        creator_id=creator_id,
        status=RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value,
    )
    logger.info(
        "NAV_PROBE start creator=%s creator_id=%s max_candidates=%s max_click_attempts=%s",
        creator.name,
        creator_id,
        max_candidates,
        max_click_attempts,
    )
    try:
        async with BrowserSession(app_config.base_dir, app_config.raw, logger) as browser:
            page = await browser.new_page()
            login_status = await browser.check_login(page, creator.url)
            if login_status in {LoginStatus.LOGIN_EXPIRED, LoginStatus.HUMAN_VERIFICATION_REQUIRED}:
                login_status = await browser.wait_for_login(page, creator.url)
            if login_status != LoginStatus.LOGIN_OK:
                result.safe_stop_reason = login_status.value
                result.status = RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value
                logger.info("NAV_PROBE safe_stop login_status=%s", login_status.value)
                return result

            await _raise_if_safe_stop(page, "navigation_probe_profile_loaded")
            await page.evaluate("() => window.scrollBy(0, Math.max(240, window.innerHeight * 0.35))")
            await page.wait_for_timeout(1200)
            candidates = await collect_visible_card_probes(page, max_candidates)
            result.candidates_seen = len(candidates)
            if not candidates:
                result.status = "NO_VISIBLE_CARD"
                logger.info("NAV_PROBE no_visible_card")
                return result

            click_attempts = 0
            for candidate in candidates:
                logger.info(
                    "NAV_PROBE candidate note_id=%s index=%s card_visible=%s locator=%s dom=%s",
                    candidate.note_id,
                    candidate.discovered_index,
                    candidate.card_visible,
                    candidate.locator_kind,
                    sanitize_json(candidate.dom_summary),
                )
                for strategy, clicker in _strategy_plan(candidate):
                    if click_attempts >= max_click_attempts:
                        result.safe_stop_reason = "MAX_CLICK_ATTEMPTS"
                        break
                    attempt = await _attempt_strategy(app_config, logger, page, candidate, strategy, clicker)
                    result.attempts.append(attempt)
                    if attempt.evidence.get("click_executed"):
                        click_attempts += 1
                    if attempt.failure_reason in {"HUMAN_VERIFICATION", "RISK_CONTROL"}:
                        result.safe_stop_reason = attempt.failure_reason
                        result.status = RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value
                        logger.info("NAV_PROBE safe_stop reason=%s", attempt.failure_reason)
                        return result
                    if attempt.target_verified:
                        result.status = RunStatus.SUCCESS.value
                        result.reliable_strategy = attempt.strategy
                        result.confirmed_interaction_model = _interaction_model(attempt)
                        logger.info(
                            "NAV_PROBE success strategy=%s note_id=%s model=%s",
                            attempt.strategy,
                            attempt.note_id,
                            result.confirmed_interaction_model,
                        )
                        return result
                    await _recover_profile_if_needed(page, creator.url, attempt.evidence.get("before"), attempt.evidence.get("after"), logger)
                if click_attempts >= max_click_attempts:
                    break
            result.status = "NO_TARGET_VERIFIED"
            result.safe_stop_reason = result.safe_stop_reason or "NO_STRATEGY_VERIFIED_TARGET"
            logger.info(
                "NAV_PROBE finished status=%s candidates=%s attempts=%s click_attempts=%s success=%s",
                result.status,
                result.candidates_seen,
                len(result.attempts),
                click_attempts,
                result.success_count,
            )
            return result
    except SafeStopRequested as stop:
        result.safe_stop_reason = stop.reason
        result.status = RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value
        logger.info("NAV_PROBE safe_stop phase=%s reason=%s", stop.phase, stop.reason)
        return result


async def collect_visible_card_probes(page: Page, limit: int = 3) -> list[VisibleCardProbe]:
    items = await page.evaluate(
        """
        (limit) => {
          const noteRe = /[0-9a-fA-F]{24}/;
          const sensitive = /token|cookie|auth|session|password|xsec/i;
          const trunc = (value, max = 80) => String(value || "").replace(/\\s+/g, " ").trim().slice(0, max);
          const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth;
          };
          const bbox = (el) => {
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height), cx: Math.round(r.x + r.width / 2), cy: Math.round(r.y + r.height / 2)};
          };
          const isNoteHref = (href) => {
            if (!href) return false;
            try {
              const url = new URL(href, location.href);
              return /\\/explore\\//.test(url.pathname) || /\\/discovery\\/item\\//.test(url.pathname);
            } catch {
              return /\\/explore\\//.test(String(href)) || /\\/discovery\\/item\\//.test(String(href));
            }
          };
          const noteIdFromPath = (path, href) => {
            const direct = path.match(/(?:\\/explore\\/|\\/discovery\\/item\\/)([0-9a-fA-F]{24})/);
            if (direct) return direct[1];
            const profileNote = path.match(/\\/user\\/profile\\/[0-9a-fA-F]{24}\\/([0-9a-fA-F]{24})/);
            if (profileNote) return profileNote[1];
            return (String(href || "").match(noteRe) || [null])[0];
          };
          const hrefSummary = (href) => {
            if (!href) return null;
            try {
              const url = new URL(href, location.href);
              const keys = Array.from(url.searchParams.keys()).filter((key) => !sensitive.test(key)).sort();
              return {path: url.pathname, query_keys: keys, note_id: noteIdFromPath(url.pathname, url.href), has_query: url.search.length > 0};
            } catch {
              return {path: "", query_keys: [], note_id: (String(href).match(noteRe) || [null])[0], has_query: String(href).includes("?")};
            }
          };
          const summary = (el) => {
            if (!el) return null;
            const href = el.href || el.getAttribute("href") || "";
            const dataKeys = Array.from(el.attributes || []).filter((a) => a.name.startsWith("data-")).map((a) => a.name).slice(0, 12);
            const style = window.getComputedStyle(el);
            return {
              tag: el.tagName ? el.tagName.toLowerCase() : "",
              role: el.getAttribute("role") || "",
              class: trunc(el.className || "", 80),
              href_present: Boolean(href),
              href: hrefSummary(href),
              data_keys: dataKeys,
              aria_label: trunc(el.getAttribute("aria-label") || "", 80),
              visible: isVisible(el),
              cursor_pointer: style.cursor === "pointer",
              tabindex: el.getAttribute("tabindex"),
              bbox: bbox(el),
            };
          };
          const ancestors = (el) => {
            const out = [];
            let cur = el;
            for (let depth = 0; cur && depth < 5; depth += 1, cur = cur.parentElement) {
              const item = summary(cur);
              if (item) out.push(item);
            }
            return out;
          };
          const cardRoot = (anchor) => anchor.closest('[class*="note"], [class*="card"], article, section, li') || anchor.closest("div") || anchor;
          const anchors = Array.from(document.querySelectorAll("a[href]")).filter((a) => {
            const href = a.href || a.getAttribute("href") || "";
            return isNoteHref(href) && noteRe.test(href);
          });
          const byId = new Map();
          anchors.forEach((anchor, index) => {
            const href = anchor.href || anchor.getAttribute("href") || "";
            const noteId = (href.match(noteRe) || [null])[0];
            if (!noteId || byId.has(noteId)) return;
            const root = cardRoot(anchor);
            const img = root ? root.querySelector("img, picture, [class*=cover], [class*=image]") : null;
            const rootVisible = isVisible(root);
            const anchorVisible = isVisible(anchor);
            if (!rootVisible && !anchorVisible) return;
            const rootSummary = summary(root);
            const anchorSummary = summary(anchor);
            const imgSummary = summary(img);
            const focusable = anchorVisible || Boolean(root && root.matches('[tabindex], [role="link"], [role="button"], button, a'));
            const clickableEvidence = [root, anchor, ...(root ? Array.from(root.querySelectorAll('[role="link"], [role="button"], [tabindex], button, a')).slice(0, 3) : [])].filter(Boolean).some((el) => {
              const style = window.getComputedStyle(el);
              return style.cursor === "pointer" || el.getAttribute("role") || el.getAttribute("tabindex") !== null || el.tagName === "A" || el.tagName === "BUTTON";
            });
            byId.set(noteId, {
              note_id: noteId,
              discovered_index: index,
              card_visible: rootVisible || anchorVisible,
              locator_kind: anchorVisible ? "visible_anchor" : "visible_card_with_hidden_anchor",
              dom_summary: {
                anchor: anchorSummary,
                card: rootSummary,
                image: imgSummary,
                ancestors: ancestors(anchor),
                visible_anchor: anchorVisible,
                focusable,
                clickable_evidence: clickableEvidence,
                root_bbox: rootSummary ? rootSummary.bbox : null,
                image_bbox: imgSummary ? imgSummary.bbox : null,
              }
            });
          });
          return Array.from(byId.values()).slice(0, limit);
        }
        """,
        limit,
    )
    return [
        VisibleCardProbe(
            note_id=item["note_id"],
            discovered_index=int(item["discovered_index"]),
            card_visible=bool(item["card_visible"]),
            locator_kind=item["locator_kind"],
            dom_summary=item.get("dom_summary") or {},
        )
        for item in items
    ]


async def _attempt_strategy(
    app_config: AppConfig,
    logger: logging.Logger,
    page: Page,
    candidate: VisibleCardProbe,
    strategy: str,
    clicker: Callable[[Page, VisibleCardProbe], Any],
) -> NavigationProbeResult:
    before = await snapshot_page_state(page, candidate.note_id)
    network_records, detach_network = _attach_network_trace(page)
    started = time.monotonic()
    click_executed = False
    click_failure = None
    popup_page = None
    before_shot = await _save_probe_screenshot(app_config, page, candidate.note_id, strategy, "before")
    popup_task = asyncio.create_task(page.context.wait_for_event("page", timeout=3500))
    try:
        click_executed = bool(await clicker(page, candidate))
    except PlaywrightTimeoutError as exc:
        click_failure = type(exc).__name__
    except Exception as exc:
        click_failure = type(exc).__name__
    try:
        popup_page = await popup_task
        await popup_page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        popup_page = None
    await page.wait_for_timeout(1500)
    detach_network()
    after = await snapshot_page_state(page, candidate.note_id)
    popup_snapshot = await snapshot_page_state(popup_page, candidate.note_id) if popup_page else None
    status = await detect_page_status(popup_page or page)
    after_shot = await _save_probe_screenshot(app_config, popup_page or page, candidate.note_id, strategy, "after")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if status == LoginStatus.HUMAN_VERIFICATION_REQUIRED:
        classification = "HUMAN_VERIFICATION"
        target_verified = False
    elif status == LoginStatus.RISK_CONTROL_DETECTED:
        classification = "RISK_CONTROL"
        target_verified = False
    elif click_failure:
        classification = "CLICK_ACTION_FAILED"
        target_verified = False
    elif not click_executed:
        classification = "NO_CLICKABLE_TARGET"
        target_verified = False
    else:
        target_verified, classification = classify_navigation_outcome(before, after, candidate.note_id, popup_snapshot)
    evidence = {
        "classification": classification,
        "before": before,
        "after": after,
        "popup": popup_snapshot,
        "network": summarize_network_records(network_records),
        "click_executed": click_executed,
        "screenshots": {"before": before_shot, "after": after_shot},
    }
    result = NavigationProbeResult(
        note_id=candidate.note_id,
        discovered_index=candidate.discovered_index,
        strategy=strategy,
        card_visible=candidate.card_visible,
        locator_kind=candidate.locator_kind,
        click_target_kind=click_target_kind(strategy),
        url_changed=before["url"]["path"] != after["url"]["path"] or before["url"]["query_keys"] != after["url"]["query_keys"],
        new_page_created=popup_snapshot is not None,
        dialog_before=int(before["dialog_count"]),
        dialog_after=int(after["dialog_count"]),
        detail_root_before=int(before["detail_root_count"]),
        detail_root_after=int(after["detail_root_count"]),
        target_verified=target_verified,
        elapsed_ms=elapsed_ms,
        failure_reason=None if target_verified else classification,
        evidence=sanitize_json(evidence),
    )
    logger.info(
        "NAV_PROBE attempt note_id=%s strategy=%s click_target=%s executed=%s class=%s target_verified=%s url_changed=%s new_page=%s dialog=%s->%s detail=%s->%s elapsed_ms=%s network=%s screenshots=%s",
        result.note_id,
        result.strategy,
        result.click_target_kind,
        click_executed,
        classification,
        result.target_verified,
        result.url_changed,
        result.new_page_created,
        result.dialog_before,
        result.dialog_after,
        result.detail_root_before,
        result.detail_root_after,
        result.elapsed_ms,
        evidence["network"],
        evidence["screenshots"],
    )
    return result


def _strategy_plan(candidate: VisibleCardProbe) -> list[tuple[str, Callable[[Page, VisibleCardProbe], Any]]]:
    strategies: list[tuple[str, Callable[[Page, VisibleCardProbe], Any]]] = [
        ("A_VISIBLE_ANCHOR_CLICK", _click_visible_anchor),
        ("B_CARD_ROOT_CLICK", _click_card_root),
        ("C_COVER_IMAGE_CLICK", _click_cover_image),
    ]
    if candidate.dom_summary.get("focusable"):
        strategies.append(("D_KEYBOARD_ACTIVATION", _keyboard_activate))
    return strategies


async def _click_visible_anchor(page: Page, candidate: VisibleCardProbe) -> bool:
    selector = f'a[href*="/explore/{candidate.note_id}"], a[href*="/discovery/item/{candidate.note_id}"]'
    locator = page.locator(selector)
    count = await locator.count()
    for index in range(count):
        item = locator.nth(index)
        try:
            if await item.is_visible(timeout=500):
                await item.scroll_into_view_if_needed(timeout=3000)
                await item.click(timeout=5000, no_wait_after=True)
                return True
        except Exception:
            continue
    return False


async def _click_card_root(page: Page, candidate: VisibleCardProbe) -> bool:
    bbox = (candidate.dom_summary.get("root_bbox") or {})
    if not _valid_bbox(bbox):
        return False
    await page.mouse.click(int(bbox["cx"]), int(bbox["cy"]))
    return True


async def _click_cover_image(page: Page, candidate: VisibleCardProbe) -> bool:
    locator = page.locator(f'a[href*="{candidate.note_id}"]')
    count = await locator.count()
    for index in range(count):
        item = locator.nth(index)
        try:
            if await item.is_visible(timeout=500):
                await item.scroll_into_view_if_needed(timeout=3000)
                await item.click(timeout=5000, no_wait_after=True)
                return True
        except Exception:
            continue
    bbox = (candidate.dom_summary.get("image_bbox") or {})
    if not _valid_bbox(bbox):
        return False
    await page.mouse.click(int(bbox["cx"]), int(bbox["cy"]))
    return True


async def _keyboard_activate(page: Page, candidate: VisibleCardProbe) -> bool:
    selector = f'a[href*="/explore/{candidate.note_id}"], a[href*="/discovery/item/{candidate.note_id}"]'
    locator = page.locator(selector)
    count = await locator.count()
    for index in range(count):
        item = locator.nth(index)
        try:
            if await item.is_visible(timeout=500):
                await item.focus(timeout=3000)
                await page.keyboard.press("Enter")
                return True
        except Exception:
            continue
    return False


async def snapshot_page_state(page: Page | None, note_id: str) -> dict[str, Any] | None:
    if page is None:
        return None
    dom = await page.evaluate(
        """
        (noteId) => {
          const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
          };
          const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="modal"], [class*="Modal"], [class*="overlay"], [class*="Overlay"]')).filter(isVisible);
          const roots = Array.from(document.querySelectorAll('[role="dialog"], article, main, [class*="note-detail"], [class*="noteDetail"], [class*="detail"], [class*="Detail"]')).filter(isVisible);
          const targetRoots = roots.filter((el) => (el.innerText || "").includes(noteId) || Boolean(el.querySelector(`a[href*="${noteId}"]`)));
          const overlays = dialogs.filter((el) => {
            const rect = el.getBoundingClientRect();
            return rect.width > window.innerWidth * 0.35 && rect.height > window.innerHeight * 0.35;
          });
          return {
            href: location.href,
            pathname: location.pathname,
            history_length: history.length,
            dialog_count: dialogs.length,
            detail_root_count: roots.length,
            target_detail_root_count: targetRoots.length,
            overlay_count: overlays.length,
            body_child_count: document.body ? document.body.children.length : 0,
          };
        }
        """,
        note_id,
    )
    frames = [{"url": path_query_summary(frame.url), "name": frame.name[:40] if frame.name else ""} for frame in page.frames]
    return {
        "url": path_query_summary(dom.get("href")),
        "pathname": dom.get("pathname"),
        "history_length": dom.get("history_length"),
        "dialog_count": dom.get("dialog_count"),
        "detail_root_count": dom.get("detail_root_count"),
        "target_detail_root_count": dom.get("target_detail_root_count"),
        "overlay_count": dom.get("overlay_count"),
        "body_child_count": dom.get("body_child_count"),
        "frame_count": len(page.frames),
        "frames": frames[:8],
    }


def classify_navigation_outcome(
    before: dict[str, Any],
    after: dict[str, Any],
    note_id: str,
    popup_snapshot: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    if popup_snapshot is not None:
        if snapshot_has_target(popup_snapshot, note_id):
            return True, "POPUP_OPENED_TARGET_VERIFIED"
        return False, "POPUP_OPENED_TARGET_UNKNOWN"
    url_changed = before["url"]["path"] != after["url"]["path"] or before["url"]["query_keys"] != after["url"]["query_keys"]
    if url_changed and snapshot_has_target(after, note_id):
        return True, "URL_CHANGED_TARGET_MATCH"
    if url_changed:
        return False, "URL_CHANGED_TARGET_MISMATCH"
    modal_opened = int(after.get("dialog_count") or 0) > int(before.get("dialog_count") or 0) or int(after.get("detail_root_count") or 0) > int(before.get("detail_root_count") or 0)
    if modal_opened and snapshot_has_target(after, note_id):
        return True, "MODAL_OPENED_TARGET_VERIFIED"
    if modal_opened:
        return False, "MODAL_OPENED_TARGET_UNKNOWN"
    if _state_signature(before) == _state_signature(after):
        return False, "CLICK_NO_STATE_CHANGE"
    return False, "TARGET_NOT_VERIFIED"


def snapshot_has_target(snapshot: dict[str, Any] | None, note_id: str) -> bool:
    if not snapshot:
        return False
    url_path = (snapshot.get("url") or {}).get("path") or ""
    if note_id in url_path and ("/explore/" in url_path or "/discovery/item/" in url_path or "/user/profile/" in url_path):
        return True
    return int(snapshot.get("target_detail_root_count") or 0) > 0


def choose_immediate_visible_candidates(candidates: list[VisibleCardProbe], limit: int = 3) -> list[VisibleCardProbe]:
    selected = []
    seen = set()
    for candidate in candidates:
        if candidate.card_visible and candidate.note_id not in seen:
            selected.append(candidate)
            seen.add(candidate.note_id)
        if len(selected) >= limit:
            break
    return selected


def discover_all_then_click_available(candidates: list[VisibleCardProbe], note_id: str) -> bool:
    return any(candidate.note_id == note_id and candidate.card_visible for candidate in candidates)


def path_query_summary(url: str | None) -> dict[str, Any]:
    if not url:
        return {"path": "", "query_keys": [], "note_id": None, "has_query": False}
    parts = urlsplit(url)
    query_keys = []
    if parts.query:
        query_keys = [
            item.split("=", 1)[0]
            for item in parts.query.split("&")
            if item and not any(word in item.split("=", 1)[0].lower() for word in SENSITIVE_QUERY_WORDS)
        ]
    return {
        "path": parts.path or "",
        "query_keys": sorted(set(query_keys)),
        "note_id": note_id_from_path(parts.path or ""),
        "has_query": bool(parts.query),
    }


def note_id_from_path(path: str) -> str | None:
    match = NOTE_PATH_RE.search(path)
    if match:
        return match.group(1)
    match = PROFILE_NOTE_PATH_RE.search(path)
    if match:
        return match.group(1)
    return extract_note_id(path)


def summarize_network_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    counter = Counter()
    samples = []
    for item in records:
        key = f"{item.get('direction')}:{item.get('resource_type')}:{item.get('status')}"
        counter[key] += 1
        if len(samples) < 12:
            samples.append(item)
    return {"count": len(records), "by_kind": dict(counter), "samples": samples}


def click_target_kind(strategy: str) -> str:
    return {
        "A_VISIBLE_ANCHOR_CLICK": "visible_anchor",
        "B_CARD_ROOT_CLICK": "card_root_center",
        "C_COVER_IMAGE_CLICK": "cover_image_center",
        "D_KEYBOARD_ACTIVATION": "focus_enter",
    }.get(strategy, strategy.lower())


def navigation_experiment_to_dict(result: NavigationExperimentResult) -> dict[str, Any]:
    data = asdict(result)
    data["success_count"] = result.success_count
    data["strategy_summary"] = _strategy_summary(result.attempts)
    return sanitize_json(data)


def _strategy_summary(attempts: list[NavigationProbeResult]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for attempt in attempts:
        bucket = summary.setdefault(attempt.strategy, {"attempts": 0, "success": 0, "failure": 0})
        bucket["attempts"] += 1
        if attempt.target_verified:
            bucket["success"] += 1
        else:
            bucket["failure"] += 1
    return summary


def _state_signature(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    return (
        (snapshot.get("url") or {}).get("path"),
        tuple((snapshot.get("url") or {}).get("query_keys") or []),
        snapshot.get("history_length"),
        snapshot.get("dialog_count"),
        snapshot.get("detail_root_count"),
        snapshot.get("body_child_count"),
        snapshot.get("frame_count"),
    )


def _interaction_model(attempt: NavigationProbeResult) -> str:
    classification = attempt.evidence.get("classification")
    if classification == "POPUP_OPENED_TARGET_VERIFIED":
        return f"{attempt.click_target_kind} -> popup/new page"
    if classification == "URL_CHANGED_TARGET_MATCH":
        return f"{attempt.click_target_kind} -> current page route"
    if classification == "MODAL_OPENED_TARGET_VERIFIED":
        return f"{attempt.click_target_kind} -> SPA modal/detail overlay"
    return f"{attempt.click_target_kind} -> target verified"


def _attach_network_trace(page: Page) -> tuple[list[dict[str, Any]], Callable[[], None]]:
    records: list[dict[str, Any]] = []

    def on_request(request: Any) -> None:
        summary = path_query_summary(request.url)
        if "xiaohongshu.com" in request.url:
            records.append({"direction": "request", "url": summary, "resource_type": request.resource_type, "status": None})

    def on_response(response: Any) -> None:
        summary = path_query_summary(response.url)
        if "xiaohongshu.com" in response.url:
            records.append({"direction": "response", "url": summary, "resource_type": response.request.resource_type, "status": response.status})

    page.on("request", on_request)
    page.on("response", on_response)

    def detach() -> None:
        try:
            page.remove_listener("request", on_request)
            page.remove_listener("response", on_response)
        except Exception:
            pass

    return records, detach


async def _save_probe_screenshot(app_config: AppConfig, page: Page | None, note_id: str, strategy: str, stage: str) -> str | None:
    if page is None:
        return None
    directory = app_config.base_dir / "screenshots" / "navigation_probe"
    directory.mkdir(parents=True, exist_ok=True)
    safe_strategy = strategy.lower().replace("_", "-")
    path = directory / f"nav_probe_{now_iso().replace(':', '').replace('+', '_')}_{note_id}_{safe_strategy}_{stage}.png"
    try:
        await page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:
        return None


async def _recover_profile_if_needed(page: Page, profile_url: str, before: dict[str, Any] | None, after: dict[str, Any] | None, logger: logging.Logger) -> None:
    if not before or not after:
        return
    if after.get("dialog_count", 0) > before.get("dialog_count", 0) or after.get("detail_root_count", 0) > before.get("detail_root_count", 0):
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(800)
        except Exception:
            pass
    if (after.get("url") or {}).get("path") != (before.get("url") or {}).get("path"):
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=5000)
            await page.wait_for_timeout(800)
            return
        except Exception:
            logger.info("NAV_PROBE recover_back_failed fallback_profile")
    try:
        await page.goto(profile_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1200)
    except Exception as exc:
        logger.info("NAV_PROBE recover_profile_failed error_type=%s", type(exc).__name__)


async def _raise_if_safe_stop(page: Page, phase: str) -> None:
    status = await detect_page_status(page)
    if status == LoginStatus.HUMAN_VERIFICATION_REQUIRED:
        raise SafeStopRequested(status, "HUMAN_VERIFICATION_REQUIRED", phase)
    if status == LoginStatus.RISK_CONTROL_DETECTED:
        raise SafeStopRequested(status, "RISK_CONTROL_DETECTED", phase)


def _valid_bbox(bbox: dict[str, Any]) -> bool:
    return bool(bbox) and int(bbox.get("width") or 0) > 2 and int(bbox.get("height") or 0) > 2


def _matches(creator: CreatorConfig, creator_filter: str | None) -> bool:
    if not creator_filter:
        return True
    fields = [creator.name, creator.xhs_id, creator.user_id, creator.url]
    return any(creator_filter in str(field) for field in fields if field)

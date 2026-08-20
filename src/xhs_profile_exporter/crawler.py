from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .browser import BrowserSession, detect_page_status
from .checkpoint import Checkpoint
from .config import AppConfig, CreatorConfig
from .db import Database
from .extractors import (
    discover_note_cards,
    extract_note_dom,
    extract_profile_dom,
    extract_user_id,
    merge_note_with_structured,
)
from .exporter import export_excel
from .qa import run_offline_qa
from .runtime import CollectionResult, OpenNoteResult, RunBudget, SafeStopRequested
from .state import LoginStatus, RunStatus
from .time_utils import now_iso
from .utils import is_sensitive_key, sanitize_json, sanitize_url


class Crawler:
    def __init__(self, app_config: AppConfig, db: Database, logger: logging.Logger):
        self.app_config = app_config
        self.db = db
        self.logger = logger
        self.structured_by_note: dict[str, dict[str, Any]] = {}
        self.structured_profile: dict[str, Any] | None = None
        self.current_run_id: str | None = None
        self.current_creator_id: str | None = None

    async def run(self, mode: str, creator_filter: str | None = None, max_notes: int | None = None, resume: bool = False) -> dict[str, Any]:
        results = {}
        creators = [c for c in self.app_config.creators if c.enabled and self._matches(c, creator_filter)]
        if not creators:
            raise ValueError("没有匹配且启用的 creator")
        for creator in creators:
            result = await self._run_creator(creator, mode, max_notes=max_notes, resume=resume)
            results[creator.name] = result
        return results

    async def _run_creator(self, creator: CreatorConfig, mode: str, max_notes: int | None = None, resume: bool = False) -> dict[str, Any]:
        user_id = creator.user_id or extract_user_id(creator.url)
        run_id = f"{now_iso().replace(':', '').replace('+', '_')}_{uuid.uuid4().hex[:8]}"
        self.current_run_id = run_id
        self.current_creator_id = user_id
        self.db.start_run(run_id, user_id, creator.name, self.app_config.version)
        checkpoint = Checkpoint(run_id=run_id, creator_id=user_id)
        resume_checkpoint = None
        resume_completed: set[str] = set()
        if resume:
            resume_checkpoint = Checkpoint.load_latest(self.app_config.base_dir / "data" / "checkpoints", user_id)
            if resume_checkpoint:
                resume_completed = set(resume_checkpoint.completed_note_ids)
                checkpoint.completed_note_ids = sorted(resume_completed)
                self.logger.info("RECOVERY_MODE checkpoint_run_id=%s completed=%s failed=%s", resume_checkpoint.run_id, len(resume_checkpoint.completed_note_ids), len(resume_checkpoint.failed_note_ids))
            else:
                self.logger.info("RECOVERY_MODE no_checkpoint creator_id=%s", user_id)
        checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
        self.logger.info("RUN_ID=%s CREATOR=%s USER_ID=%s MODE=%s", run_id, creator.name, user_id, mode)
        budget = self._build_budget()

        if mode == "login-only":
            return await self._login_only(creator, user_id, run_id)

        try:
            async with BrowserSession(self.app_config.base_dir, self.app_config.raw, self.logger) as browser:
                browser.response_callback = lambda url, data: self._capture_structured(run_id, url, data)
                page = await browser.new_page()
                budget.count_page_visit("login_check")
                login_status = await browser.check_login(page, creator.url)
                if login_status in {LoginStatus.LOGIN_EXPIRED, LoginStatus.HUMAN_VERIFICATION_REQUIRED}:
                    login_status = await browser.wait_for_login(page, creator.url)
                if login_status != LoginStatus.LOGIN_OK:
                    checkpoint.safe_stop_reason = login_status.value
                    checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
                    self.db.finish_run(
                        run_id,
                        RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value,
                        browser_version=browser.browser_version,
                        login_status=login_status.value,
                        safe_stop_reason=login_status.value,
                        risk_detected=1 if login_status == LoginStatus.RISK_CONTROL_DETECTED else 0,
                    )
                    return {"run_id": run_id, "status": RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value, "login_status": login_status.value}

                profile = await extract_profile_dom(page, creator.url, creator.name)
                if self.structured_profile:
                    profile["raw_json"] = {**(profile.get("raw_json") or {}), "structured": self.structured_profile}
                    profile["source"] = "dom+page_response"
                self.db.save_profile_snapshot(user_id, profile)
                self._write_raw("profile", user_id, run_id, profile)
                self.logger.info("PROFILE captured nickname=%s followers=%s raw=%s", profile.get("nickname"), profile.get("followers_value"), profile.get("followers_raw"))

                configured_limit = self.app_config.raw.get("safety", {}).get("max_notes_per_run")
                limit = max_notes if max_notes is not None else configured_limit
                target_exportable = None
                if mode == "smoke":
                    collection_cfg = self.app_config.raw.get("collection", {})
                    target_exportable = int(collection_cfg.get("smoke_note_limit", 3))
                    limit = int(collection_cfg.get("smoke_max_attempts", max(target_exportable, 12)))
                note_cards = await self._discover_notes(page, checkpoint, budget, target_unique=int(limit) if limit else None)
                self.logger.info("DISCOVERY completed total_unique=%s first_ids=%s", len(note_cards), [item["note_id"] for item in note_cards[:5]])
                if limit:
                    note_cards = select_smoke_candidates(note_cards, int(limit)) if mode == "smoke" else note_cards[: int(limit)]
                if resume_checkpoint:
                    before_resume = len(note_cards)
                    note_cards = [card for card in note_cards if card["note_id"] not in resume_completed]
                    self.logger.info("RECOVERY_MODE mapped_current_cards=%s skipped_completed=%s remaining=%s", before_resume, before_resume - len(note_cards), len(note_cards))

                collection = await self._collect_notes(page, creator, note_cards, checkpoint, budget, target_exportable=target_exportable, initial_completed=resume_completed)
                offline = run_offline_qa(self.db, user_id, self.logger)
                excel_path = export_excel(self.db, self.app_config.base_dir, user_id, creator.name, self.logger)
                database_exportable = len(self.db.current_notes(user_id))
                status = determine_run_status(mode, collection, target_exportable)
                if status == RunStatus.SUCCESS.value:
                    checkpoint.mark_complete()
                    checkpoint.completed_note_ids = sorted(set(checkpoint.completed_note_ids) | set(collection.exportable_ids))
                    checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
                self.db.finish_run(
                    run_id,
                    status,
                    browser_version=browser.browser_version,
                    login_status=LoginStatus.LOGIN_OK.value,
                    notes_discovered=len(note_cards),
                    notes_completed=len(collection.exportable_ids),
                    notes_failed=len(collection.failed_ids),
                    notes={
                        "attempted": collection.attempted_ids,
                        "exportable": collection.exportable_ids,
                        "non_exportable": collection.non_exportable_ids,
                        "navigation_failed": collection.navigation_failed_ids,
                        "non_public": collection.non_public_ids,
                        "failed": collection.failed_ids,
                    },
                )
                self.logger.info(
                    "RUN finished status=%s discovered=%s attempted=%s current_run_exportable=%s navigation_failed=%s non_exportable=%s failed=%s database_total_exportable=%s page_visits=%s excel=%s",
                    status,
                    len(note_cards),
                    collection.attempted_count,
                    len(collection.exportable_ids),
                    len(collection.navigation_failed_ids),
                    len(collection.non_exportable_ids),
                    len(collection.failed_ids),
                    database_exportable,
                    budget.page_visits,
                    excel_path,
                )
                return {
                    "run_id": run_id,
                    "status": status,
                    "login_status": LoginStatus.LOGIN_OK.value,
                    "notes_discovered": len(note_cards),
                    "notes_attempted": collection.attempted_count,
                    "notes_completed": len(collection.exportable_ids),
                    "notes_failed": len(collection.failed_ids),
                    "notes_exportable": len(collection.exportable_ids),
                    "navigation_failed": len(collection.navigation_failed_ids),
                    "non_exportable": len(collection.non_exportable_ids),
                    "database_total_exportable": database_exportable,
                    "excel": str(excel_path),
                    "offline_qa": offline,
                }
        except SafeStopRequested as stop:
            checkpoint.mark_safe_stop(stop.reason)
            checkpoint.safe_stop_reason = stop.reason
            checkpoint.current_note_id = stop.note_id
            checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
            self.db.finish_run(
                run_id,
                RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value,
                login_status=stop.status.value,
                safe_stop_reason=stop.reason,
                risk_detected=1 if stop.status == LoginStatus.RISK_CONTROL_DETECTED else 0,
            )
            self.logger.info("SAFE_STOP run_id=%s creator_id=%s phase=%s note_id=%s status=%s reason=%s", run_id, user_id, stop.phase, stop.note_id, stop.status.value, stop.reason)
            return {
                "run_id": run_id,
                "status": RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value,
                "login_status": stop.status.value,
                "safe_stop_reason": stop.reason,
            }
        except KeyboardInterrupt:
            checkpoint.mark_interrupted()
            checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
            self.db.finish_run(run_id, RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value, safe_stop_reason="USER_INTERRUPTED")
            raise
        except Exception as exc:
            self.logger.exception("RUN failed run_id=%s error_type=%s", run_id, type(exc).__name__)
            self.db.finish_run(run_id, RunStatus.FAILED.value, safe_stop_reason=type(exc).__name__)
            raise

    async def _login_only(self, creator: CreatorConfig, user_id: str, run_id: str) -> dict[str, Any]:
        async with BrowserSession(self.app_config.base_dir, self.app_config.raw, self.logger) as browser:
            page = await browser.new_page()
            login_status = await browser.check_login(page, creator.url)
            if login_status in {LoginStatus.LOGIN_EXPIRED, LoginStatus.HUMAN_VERIFICATION_REQUIRED}:
                login_status = await browser.wait_for_login(page, creator.url)
            self.db.finish_run(run_id, RunStatus.SUCCESS.value if login_status == LoginStatus.LOGIN_OK else RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value, browser_version=browser.browser_version, login_status=login_status.value)
            return {"run_id": run_id, "status": login_status.value, "creator_id": user_id}

    async def _discover_notes(self, page: Page, checkpoint: Checkpoint, budget: RunBudget, target_unique: int | None = None) -> list[dict[str, Any]]:
        safety = self.app_config.raw.get("safety", {})
        idle_limit = int(safety.get("scroll_idle_rounds", 5))
        max_rounds = int(safety.get("scroll_max_rounds", 300))
        seen: dict[str, dict[str, Any]] = {}
        idle_rounds = 0
        for round_no in range(1, max_rounds + 1):
            budget.check("discovery")
            await self._raise_if_safe_stop(page, "discovery")
            cards = await discover_note_cards(page)
            before = len(seen)
            for card in cards:
                seen.setdefault(card["note_id"], card)
            height = await page.evaluate("() => document.body ? document.body.scrollHeight : 0")
            added = len(seen) - before
            self.logger.info("DISCOVERY round=%s known=%s added=%s scroll_height=%s", round_no, len(seen), added, height)
            checkpoint.discovered_note_ids = list(seen)
            checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
            if target_unique is not None and len(seen) >= target_unique:
                self.logger.info("DISCOVERY termination_reason=target_unique_limit target=%s known=%s", target_unique, len(seen))
                break
            if added == 0:
                idle_rounds += 1
            else:
                idle_rounds = 0
            if idle_rounds >= idle_limit:
                self.logger.info("DISCOVERY termination_reason=idle_rounds_without_new_notes rounds=%s", idle_rounds)
                break
            await self._raise_if_safe_stop(page, "discovery_pre_scroll")
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await self._polite_delay()
            await self._raise_if_safe_stop(page, "discovery_post_scroll")
        return list(seen.values())

    async def _collect_notes(
        self,
        page: Page,
        creator: CreatorConfig,
        note_cards: list[dict[str, Any]],
        checkpoint: Checkpoint,
        budget: RunBudget,
        target_exportable: int | None = None,
        initial_completed: set[str] | None = None,
    ) -> CollectionResult:
        result = CollectionResult()
        errors = 0
        completed = set(initial_completed or set())
        max_errors = int(self.app_config.raw.get("safety", {}).get("max_consecutive_errors", 3))
        top_n = int(self.app_config.raw.get("collection", {}).get("collect_top_comments", 3))
        for index, card in enumerate(note_cards, start=1):
            note_id = card["note_id"]
            budget.check("collect_note", note_id)
            checkpoint.current_note_id = note_id
            checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
            result.attempted_ids.append(note_id)
            try:
                self.logger.info("[%s/%s] NOTE attempt note_id=%s phase=open candidate_rank=%s pinned=%s", index, len(note_cards), note_id, index, card.get("is_pinned"))
                open_result = await self._open_note_from_profile(page, creator.url, card, budget)
                page = open_result.page
                await self._raise_if_safe_stop(page, "note_after_open", note_id)
                if not open_result.target_verified:
                    errors += 1
                    result.navigation_failed_ids.append(note_id)
                    await self._save_error_screenshot(page, note_id, open_result.reason or "target_not_verified")
                    self.logger.info(
                        "NOTE attempt_result note_id=%s result=TARGET_NOT_VERIFIED strategy=%s detail_kind=%s reason=%s",
                        note_id,
                        open_result.strategy,
                        open_result.detail_kind,
                        open_result.reason,
                    )
                    checkpoint.failed_note_ids = sorted(set(checkpoint.failed_note_ids) | {note_id})
                    checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
                    if errors >= max_errors:
                        checkpoint.mark_safe_stop("MAX_CONSECUTIVE_ERRORS")
                        checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
                        self.logger.info("SAFE_STOP reason=MAX_CONSECUTIVE_ERRORS count=%s", errors)
                        break
                    continue
                await browser_flush_if_available(page)
                initial_state_record = await self._extract_initial_state_note_record(page, note_id)
                if initial_state_record:
                    self.structured_by_note[note_id] = {**initial_state_record, **self.structured_by_note.get(note_id, {})}
                note = await extract_note_dom(page, note_id, top_n)
                if note.get("status") != "OK":
                    self.logger.info("NOTE non_ok note_id=%s status=%s reason=%s", note_id, note.get("status"), note.get("status_note"))
                note.update({k: card.get(k) for k in ("is_pinned",) if card.get(k) is not None})
                note = merge_note_with_structured(note, self.structured_by_note.get(note_id))
                self.db.upsert_note(creator.user_id or extract_user_id(creator.url), note)
                self._write_raw("note", note_id, checkpoint.run_id, note)
                if note.get("status") in {"OK", "PARSE_PARTIAL"}:
                    result.exportable_ids.append(note_id)
                else:
                    result.non_exportable_ids.append(note_id)
                    result.non_public_ids.append(note_id)
                completed.add(note_id)
                checkpoint.completed_note_ids = sorted(completed)
                checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
                errors = 0
                self.logger.info("NOTE attempt_result note_id=%s result=PARSED title=%s likes=%s exact=%s comments=%s top_level_comments=%s status=%s target_verified=true", note_id, note.get("title"), note.get("likes_value"), note.get("likes_is_exact"), note.get("comments_value"), len(note.get("top_comments", [])), note.get("status"))
                return_result = await self._return_to_creator_profile(page, creator.url, note_id)
                self.logger.info(
                    "NOTE return_result note_id=%s strategy=%s profile_restored=%s route_after=%s",
                    note_id,
                    return_result["strategy"],
                    return_result["profile_restored"],
                    return_result["route_after"],
                )
            except SafeStopRequested as stop:
                result.safe_stop_status = stop.status
                result.safe_stop_reason = stop.reason
                raise
            except PlaywrightTimeoutError as exc:
                errors += 1
                result.failed_ids.append(note_id)
                await self._save_error_screenshot(page, note_id, "timeout")
                self.logger.warning("NOTE failed note_id=%s error_type=%s", note_id, type(exc).__name__)
            except Exception as exc:
                errors += 1
                result.failed_ids.append(note_id)
                await self._save_error_screenshot(page, note_id, type(exc).__name__)
                self.logger.exception("NOTE failed note_id=%s error_type=%s", note_id, type(exc).__name__)
            checkpoint.failed_note_ids = sorted(set(checkpoint.failed_note_ids) | set(result.failed_ids))
            checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
            if errors >= max_errors:
                checkpoint.mark_safe_stop("MAX_CONSECUTIVE_ERRORS")
                checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
                self.logger.info("SAFE_STOP reason=MAX_CONSECUTIVE_ERRORS count=%s", errors)
                break
            if target_exportable is not None and len(result.exportable_ids) >= target_exportable:
                self.logger.info("SMOKE target_exportable_reached target=%s attempted=%s exportable=%s", target_exportable, result.attempted_count, len(result.exportable_ids))
                break
            await self._polite_delay()
        return result

    async def _open_note_from_profile(self, page: Page, profile_url: str, card: dict[str, Any], budget: RunBudget) -> OpenNoteResult:
        note_id = card["note_id"]
        creator_id = extract_user_id(profile_url)
        current_result = await self._click_visible_note_cover_on_current_page(page, creator_id, note_id, budget, "current_mounted_cover_click")
        if current_result.target_verified:
            return current_result

        budget.count_page_visit("note_profile_navigation", note_id)
        await page.goto(profile_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        await self._raise_if_safe_stop(page, "note_profile_loaded", note_id)
        try:
            scan_result = await self._scan_profile_for_visible_note_cover(page, creator_id, note_id, budget)
            locator = scan_result.get("locator")
            if locator:
                budget.count_page_visit("note_cover_click", note_id)
                self.logger.info(
                    "NOTE open_strategy=profile_visible_cover_click note_id=%s scan_round=%s cover_candidates=%s href_path_pattern=%s bbox=%s",
                    note_id,
                    scan_result.get("round"),
                    scan_result.get("cover_candidates"),
                    scan_result.get("href_path_pattern"),
                    scan_result.get("bbox"),
                )
                try:
                    await locator.scroll_into_view_if_needed(timeout=5000)
                    await locator.click(timeout=10000, no_wait_after=True)
                    click_strategy = "COVER_LOCATOR_CLICK"
                except Exception as exc:
                    self.logger.info("NOTE cover_locator_click_failed note_id=%s error_type=%s fallback=center_mouse", note_id, type(exc).__name__)
                    box = await locator.bounding_box(timeout=5000)
                    if not box:
                        return OpenNoteResult(page=page, note_id=note_id, strategy="profile_visible_cover_click", target_verified=False, reason="COVER_BOUNDING_BOX_MISSING")
                    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    click_strategy = "COVER_CENTER_MOUSE_CLICK_FALLBACK"
                verified, reason, detail_count = await self._wait_for_target_note_detail(page, note_id)
                self.logger.info(
                    "NOTE click_result note_id=%s strategy=%s route_after=%s detail_root_count=%s target_verified=%s reason=%s",
                    note_id,
                    click_strategy,
                    safe_route_summary(page.url),
                    detail_count,
                    verified,
                    reason,
                )
                return OpenNoteResult(
                    page=page,
                    note_id=note_id,
                    strategy=click_strategy,
                    target_verified=verified,
                    detail_kind="route_and_detail_root" if verified else None,
                    reason=None if verified else reason,
                )
            self.logger.info(
                "NOTE open_strategy=profile_visible_cover_click note_id=%s result=VISIBLE_COVER_NOT_FOUND rounds=%s",
                note_id,
                scan_result.get("rounds"),
            )
        except SafeStopRequested:
            raise
        except Exception as exc:
            self.logger.info("NOTE open_strategy=profile_visible_cover_click_failed note_id=%s reason=%s", note_id, type(exc).__name__)

        return OpenNoteResult(page=page, note_id=note_id, strategy="visible_cover_only", target_verified=False, reason="TARGET_NOT_VERIFIED")

    async def _click_visible_note_cover_on_current_page(self, page: Page, creator_id: str, note_id: str, budget: RunBudget, strategy: str) -> OpenNoteResult:
        find_result = await self._find_visible_note_cover(page, creator_id, note_id)
        locator = find_result.get("locator")
        self.logger.info(
            "NOTE current_page_cover_find note_id=%s strategy=%s cover_candidates=%s visible_cover_found=%s href_path_pattern=%s",
            note_id,
            strategy,
            find_result.get("cover_candidates"),
            bool(locator),
            find_result.get("href_path_pattern"),
        )
        if not locator:
            return OpenNoteResult(page=page, note_id=note_id, strategy=strategy, target_verified=False, reason="VISIBLE_COVER_NOT_FOUND")
        budget.count_page_visit(strategy, note_id)
        try:
            await locator.scroll_into_view_if_needed(timeout=5000)
            await locator.click(timeout=10000, no_wait_after=True)
            verified, reason, detail_count = await self._wait_for_target_note_detail(page, note_id)
            self.logger.info(
                "NOTE click_result note_id=%s strategy=%s route_after=%s detail_root_count=%s target_verified=%s reason=%s",
                note_id,
                strategy,
                safe_route_summary(page.url),
                detail_count,
                verified,
                reason,
            )
            return OpenNoteResult(
                page=page,
                note_id=note_id,
                strategy=strategy,
                target_verified=verified,
                detail_kind="route_and_detail_root" if verified else None,
                reason=None if verified else reason,
            )
        except SafeStopRequested:
            raise
        except Exception as exc:
            self.logger.info("NOTE current_page_cover_click_failed note_id=%s strategy=%s reason=%s", note_id, strategy, type(exc).__name__)
            return OpenNoteResult(page=page, note_id=note_id, strategy=strategy, target_verified=False, reason="CLICK_ACTION_FAILED")

    async def _scan_profile_for_visible_note_cover(self, page: Page, creator_id: str, note_id: str, budget: RunBudget) -> dict[str, Any]:
        collection_cfg = self.app_config.raw.get("collection", {})
        max_rounds = int(collection_cfg.get("detail_scan_max_rounds", 12))
        last_result: dict[str, Any] = {"locator": None, "cover_candidates": 0, "rounds": max_rounds}
        for round_no in range(1, max_rounds + 1):
            budget.check("profile_cover_scan", note_id)
            await self._raise_if_safe_stop(page, "profile_cover_scan", note_id)
            result = await self._find_visible_note_cover(page, creator_id, note_id)
            result["round"] = round_no
            result["rounds"] = max_rounds
            last_result = result
            self.logger.info(
                "NOTE profile_cover_scan note_id=%s round=%s cover_candidates=%s visible_cover_found=%s href_path_pattern=%s bbox=%s",
                note_id,
                round_no,
                result.get("cover_candidates"),
                bool(result.get("locator")),
                result.get("href_path_pattern"),
                result.get("bbox"),
            )
            if result.get("locator"):
                return result
            await page.evaluate("() => window.scrollBy(0, Math.max(600, window.innerHeight * 0.85))")
            await page.wait_for_timeout(1000)
        return last_result

    async def _find_visible_note_cover(self, page: Page, creator_id: str, note_id: str) -> dict[str, Any]:
        locator = page.locator(f'a[href*="{note_id}"]')
        count = await locator.count()
        candidates: list[tuple[int, dict[str, Any]]] = []
        for index in range(count):
            item = locator.nth(index)
            try:
                summary = await item.evaluate(
                    """
                    (el, args) => {
                      const [creatorId, noteId] = args;
                      const rect = el.getBoundingClientRect();
                      const style = window.getComputedStyle(el);
                      const href = el.href || el.getAttribute("href") || "";
                      let path = "";
                      let hasQuery = false;
                      try {
                        const url = new URL(href, location.href);
                        path = url.pathname;
                        hasQuery = url.search.length > 0;
                      } catch {
                        path = href.split("?")[0] || "";
                        hasQuery = href.includes("?");
                      }
                      const root = el.closest('[class*="note"], [class*="card"], article, section, li');
                      const rootRect = root ? root.getBoundingClientRect() : null;
                      const visible = rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none" && rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth;
                      const classes = String(el.className || "").replace(/\\s+/g, " ").trim();
                      const rootClasses = root ? String(root.className || "").replace(/\\s+/g, " ").trim() : "";
                      const isCreatorNotePath = path.includes(`/user/profile/${creatorId}/${noteId}`);
                      const isDirectNotePath = path.includes(`/explore/${noteId}`) || path.includes(`/discovery/item/${noteId}`);
                      return {
                        visible,
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        class: classes.slice(0, 80),
                        root_class: rootClasses.slice(0, 80),
                        href_path_pattern: isCreatorNotePath ? "/user/profile/{creator_id}/{note_id}" : (isDirectNotePath ? "/explore_or_discovery/{note_id}" : "other_note_href"),
                        has_query: hasQuery,
                        is_cover_class: /(^|\\s)cover(\\s|$)/i.test(classes) || /cover|image|mask|thumbnail/i.test(classes),
                        is_card_ancestor: Boolean(root),
                        root_width: rootRect ? Math.round(rootRect.width) : 0,
                        root_height: rootRect ? Math.round(rootRect.height) : 0,
                      };
                    }
                    """,
                    [creator_id, note_id],
                )
            except Exception:
                continue
            if is_visible_note_cover_summary(summary):
                candidates.append((index, summary))
        selected = select_visible_note_cover_candidate([summary for _, summary in candidates])
        if not selected:
            return {"locator": None, "cover_candidates": len(candidates), "href_path_pattern": None, "bbox": None}
        selected_index = next(index for index, summary in candidates if summary is selected)
        return {
            "locator": locator.nth(selected_index),
            "cover_candidates": len(candidates),
            "href_path_pattern": selected.get("href_path_pattern"),
            "bbox": {"width": selected.get("width"), "height": selected.get("height"), "x": selected.get("x"), "y": selected.get("y")},
            "has_query": selected.get("has_query"),
        }

    async def _wait_for_target_note_detail(self, page: Page, note_id: str, timeout_ms: int = 10000) -> tuple[bool, str | None, int]:
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        last_reason = "DETAIL_NOT_READY"
        last_detail_count = 0
        while asyncio.get_event_loop().time() < deadline:
            await self._raise_if_safe_stop(page, "note_wait_target_detail", note_id)
            route_match = route_matches_note(page.url, note_id)
            wrong_route = route_is_note_detail(page.url) and not route_match
            evidence = await self._get_note_detail_evidence(page, note_id)
            detail_count = int(evidence.get("detail_root_count") or 0)
            last_detail_count = detail_count
            if route_match and evidence.get("verified"):
                return True, None, detail_count
            if wrong_route:
                return False, "TARGET_MISMATCH", detail_count
            if route_match:
                last_reason = "DETAIL_NOT_READY"
            elif detail_count > 0:
                last_reason = "TARGET_NOT_VERIFIED"
            else:
                last_reason = "CLICK_NO_STATE_CHANGE"
            await page.wait_for_timeout(500)
        return False, last_reason, last_detail_count

    async def _verify_target_note(self, page: Page, note_id: str) -> bool:
        verified, _, _ = await self._wait_for_target_note_detail(page, note_id, timeout_ms=100)
        return verified

    async def _visible_detail_root_count(self, page: Page) -> int:
        evidence = await self._get_note_detail_evidence(page, "")
        return int(evidence.get("detail_root_count") or 0)

    async def _get_note_detail_evidence(self, page: Page, note_id: str) -> dict[str, Any]:
        try:
            return await page.evaluate(
                """
            (noteId) => {
              const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
              };
              const unavailableWords = ["当前笔记暂时无法浏览", "暂时无法浏览", "笔记不存在", "内容不存在", "已被删除"];
              const bodyText = document.body ? document.body.innerText || "" : "";
              const unavailable = unavailableWords.some((word) => bodyText.includes(word));
              const title = document.querySelector("#detail-title");
              const desc = document.querySelector("#detail-desc");
              const engage = document.querySelector(".engage-bar, [class*=engage], [class*=interaction], [class*=Interact]");
              const noteRoots = Array.from(document.querySelectorAll('[class*="note-detail"], [class*="noteDetail"], [class*="NoteDetail"], [data-testid*="note-detail"], [role="dialog"]')).filter(visible);
              const exactLinks = noteId ? Array.from(document.querySelectorAll(`a[href*="${noteId}"]`)).filter(visible) : [];
              const hasInitialStateNote = (root) => {
                if (!noteId || !root || typeof root !== "object") return false;
                const seen = new Set();
                const queue = [root];
                let scanned = 0;
                while (queue.length && scanned < 5000) {
                  const value = queue.shift();
                  scanned += 1;
                  if (!value || typeof value !== "object" || seen.has(value)) continue;
                  seen.add(value);
                  if ((value.note_id === noteId || value.id === noteId) && (value.title || value.display_title || value.desc || value.content)) return true;
                  let children = [];
                  try { children = Object.values(value); } catch { children = []; }
                  for (const child of children) {
                    if (child && typeof child === "object" && !seen.has(child)) queue.push(child);
                  }
                }
                return false;
              };
              const strongRoot = noteRoots.some((el) => {
                const text = el.innerText || "";
                return !unavailable && (!noteId || text.includes(noteId) || Boolean(el.querySelector(`a[href*="${noteId}"]`)) || visible(title) || visible(desc));
              });
              const initialStateMatch = hasInitialStateNote(window.__INITIAL_STATE__);
              const independentSignals = [visible(title), visible(desc), visible(engage), exactLinks.length > 0, initialStateMatch].filter(Boolean).length;
              return {
                verified: Boolean(!unavailable && (strongRoot || independentSignals >= 2)),
                unavailable,
                detail_root_count: noteRoots.length,
                title_visible: visible(title),
                desc_visible: visible(desc),
                engage_visible: visible(engage),
                exact_link_count: exactLinks.length,
                initial_state_match: initialStateMatch,
                independent_signals: independentSignals,
              };
            }
            """,
                note_id,
            )
        except Exception as exc:
            return {"verified": False, "detail_root_count": 0, "evidence_error": type(exc).__name__}

    async def _extract_initial_state_note_record(self, page: Page, note_id: str) -> dict[str, Any] | None:
        try:
            data = await page.evaluate(
                """
                (noteId) => {
                  const allow = new Set(["id", "note_id", "title", "display_title", "desc", "content", "type", "note_type", "model_type", "time", "publish_time", "liked_count", "collected_count", "comment_count", "share_count"]);
                  const walk = (root) => {
                    if (!root || typeof root !== "object") return null;
                    const seen = new Set();
                    const queue = [root];
                    let scanned = 0;
                    while (queue.length && scanned < 5000) {
                      const value = queue.shift();
                      scanned += 1;
                      if (!value || typeof value !== "object" || seen.has(value)) continue;
                      seen.add(value);
                      if (value.id === noteId || value.note_id === noteId) {
                        const out = {};
                        for (const key of Object.keys(value)) {
                          if (allow.has(key) && (typeof value[key] !== "object" || value[key] === null)) out[key] = value[key];
                        }
                        return out;
                      }
                      let children = [];
                      try { children = Object.values(value); } catch { children = []; }
                      for (const item of children) {
                        if (item && typeof item === "object" && !seen.has(item)) queue.push(item);
                      }
                    }
                    return null;
                  };
                  return walk(window.__INITIAL_STATE__);
                }
                """,
                note_id,
            )
        except Exception:
            return None
        return sanitize_json(data) if data else None

    async def _return_to_creator_profile(self, page: Page, profile_url: str, note_id: str) -> dict[str, Any]:
        creator_id = extract_user_id(profile_url)
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=10000)
            await page.wait_for_timeout(1500)
            await self._raise_if_safe_stop(page, "profile_return_after_back", note_id)
            if await self._profile_restored(page, creator_id):
                return {"strategy": "PROFILE_RETURN_HISTORY_SUCCESS", "profile_restored": True, "route_after": safe_route_summary(page.url)}
            self.logger.info("NOTE profile_return_history_not_ready note_id=%s route_after=%s", note_id, safe_route_summary(page.url))
        except SafeStopRequested:
            raise
        except Exception as exc:
            self.logger.info("NOTE profile_return_history_failed note_id=%s error_type=%s", note_id, type(exc).__name__)
        try:
            await page.goto(profile_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            await self._raise_if_safe_stop(page, "profile_return_after_goto", note_id)
            restored = await self._profile_restored(page, creator_id)
            return {"strategy": "PROFILE_RETURN_GOTO_FALLBACK", "profile_restored": restored, "route_after": safe_route_summary(page.url)}
        except SafeStopRequested:
            raise
        except Exception as exc:
            self.logger.info("NOTE profile_return_failed note_id=%s error_type=%s", note_id, type(exc).__name__)
            return {"strategy": "PROFILE_RETURN_FAILED", "profile_restored": False, "route_after": safe_route_summary(page.url)}

    async def _profile_restored(self, page: Page, creator_id: str) -> bool:
        if f"/user/profile/{creator_id}" not in urlsplit(page.url).path:
            return False
        try:
            count = await page.locator('a[href*="/explore/"], a[href*="/discovery/item/"], section[class*="note"], [data-note-id]').count()
            return count > 0
        except Exception:
            return False

    async def _raise_if_safe_stop(self, page: Page, phase: str, note_id: str | None = None) -> None:
        status = await detect_page_status(page)
        if status == LoginStatus.HUMAN_VERIFICATION_REQUIRED:
            raise SafeStopRequested(status, "HUMAN_VERIFICATION_REQUIRED", phase, note_id)
        if status == LoginStatus.RISK_CONTROL_DETECTED:
            raise SafeStopRequested(status, "RISK_CONTROL_DETECTED", phase, note_id)

    def _capture_structured(self, run_id: str, url: str, data: Any) -> None:
        records = extract_public_note_records(data)
        for note_id, record in records.items():
            self.structured_by_note[note_id] = record
        profile_before = self.structured_profile is not None
        profile_record = extract_public_profile_record(data, self.current_creator_id) if self.current_creator_id else None
        if profile_record:
            self.structured_profile = profile_record
        if records or (self.structured_profile is not None and not profile_before):
            self.logger.info("STRUCTURED_RESPONSE run_id=%s extracted_note_records=%s profile_associated=%s", run_id, len(records), bool(self.structured_profile))

    def _write_raw(self, entity_type: str, entity_id: str, run_id: str, data: Any) -> None:
        raw_dir = self.app_config.base_dir / "data" / "raw" / ("profile" if entity_type == "profile" else "notes")
        raw_dir.mkdir(parents=True, exist_ok=True)
        path = raw_dir / f"{entity_id}_{run_id}.json"
        with path.open("w", encoding="utf-8") as fh:
            json.dump(sanitize_json(data), fh, ensure_ascii=False, indent=2, default=str)
        self.db.save_raw(run_id, entity_type, entity_id, "normalized", sanitize_json(data))

    async def _save_error_screenshot(self, page: Page, note_id: str, error_type: str) -> None:
        path = self.app_config.base_dir / "screenshots" / "errors" / f"{now_iso().replace(':', '').replace('+', '_')}_{note_id}_{error_type}.png"
        try:
            await page.screenshot(path=str(path), full_page=False)
            self.logger.info("ERROR_SCREENSHOT path=%s", path)
        except Exception:
            self.logger.warning("ERROR_SCREENSHOT failed note_id=%s", note_id)

    async def _polite_delay(self) -> None:
        safety = self.app_config.raw.get("safety", {})
        minimum = float(safety.get("min_delay_seconds", 6))
        maximum = float(safety.get("max_delay_seconds", 14))
        delay = random.uniform(minimum, maximum)
        self.logger.info("DELAY seconds=%.1f", delay)
        await asyncio.sleep(delay)

    def _matches(self, creator: CreatorConfig, creator_filter: str | None) -> bool:
        if not creator_filter:
            return True
        fields = [creator.name, creator.xhs_id, creator.user_id, creator.url]
        return any(creator_filter in str(field) for field in fields if field)

    def _build_budget(self) -> RunBudget:
        safety = self.app_config.raw.get("safety", {})
        return RunBudget(
            max_page_visits=safety.get("max_page_visits_per_run"),
            max_runtime_minutes=safety.get("max_runtime_minutes"),
        )


async def browser_flush_if_available(page: Page) -> None:
    # Response data is auxiliary only. The crawler must not depend on it.
    context = getattr(page, "context", None)
    browser_session = getattr(context, "_xhs_browser_session", None)
    if browser_session:
        await browser_session.flush_response_tasks(timeout=5)


def select_smoke_candidates(cards: list[dict[str, Any]], max_attempts: int) -> list[dict[str, Any]]:
    if len(cards) <= max_attempts:
        return cards
    non_pinned = [card for card in cards if not card.get("is_pinned")]
    source = non_pinned or cards
    evenly_spaced = sorted({round(i * (len(source) - 1) / max(1, max_attempts - 1)) for i in range(max_attempts)})
    priority = [len(source) - 1, len(source) // 2, 0]
    indexes = priority + evenly_spaced
    selected: list[dict[str, Any]] = []
    seen = set()
    for idx in indexes:
        note_id = source[idx]["note_id"]
        if note_id not in seen:
            selected.append(source[idx])
            seen.add(note_id)
        if len(selected) >= max_attempts:
            return selected
    return selected


def determine_run_status(mode: str, collection: CollectionResult, target_exportable: int | None = None) -> str:
    if collection.safe_stop_reason or collection.safe_stop_status:
        return RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value
    if mode == "smoke":
        if target_exportable is not None and len(collection.exportable_ids) >= target_exportable and not collection.failed_ids and not collection.navigation_failed_ids:
            return RunStatus.SUCCESS.value
        return RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value
    if collection.failed_ids or collection.navigation_failed_ids:
        return RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value
    if collection.attempted_ids and (collection.exportable_ids or collection.non_public_ids):
        return RunStatus.SUCCESS.value
    return RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value


def is_visible_note_cover_summary(summary: dict[str, Any]) -> bool:
    return bool(
        summary.get("visible")
        and int(summary.get("width") or 0) >= 80
        and int(summary.get("height") or 0) >= 80
        and summary.get("is_card_ancestor")
        and summary.get("href_path_pattern") in {"/user/profile/{creator_id}/{note_id}", "/explore_or_discovery/{note_id}"}
    )


def select_visible_note_cover_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [item for item in candidates if is_visible_note_cover_summary(item)]
    if not eligible:
        return None
    return sorted(
        eligible,
        key=lambda item: (
            0 if item.get("is_cover_class") else 1,
            0 if item.get("href_path_pattern") == "/user/profile/{creator_id}/{note_id}" else 1,
            -(int(item.get("width") or 0) * int(item.get("height") or 0)),
        ),
    )[0]


def route_matches_note(url: str, note_id: str) -> bool:
    path = urlsplit(url).path
    return path in {f"/explore/{note_id}", f"/discovery/item/{note_id}"}


def route_is_note_detail(url: str) -> bool:
    path = urlsplit(url).path
    return path.startswith("/explore/") or path.startswith("/discovery/item/")


def safe_route_summary(url: str) -> str:
    parts = urlsplit(url)
    keys = sorted(
        {
            key
            for key, _ in parse_qsl(parts.query, keep_blank_values=True)
            if key and not is_sensitive_key(key)
        }
    )
    return parts.path + (f"?keys={','.join(keys)}" if keys else "")


NOTE_PUBLIC_ALLOWLIST = {
    "id",
    "note_id",
    "title",
    "display_title",
    "desc",
    "content",
    "type",
    "note_type",
    "model_type",
    "time",
    "publish_time",
    "liked_count",
    "collected_count",
    "comment_count",
    "share_count",
}

PROFILE_PUBLIC_FIELD_ALIASES = {
    "user_id": "user_id",
    "id": "user_id",
    "nickname": "nickname",
    "name": "nickname",
    "red_id": "xhs_id",
    "redId": "xhs_id",
    "xhs_id": "xhs_id",
    "desc": "bio",
    "bio": "bio",
    "avatar": "avatar_url",
    "avatar_url": "avatar_url",
    "image": "avatar_url",
    "fans": "followers",
    "followers": "followers",
    "follower_count": "followers",
    "follows": "following",
    "following": "following",
    "liked": "interactions",
    "liked_count": "interactions",
    "interaction_count": "interactions",
    "tags": "tags",
}


def extract_public_note_records(value: Any) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if isinstance(value, dict):
        note_id = value.get("note_id") or value.get("id")
        if isinstance(note_id, str) and len(note_id) == 24:
            records[note_id] = sanitize_json({key: value.get(key) for key in NOTE_PUBLIC_ALLOWLIST if key in value})
        for item in value.values():
            records.update(extract_public_note_records(item))
    elif isinstance(value, list):
        for item in value:
            records.update(extract_public_note_records(item))
    return records


def extract_public_profile_record(value: Any, creator_id: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        candidate_id = value.get("user_id") or value.get("id")
        if candidate_id == creator_id:
            record: dict[str, Any] = {"user_id": creator_id}
            for key, out_key in PROFILE_PUBLIC_FIELD_ALIASES.items():
                if key not in value or is_sensitive_key(key):
                    continue
                item = value.get(key)
                if isinstance(item, (dict, list)) and out_key != "tags":
                    continue
                if out_key == "avatar_url":
                    record[out_key] = sanitize_url(str(item)) if item else None
                else:
                    record[out_key] = sanitize_json(item)
            return {key: item for key, item in record.items() if item is not None}
        for item in value.values():
            found = extract_public_profile_record(item, creator_id)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = extract_public_profile_record(item, creator_id)
            if found:
                return found
    return None


def is_public_profile_payload(value: Any, creator_id: str) -> bool:
    return extract_public_profile_record(value, creator_id) is not None

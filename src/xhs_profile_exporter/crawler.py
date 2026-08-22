from __future__ import annotations

import asyncio
from collections import deque
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
from .debug_report import build_extraction_report, write_extraction_report
from .extractors import (
    discover_note_cards,
    extract_note_dom,
    extract_profile_dom,
    extract_user_id,
    merge_note_with_structured,
    merge_tags,
    normalize_tag_names,
    normalize_public_note_record,
    note_completeness_values,
)
from .exporter import export_excel
from .five_note_review import (
    build_actual_payload as build_five_note_actual_payload,
    capture_e2e_review_state,
    e2e_review_dir,
    e2e_review_enabled,
    validate_excel_readback,
    write_e2e_review_summary,
    write_excel_readback_artifact,
    write_note_review_artifact,
)
from .qa import run_offline_qa
from .runtime import CollectionResult, OpenNoteResult, RunBudget, SafeStopRequested
from .state import LoginStatus, RunStatus
from .time_utils import now_iso
from .utils import is_sensitive_key, parse_count, sanitize_json, sanitize_url
from .validation_artifacts import (
    build_detail_not_ready_validation_note,
    build_validation_note,
    write_live_validation_artifact,
)


MAX_STRUCTURED_NODES_PER_RESPONSE = 5000
NOTE_ID_LENGTH = 24
STRUCTURED_SOURCE_PRIORITY = {"PAGE_RESPONSE": 1, "DETAIL_INITIAL_STATE": 2}


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

    async def debug_extract(self, note_id: str, creator_filter: str | None = None) -> dict[str, Any]:
        results = {}
        creators = [c for c in self.app_config.creators if c.enabled and self._matches(c, creator_filter)]
        if not creators:
            raise ValueError("没有匹配且启用的 creator")
        for creator in creators:
            results[creator.name] = await self._debug_extract_creator(creator, note_id)
        return results

    async def _debug_extract_creator(self, creator: CreatorConfig, note_id: str) -> dict[str, Any]:
        user_id = creator.user_id or extract_user_id(creator.url)
        run_id = f"{now_iso().replace(':', '').replace('+', '_')}_{uuid.uuid4().hex[:8]}"
        self.current_run_id = run_id
        self.current_creator_id = user_id
        self.structured_by_note = {}
        self.structured_profile = None
        self.logger.info("DEBUG_EXTRACT start run_id=%s note_id=%s creator_id=%s", run_id, note_id, user_id)
        budget = self._build_budget()
        checkpoint = Checkpoint(run_id=run_id, creator_id=user_id)
        try:
            async with BrowserSession(self.app_config.base_dir, self.app_config.raw, self.logger) as browser:
                browser.response_callback = lambda url, data: self._capture_structured(run_id, url, data)
                page = await browser.new_page()
                budget.count_page_visit("debug_login_check", note_id)
                login_status = await browser.check_login(page, creator.url)
                if login_status in {LoginStatus.LOGIN_EXPIRED, LoginStatus.HUMAN_VERIFICATION_REQUIRED}:
                    login_status = await browser.wait_for_login(page, creator.url)
                if login_status != LoginStatus.LOGIN_OK:
                    return {"run_id": run_id, "status": login_status.value, "login_status": login_status.value, "note_id": note_id}

                note_cards = await self._discover_notes(page, checkpoint, budget, target_unique=60)
                card = next((item for item in note_cards if item.get("note_id") == note_id), None)
                if not card:
                    self.logger.info("DEBUG_EXTRACT note_not_found run_id=%s note_id=%s discovered=%s", run_id, note_id, len(note_cards))
                    return {"run_id": run_id, "status": "NOTE_NOT_FOUND", "login_status": LoginStatus.LOGIN_OK.value, "note_id": note_id, "notes_discovered": len(note_cards)}

                open_result = await self._open_note_from_profile(page, creator.url, card, budget)
                page = open_result.page
                if not open_result.target_verified:
                    return {
                        "run_id": run_id,
                        "status": open_result.reason or "TARGET_NOT_VERIFIED",
                        "login_status": LoginStatus.LOGIN_OK.value,
                        "note_id": note_id,
                        "detail_ready": bool(open_result.detail_ready),
                    }

                await browser_flush_if_available(page)
                initial_state_record = await self._extract_initial_state_note_record(page, note_id)
                if initial_state_record:
                    self.structured_by_note[note_id] = merge_public_note_records(
                        self.structured_by_note.get(note_id),
                        initial_state_record,
                        note_id,
                        prefer_incoming=True,
                        incoming_source="DETAIL_INITIAL_STATE",
                    )
                note = await extract_note_dom(page, note_id, 0)
                note.update({k: card.get(k) for k in ("is_pinned",) if card.get(k) is not None})
                note = merge_note_with_structured(note, self.structured_by_note.get(note_id))
                dom_summary = await self._debug_dom_summary(page)
                report = build_extraction_report(run_id=run_id, note_id=note_id, detail_ready=True, note=note, dom_summary=dom_summary)
                report_path = write_extraction_report(self.app_config.base_dir, report)
                self.logger.info("DEBUG_EXTRACT report_exported path=%s", report_path)
                return {
                    "run_id": run_id,
                    "status": "OK",
                    "login_status": LoginStatus.LOGIN_OK.value,
                    "note_id": note_id,
                    "detail_ready": True,
                    "artifact": str(report_path),
                }
        except SafeStopRequested as stop:
            self.logger.info("SAFE_STOP debug_extract phase=%s note_id=%s status=%s reason=%s", stop.phase, stop.note_id, stop.status.value, stop.reason)
            return {"run_id": run_id, "status": stop.reason, "login_status": stop.status.value, "note_id": note_id, "safe_stop_reason": stop.reason}
        finally:
            self._clear_run_context(run_id, user_id)

    async def _run_creator(self, creator: CreatorConfig, mode: str, max_notes: int | None = None, resume: bool = False) -> dict[str, Any]:
        user_id = creator.user_id or extract_user_id(creator.url)
        run_id = f"{now_iso().replace(':', '').replace('+', '_')}_{uuid.uuid4().hex[:8]}"
        self.current_run_id = run_id
        self.current_creator_id = user_id
        self.structured_by_note = {}
        self.structured_profile = None
        self.logger.info("STRUCTURED_STATE reset run_id=%s creator_id=%s", run_id, user_id)
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
            try:
                return await self._login_only(creator, user_id, run_id)
            finally:
                self._clear_run_context(run_id, user_id)

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
                profile_state_record = await self._extract_initial_state_profile_record(page, user_id)
                if profile_state_record:
                    self.structured_profile = merge_public_profile_records(
                        self.structured_profile,
                        profile_state_record,
                        user_id,
                        prefer_incoming=True,
                    )
                if self.structured_profile:
                    profile = merge_profile_with_structured(profile, self.structured_profile)
                    profile["raw_json"] = {**(profile.get("raw_json") or {}), "structured": self.structured_profile}
                    profile["source"] = "dom+page_response"
                profile_completeness = summarize_profile_fields(profile, self.structured_profile)
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

                review_dir = e2e_review_dir(self.app_config.base_dir, run_id) if e2e_review_enabled(mode, int(limit or 0)) else None
                collection = await self._collect_notes(page, creator, note_cards, checkpoint, budget, target_exportable=target_exportable, initial_completed=resume_completed, review_dir=review_dir)
                offline = run_offline_qa(self.db, user_id, self.logger)
                excel_path = export_excel(self.db, self.app_config.base_dir, user_id, creator.name, self.logger)
                excel_readback = None
                if review_dir:
                    expected_ids = list(collection.exportable_ids)
                    expected_rows = [row for row in self.db.current_notes(user_id) if row["note_id"] in set(expected_ids)]
                    excel_readback = validate_excel_readback(excel_path, expected_rows, expected_ids)
                    write_excel_readback_artifact(review_dir, excel_readback, excel_path)
                    write_e2e_review_summary(review_dir, run_id=run_id, collection=collection, excel_readback=excel_readback)
                    self.logger.info(
                        "E2E_REVIEW excel_readback rows_expected=%s rows_found=%s duplicates=%s missing=%s diffs=%s path=%s",
                        excel_readback["rows_expected"],
                        excel_readback["rows_found"],
                        len(excel_readback["duplicate_note_ids"]),
                        len(excel_readback["missing_note_ids"]),
                        len(excel_readback["field_diffs"]),
                        review_dir / "excel_readback.json",
                    )
                database_exportable = len(self.db.current_notes(user_id))
                status = determine_run_status(mode, collection, target_exportable)
                validation_dir = write_live_validation_artifact(
                    self.app_config.base_dir,
                    run_id=run_id,
                    status=status,
                    login_status=LoginStatus.LOGIN_OK.value,
                    notes_discovered=len(note_cards),
                    collection=collection,
                )
                self.logger.info("VALIDATION_ARTIFACT exported path=%s", validation_dir)
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
                    risk_detected=1 if collection.safe_stop_status == LoginStatus.RISK_CONTROL_DETECTED else 0,
                    notes={
                        "attempted": collection.attempted_ids,
                        "verified": collection.verified_ids,
                        "exportable": collection.exportable_ids,
                        "non_exportable": collection.non_exportable_ids,
                        "navigation_failed": collection.navigation_failed_ids,
                        "non_public": collection.non_public_ids,
                        "failed": collection.failed_ids,
                        "safe_stop_reason": collection.safe_stop_reason,
                        "navigation_strategy_counts": collection.navigation_strategy_counts,
                        "profile_return_counts": collection.profile_return_counts,
                        "field_presence": collection.field_presence,
                        "field_source_counts": collection.field_source_counts,
                        "profile_fields": profile_completeness,
                    },
                    safe_stop_reason=collection.safe_stop_reason,
                )
                self.logger.info(
                    "RUN finished status=%s discovered=%s attempted=%s target_verified=%s current_run_exportable=%s navigation_failed=%s non_exportable=%s failed=%s safe_stop_reason=%s database_total_exportable=%s page_visits=%s excel=%s",
                    status,
                    len(note_cards),
                    collection.attempted_count,
                    len(collection.verified_ids),
                    len(collection.exportable_ids),
                    len(collection.navigation_failed_ids),
                    len(collection.non_exportable_ids),
                    len(collection.failed_ids),
                    collection.safe_stop_reason,
                    database_exportable,
                    budget.page_visits,
                    excel_path,
                )
                self.logger.info("RUN navigation_strategy_counts=%s profile_return_counts=%s", collection.navigation_strategy_counts, collection.profile_return_counts)
                self.logger.info("RUN field_presence=%s field_sources=%s profile_fields=%s", collection.field_presence, collection.field_source_counts, profile_completeness)
                return {
                    "run_id": run_id,
                    "status": status,
                    "login_status": LoginStatus.LOGIN_OK.value,
                    "notes_discovered": len(note_cards),
                    "notes_attempted": collection.attempted_count,
                    "target_verified": len(collection.verified_ids),
                    "notes_completed": len(collection.exportable_ids),
                    "notes_failed": len(collection.failed_ids),
                    "notes_exportable": len(collection.exportable_ids),
                    "navigation_failed": len(collection.navigation_failed_ids),
                    "non_exportable": len(collection.non_exportable_ids),
                    "safe_stop_reason": collection.safe_stop_reason,
                    "page_visits": budget.page_visits,
                    "navigation_strategy_counts": collection.navigation_strategy_counts,
                    "profile_return_counts": collection.profile_return_counts,
                    "field_presence": collection.field_presence,
                    "field_source_counts": collection.field_source_counts,
                    "profile_fields": profile_completeness,
                    "database_total_exportable": database_exportable,
                    "excel": str(excel_path),
                    "offline_qa": offline,
                    "validation_artifact": str(validation_dir),
                    "e2e_review_artifact": str(review_dir) if review_dir else None,
                    "five_note_review_artifact": str(review_dir) if review_dir else None,
                    "excel_readback": excel_readback,
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
        finally:
            self._clear_run_context(run_id, user_id)

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
        review_dir: Any | None = None,
    ) -> CollectionResult:
        result = CollectionResult()
        errors = 0
        completed = set(initial_completed or set())
        max_errors = int(self.app_config.raw.get("safety", {}).get("max_consecutive_errors", 3))
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
                result.count_navigation_strategy(open_result.strategy)
                await self._raise_if_safe_stop(page, "note_after_open", note_id)
                if open_result.reason == "DETAIL_NOT_READY" and not open_result.detail_ready:
                    result.non_exportable_ids.append(note_id)
                    result.validation_notes.append(build_detail_not_ready_validation_note(note_id))
                    self.logger.info(
                        "NOTE attempt_result note_id=%s result=DETAIL_NOT_READY strategy=%s detail_kind=%s reason=%s navigation_success=true detail_ready=false",
                        note_id,
                        open_result.strategy,
                        open_result.detail_kind,
                        open_result.reason,
                    )
                    checkpoint.failed_note_ids = sorted(set(checkpoint.failed_note_ids) | {note_id})
                    checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
                    continue
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
                        result.safe_stop_reason = "MAX_CONSECUTIVE_ERRORS"
                        self.logger.info("SAFE_STOP reason=MAX_CONSECUTIVE_ERRORS count=%s", errors)
                        break
                    continue
                result.verified_ids.append(note_id)
                await browser_flush_if_available(page)
                initial_state_record = await self._extract_initial_state_note_record(page, note_id)
                if initial_state_record:
                    self.structured_by_note[note_id] = merge_public_note_records(
                        self.structured_by_note.get(note_id),
                        initial_state_record,
                        note_id,
                        prefer_incoming=True,
                        incoming_source="DETAIL_INITIAL_STATE",
                    )
                pre_extract_capture = await capture_e2e_review_state(page, note_id) if review_dir else None
                note = await extract_note_dom(page, note_id, 0)
                if note.get("status") != "OK":
                    self.logger.info("NOTE non_ok note_id=%s status=%s reason=%s", note_id, note.get("status"), note.get("status_note"))
                note.update({k: card.get(k) for k in ("is_pinned",) if card.get(k) is not None})
                note = merge_note_with_structured(note, self.structured_by_note.get(note_id))
                record_note_field_stats(result, note)
                self.db.upsert_note(creator.user_id or extract_user_id(creator.url), note)
                self._write_raw("note", note_id, checkpoint.run_id, note)
                if note.get("status") in {"OK", "PARSE_PARTIAL"}:
                    result.exportable_ids.append(note_id)
                    result.validation_notes.append(build_validation_note(note_id, note, detail_ready=True, exportable=True))
                else:
                    result.non_exportable_ids.append(note_id)
                    result.non_public_ids.append(note_id)
                    result.validation_notes.append(build_validation_note(note_id, note, detail_ready=True, exportable=False))
                completed.add(note_id)
                checkpoint.completed_note_ids = sorted(completed)
                checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
                errors = 0
                self.logger.info("NOTE attempt_result note_id=%s result=PARSED title=%s likes=%s exact=%s comments=%s status=%s target_verified=true", note_id, note.get("title"), note.get("likes_value"), note.get("likes_is_exact"), note.get("comments_value"), note.get("status"))
                if review_dir and pre_extract_capture:
                    post_extract_capture = await capture_e2e_review_state(page, note_id)
                    screenshot_tmp = review_dir / f".{note_id}_{uuid.uuid4().hex}.png"
                    await page.screenshot(path=str(screenshot_tmp), full_page=False)
                    artifact_path = write_note_review_artifact(
                        review_dir,
                        note_id,
                        build_five_note_actual_payload(note),
                        pre_extract_capture,
                        post_extract_capture,
                        screenshot_tmp,
                    )
                    screenshot_tmp.unlink(missing_ok=True)
                    self.logger.info("E2E_REVIEW note_artifact_exported note_id=%s path=%s", note_id, artifact_path)
                return_result = await self._return_to_creator_profile(page, creator.url, note_id)
                result.count_profile_return(return_result["strategy"])
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
                checkpoint.mark_safe_stop(stop.reason)
                checkpoint.current_note_id = stop.note_id
                checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
                self.logger.info("SAFE_STOP collect phase=%s note_id=%s status=%s reason=%s", stop.phase, stop.note_id, stop.status.value, stop.reason)
                break
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
                result.safe_stop_reason = "MAX_CONSECUTIVE_ERRORS"
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
                    detail_ready=verified,
                    detail_kind="route_and_detail_ready" if verified else ("route_not_ready" if reason == "DETAIL_NOT_READY" else None),
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
                detail_ready=verified,
                detail_kind="route_and_detail_ready" if verified else ("route_not_ready" if reason == "DETAIL_NOT_READY" else None),
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
        consecutive_verified = 0
        while asyncio.get_event_loop().time() < deadline:
            await self._raise_if_safe_stop(page, "note_wait_target_detail", note_id)
            route_match = route_matches_note(page.url, note_id)
            wrong_route = route_is_note_detail(page.url) and not route_match
            evidence = await self._get_note_detail_evidence(page, note_id)
            detail_count = int(evidence.get("detail_root_count") or 0)
            last_detail_count = detail_count
            if route_match and evidence.get("verified"):
                consecutive_verified += 1
                if consecutive_verified >= 2:
                    return True, None, detail_count
            else:
                consecutive_verified = 0
            if wrong_route:
                return False, "TARGET_MISMATCH", detail_count
            if route_match:
                last_reason = str(evidence.get("detail_ready_reason") or "DETAIL_NOT_READY")
            elif detail_count > 0:
                last_reason = "TARGET_NOT_VERIFIED"
            else:
                last_reason = "CLICK_NO_STATE_CHANGE"
            await page.wait_for_timeout(500)
        if last_reason in {"DETAIL_NOT_READY", "LOADING_STATE", "NO_DETAIL_EVIDENCE", "EMPTY_DETAIL_ROOT"}:
            self.logger.info("DETAIL_READINESS_FAILED note_id=%s reason=%s detail_root_count=%s", note_id, last_reason, last_detail_count)
            last_reason = "DETAIL_NOT_READY"
        return False, last_reason, last_detail_count

    async def _verify_target_note(self, page: Page, note_id: str) -> bool:
        verified, _, _ = await self._wait_for_target_note_detail(page, note_id, timeout_ms=100)
        return verified

    async def _visible_detail_root_count(self, page: Page) -> int:
        evidence = await self._get_note_detail_evidence(page, "")
        return int(evidence.get("detail_root_count") or 0)

    async def _debug_dom_summary(self, page: Page) -> dict[str, Any]:
        return await page.evaluate(
            """
            () => {
              const selectors = [
                "#detail-title",
                "#detail-desc",
                ".engage-bar .like-wrapper",
                ".engage-bar .collect-wrapper",
                ".engage-bar .chat-wrapper",
                '#detail-desc a[href*="search"]',
                'a[href*="/search"]'
              ];
              const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
              };
              const clean = (value) => String(value || "").replace(/\\s+/g, " ").trim().slice(0, 200);
              const matched = [];
              for (const selector of selectors) {
                for (const el of Array.from(document.querySelectorAll(selector)).filter(visible).slice(0, 3)) {
                  matched.push({selector, text_preview: clean(el.innerText || el.textContent || "")});
                }
              }
              return {selectors_checked: selectors, matched_nodes: matched.slice(0, 30)};
            }
            """
        )

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
              const loadingWords = ["加载中"];
              const bodyText = document.body ? (document.body.innerText || document.body.textContent || "") : "";
              const unavailable = unavailableWords.some((word) => bodyText.includes(word));
              const loadingVisible = loadingWords.some((word) => bodyText.includes(word));
              const title = document.querySelector("#detail-title");
              const desc = document.querySelector("#detail-desc");
              const engage = document.querySelector(".engage-bar, [class*=engage], [class*=interaction], [class*=Interact]");
              const metricSelectors = ".engage-bar .count, .engage-bar [class*=count], .engage-bar [class*=Count], .engage-bar [class*=like], .engage-bar [class*=Like], .engage-bar [class*=collect], .engage-bar [class*=Collect], .engage-bar [class*=comment], .engage-bar [class*=Comment]";
              const tagSelectors = '#detail-desc a[href*="search"], #detail-desc a[href*="search_result"], a[href*="/search_result"], a[href*="/search"]';
              const noteRoots = Array.from(document.querySelectorAll('[class*="note-detail"], [class*="noteDetail"], [class*="NoteDetail"], [data-testid*="note-detail"], [role="dialog"]')).filter(visible);
              const exactLinks = noteId ? Array.from(document.querySelectorAll(`a[href*="${noteId}"]`)).filter(visible) : [];
              const clean = (el) => el ? String(el.innerText || el.textContent || "").trim() : "";
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
                  const matched = value.note_id === noteId || value.noteId === noteId || value.id === noteId;
                  if (matched) {
                    const interact = value.interactInfo && typeof value.interactInfo === "object" ? value.interactInfo : {};
                    const hasStructuredDetail = Boolean(
                      value.desc || value.content || value.time || value.create_time || value.createTime ||
                      value.tagList || value.tags ||
                      value.likedCount || value.liked_count || value.collectedCount || value.collected_count || value.commentCount || value.comment_count ||
                      interact.likedCount || interact.liked_count || interact.collectedCount || interact.collected_count || interact.commentCount || interact.comment_count
                    );
                    if (hasStructuredDetail) return true;
                  }
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
              const titleText = clean(title);
              const descText = clean(desc);
              const hasBody = visible(desc) && descText && descText !== titleText && !loadingWords.includes(descText);
              const detailText = noteRoots.map((el) => el.innerText || "").join("\\n");
              const hasPublishTime = /\\b\\d{4}[-/.]\\d{1,2}[-/.]\\d{1,2}\\b|\\b\\d{1,2}[-/.]\\d{1,2}\\b|发布于|编辑于|昨天|今天|前\\b/.test(detailText);
              const metricTexts = Array.from(document.querySelectorAll(metricSelectors)).filter(visible).map((el) => clean(el)).filter(Boolean);
              const numericMetricPattern = /\\b\\d+(?:\\.\\d+)?\\s*(?:万|千|w|W|k|K)?\\b/;
              const labelOnlyMetricCount = metricTexts.filter((text) => /^(赞|点赞|收藏|评论|分享)$/.test(text)).length;
              const hasNumericMetric = metricTexts.some((text) => numericMetricPattern.test(text));
              const hasTags = Array.from(document.querySelectorAll(tagSelectors)).filter(visible).some((el) => clean(el).replace(/^#/, ""));
              const hasStrongDetailEvidence = Boolean(hasBody || hasPublishTime || hasNumericMetric || hasTags || initialStateMatch);
              let detailReadyReason = null;
              if (!noteRoots.length) {
                detailReadyReason = "EMPTY_DETAIL_ROOT";
              } else if (loadingVisible && !hasStrongDetailEvidence) {
                detailReadyReason = "LOADING_STATE";
              } else if (!visible(title) || !hasStrongDetailEvidence) {
                detailReadyReason = "NO_DETAIL_EVIDENCE";
              }
              const independentSignals = [visible(title), visible(desc), visible(engage), exactLinks.length > 0, initialStateMatch].filter(Boolean).length;
              return {
                verified: Boolean(!unavailable && strongRoot && visible(title) && hasStrongDetailEvidence && !detailReadyReason),
                unavailable,
                detail_root_count: noteRoots.length,
                detail_ready_reason: detailReadyReason,
                loading_visible: loadingVisible,
                title_visible: visible(title),
                desc_visible: visible(desc),
                engage_visible: visible(engage),
                body_visible: hasBody,
                publish_time_visible: hasPublishTime,
                interact_metrics_visible: hasNumericMetric,
                numeric_metric_visible: hasNumericMetric,
                label_only_metric_visible: labelOnlyMetricCount > 0,
                label_only_metric_count: labelOnlyMetricCount,
                tags_visible: hasTags,
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
                  const normalize = (value) => {
                    if (!value || typeof value !== "object") return null;
                    const id = value.note_id || value.noteId || value.id;
                    if (id && id !== noteId) return null;
                    const interact = value.interactInfo && typeof value.interactInfo === "object" ? value.interactInfo : {};
                    const firstScalar = (keys, source) => {
                      for (const key of keys) {
                        if (source && Object.prototype.hasOwnProperty.call(source, key) && (typeof source[key] !== "object" || source[key] === null)) return source[key];
                      }
                      return undefined;
                    };
                    const tags = Array.isArray(value.tagList)
                      ? value.tagList.map((item) => item && typeof item === "object" ? (item.name || item.tagName || item.title) : item).filter(Boolean).map(String)
                      : [];
                    const out = {
                      note_id: noteId,
                      id,
                      title: firstScalar(["title", "display_title", "displayTitle"], value),
                      display_title: firstScalar(["display_title", "displayTitle"], value),
                      desc: firstScalar(["desc", "content"], value),
                      type: firstScalar(["type", "note_type", "noteType", "model_type", "modelType"], value),
                      time: firstScalar(["time", "publish_time", "publishTime"], value),
                      liked_count: firstScalar(["liked_count", "likedCount"], value) ?? firstScalar(["liked_count", "likedCount"], interact),
                      collected_count: firstScalar(["collected_count", "collectedCount"], value) ?? firstScalar(["collected_count", "collectedCount"], interact),
                      comment_count: firstScalar(["comment_count", "commentCount"], value) ?? firstScalar(["comment_count", "commentCount"], interact),
                      tags
                    };
                    return Object.fromEntries(Object.entries(out).filter(([, item]) => item !== undefined && item !== null && item !== "" && !(Array.isArray(item) && item.length === 0)));
                  };
                  const exact = window.__INITIAL_STATE__?.note?.noteDetailMap?.[noteId]?.note;
                  const direct = normalize(exact);
                  if (direct) return direct;
                  const hasNoteEvidence = (value) => Boolean(
                    value &&
                    typeof value === "object" &&
                    (
                      Object.prototype.hasOwnProperty.call(value, "interactInfo") ||
                      Object.prototype.hasOwnProperty.call(value, "tagList") ||
                      Object.prototype.hasOwnProperty.call(value, "displayTitle") ||
                      Object.prototype.hasOwnProperty.call(value, "display_title") ||
                      Object.prototype.hasOwnProperty.call(value, "title") ||
                      Object.prototype.hasOwnProperty.call(value, "desc") ||
                      Object.prototype.hasOwnProperty.call(value, "noteType") ||
                      Object.prototype.hasOwnProperty.call(value, "note_type") ||
                      Object.prototype.hasOwnProperty.call(value, "modelType") ||
                      Object.prototype.hasOwnProperty.call(value, "model_type")
                    )
                  );
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
                      if ((value.id === noteId || value.note_id === noteId || value.noteId === noteId) && hasNoteEvidence(value)) return normalize(value);
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
        return normalize_public_note_record(sanitize_json(data), note_id) if data else None

    async def _extract_initial_state_profile_record(self, page: Page, creator_id: str) -> dict[str, Any] | None:
        try:
            data = await page.evaluate(
                """
                (creatorId) => {
                  const normalize = (value) => {
                    if (!value || typeof value !== "object") return null;
                    const id = value.userId || value.user_id || value.id;
                    if (id !== creatorId) return null;
                    const tags = Array.isArray(value.tags)
                      ? value.tags.map((item) => item && typeof item === "object" ? (item.name || item.tagName || item.title) : item).filter(Boolean).map(String)
                      : [];
                    const out = {
                      user_id: creatorId,
                      nickname: value.nickname || value.name,
                      xhs_id: value.redId || value.red_id || value.xhs_id,
                      bio: value.desc || value.bio,
                      avatar_url: value.avatar || value.avatar_url || value.image,
                      ip_location: value.ipLocation || value.ip_location,
                      following: value.follows ?? value.following,
                      followers: value.fans ?? value.followers ?? value.follower_count,
                      interactions: value.interaction ?? value.liked ?? value.liked_count ?? value.interaction_count,
                      gender: value.gender,
                      tags
                    };
                    return Object.fromEntries(Object.entries(out).filter(([, item]) => item !== undefined && item !== null && item !== "" && !(Array.isArray(item) && item.length === 0)));
                  };
                  const direct = normalize(window.__INITIAL_STATE__?.user?.userPageData);
                  if (direct) return direct;
                  const walk = (root) => {
                    if (!root || typeof root !== "object") return null;
                    const seen = new Set();
                    const queue = [root];
                    let scanned = 0;
                    while (queue.length && scanned < 2000) {
                      const value = queue.shift();
                      scanned += 1;
                      if (!value || typeof value !== "object" || seen.has(value)) continue;
                      seen.add(value);
                      const found = normalize(value);
                      if (found) return found;
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
                creator_id,
            )
        except Exception:
            return None
        return extract_public_profile_record(sanitize_json(data), creator_id) if data else None

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
        if run_id != self.current_run_id:
            self.logger.info("STRUCTURED_RESPONSE ignored_stale run_id=%s current_run_id=%s", run_id, self.current_run_id)
            return
        if is_comment_response_path(url):
            self.logger.info("STRUCTURED_RESPONSE skipped_comment_payload path=%s", urlsplit(url).path)
            return
        records, note_limit_reached = extract_public_note_records_with_stats(data)
        if note_limit_reached:
            self.logger.info("STRUCTURED_RESPONSE traversal_limit_reached nodes=%s", MAX_STRUCTURED_NODES_PER_RESPONSE)
        for note_id, record in records.items():
            self.structured_by_note[note_id] = merge_public_note_records(
                self.structured_by_note.get(note_id),
                record,
                note_id,
                prefer_incoming=False,
                incoming_source="PAGE_RESPONSE",
            )
        profile_before = self.structured_profile is not None
        profile_record, profile_limit_reached = extract_public_profile_record_with_stats(data, self.current_creator_id) if self.current_creator_id else (None, False)
        if profile_limit_reached:
            self.logger.info("STRUCTURED_RESPONSE profile_traversal_limit_reached nodes=%s", MAX_STRUCTURED_NODES_PER_RESPONSE)
        if profile_record:
            self.structured_profile = merge_public_profile_records(
                self.structured_profile,
                profile_record,
                self.current_creator_id,
                prefer_incoming=False,
            )
        if records or (self.structured_profile is not None and not profile_before):
            self.logger.info("STRUCTURED_RESPONSE run_id=%s extracted_note_records=%s profile_associated=%s", run_id, len(records), bool(self.structured_profile))

    def _clear_run_context(self, run_id: str, creator_id: str) -> None:
        if self.current_run_id == run_id:
            self.current_run_id = None
            self.current_creator_id = None
            self.structured_by_note = {}
            self.structured_profile = None
            self.logger.info("STRUCTURED_STATE cleared run_id=%s creator_id=%s", run_id, creator_id)

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


def record_note_field_stats(result: CollectionResult, note: dict[str, Any]) -> None:
    values = note_completeness_values(note)
    sources = note.get("field_sources") or {}
    for field_name, value in values.items():
        result.record_field(field_name, field_value_present(value), sources.get(field_name))


def summarize_profile_fields(profile: dict[str, Any], structured: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    fields = {
        "nickname": profile.get("nickname"),
        "user_id": extract_user_id(profile.get("canonical_url") or "") if profile.get("canonical_url") else None,
        "description": profile.get("bio"),
        "followers": profile.get("followers_value"),
        "following": profile.get("following_value"),
        "likes_interaction": profile.get("total_interactions_value"),
        "xhs_id": profile.get("xhs_id"),
        "avatar_url": profile.get("avatar_url"),
        "ip_location": profile.get("ip_location"),
        "profile_tags": profile.get("profile_tags"),
        "identity_tags": profile.get("identity_tags"),
        "gender": profile.get("gender"),
    }
    sources = profile.get("field_sources") or {}
    summary = {}
    for field, value in fields.items():
        present = field_value_present(value)
        summary[field] = {
            "present": present,
            "source": sources.get(field) or ("DOM" if present else "MISSING"),
            "reason": None if present else "NOT_OBSERVED",
        }
    return summary


def field_value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


NOTE_PUBLIC_RECORD_KEYS = {
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
    "tags",
    "_structured_source",
    "_field_sources",
}

NOTE_PUBLIC_BUSINESS_KEYS = {
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
    "tags",
}

STRUCTURED_CANONICAL_FIELDS = {
    "title",
    "body",
    "note_type",
    "publish_time",
    "like_count",
    "collect_count",
    "comment_count",
    "tags",
}

STRUCTURED_FIELD_ALIASES = {
    "title": "title",
    "display_title": "title",
    "desc": "body",
    "content": "body",
    "type": "note_type",
    "note_type": "note_type",
    "model_type": "note_type",
    "time": "publish_time",
    "publish_time": "publish_time",
    "liked_count": "like_count",
    "like_count": "like_count",
    "collected_count": "collect_count",
    "collect_count": "collect_count",
    "comment_count": "comment_count",
    "tags": "tags",
}

NOTE_SCHEMA_EVIDENCE_KEYS = {
    "interactInfo",
    "tagList",
    "displayTitle",
    "display_title",
    "title",
    "desc",
    "noteType",
    "note_type",
    "modelType",
    "model_type",
}

NOTE_SCHEMA_NON_PROFILE_EVIDENCE_KEYS = NOTE_SCHEMA_EVIDENCE_KEYS - {"desc"}

PROFILE_SCHEMA_EVIDENCE_KEYS = {
    "nickname",
    "name",
    "user_id",
    "userId",
    "red_id",
    "redId",
    "xhs_id",
    "fans",
    "followers",
    "follows",
    "following",
}

PROFILE_PUBLIC_RECORD_KEYS = {
    "user_id",
    "nickname",
    "xhs_id",
    "bio",
    "avatar_url",
    "ip_location",
    "followers",
    "following",
    "interactions",
    "gender",
    "tags",
}


def merge_public_note_records(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
    note_id: str,
    *,
    prefer_incoming: bool,
    incoming_source: str,
) -> dict[str, Any]:
    """Merge allowlisted transient note state without letting sparse records erase richer ones."""
    normalized_existing = normalize_public_note_record(existing or {}, note_id) if existing else None
    normalized_incoming = normalize_public_note_record(incoming or {}, note_id) if incoming else None
    existing_source = (existing or {}).get("_structured_source")
    existing_field_sources = _normalize_structured_field_sources((existing or {}).get("_field_sources"))
    if not normalized_existing and not normalized_incoming:
        return {"note_id": note_id, "_structured_source": incoming_source, "_field_sources": {}}
    if not normalized_existing:
        merged = _allowlisted_note_record(normalized_incoming or {"note_id": note_id})
        merged["_structured_source"] = incoming_source
        merged["_field_sources"] = _field_sources_for_record(merged, incoming_source)
        return merged
    if not normalized_incoming:
        merged = _allowlisted_note_record(normalized_existing)
        if existing_source:
            merged["_structured_source"] = existing_source
        merged["_field_sources"] = existing_field_sources
        return merged

    if normalized_incoming.get("note_id") != normalized_existing.get("note_id"):
        raise ValueError(f"structured note_id mismatch: {normalized_existing.get('note_id')} != {normalized_incoming.get('note_id')}")

    merged = _allowlisted_note_record(normalized_existing)
    field_sources = dict(existing_field_sources) or _field_sources_for_record(merged, str(existing_source or incoming_source))
    existing_score = _public_note_completeness_score(normalized_existing)
    incoming_score = _public_note_completeness_score(normalized_incoming)
    same_source_richer = incoming_score > existing_score
    for key, value in _allowlisted_note_record(normalized_incoming).items():
        if key in {"id", "note_id", "_structured_source"}:
            continue
        if key == "_field_sources":
            continue
        if key == "tags":
            before = merged.get("tags") or []
            merged["tags"] = merge_tags(merged.get("tags") or [], value or [])
            if field_value_present(value):
                field_sources["tags"] = _merge_source_labels(field_sources.get("tags"), incoming_source)
            continue
        if not field_value_present(value):
            continue
        canonical_field = STRUCTURED_FIELD_ALIASES.get(key, key)
        if _should_replace_structured_field(
            field_sources.get(canonical_field),
            incoming_source,
            prefer_incoming=prefer_incoming,
            existing_value=merged.get(key),
            incoming_value=value,
            same_source_richer=same_source_richer,
        ):
            merged[key] = value
            field_sources[canonical_field] = incoming_source
    merged = normalize_public_note_record(merged, note_id) or {"note_id": note_id}
    merged["_structured_source"] = _merged_structured_source(existing_source, incoming_source, prefer_incoming)
    merged["_field_sources"] = _normalize_structured_field_sources(field_sources)
    return _allowlisted_note_record(merged)


def merge_public_profile_records(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
    creator_id: str,
    *,
    prefer_incoming: bool,
) -> dict[str, Any] | None:
    """Merge public profile fields for one creator without preserving cross-creator data."""
    normalized_incoming = _allowlisted_profile_record(incoming, creator_id)
    if not normalized_incoming:
        return _allowlisted_profile_record(existing, creator_id)
    normalized_existing = _allowlisted_profile_record(existing, creator_id)
    if not normalized_existing:
        return normalized_incoming
    if normalized_existing.get("user_id") != normalized_incoming.get("user_id"):
        return normalized_existing

    merged = dict(normalized_existing)
    existing_score = _public_profile_completeness_score(normalized_existing)
    incoming_score = _public_profile_completeness_score(normalized_incoming)
    incoming_can_replace_conflicts = prefer_incoming or incoming_score > existing_score
    for key, value in normalized_incoming.items():
        if key == "user_id":
            continue
        if key == "tags":
            merged["tags"] = merge_tags(merged.get("tags") or [], value or [])
            continue
        if not field_value_present(value):
            continue
        if incoming_can_replace_conflicts or not field_value_present(merged.get(key)):
            merged[key] = value
    return _allowlisted_profile_record(merged, creator_id)


def _allowlisted_note_record(record: dict[str, Any]) -> dict[str, Any]:
    clean = {}
    for key, value in record.items():
        if key not in NOTE_PUBLIC_RECORD_KEYS or not field_value_present(value):
            continue
        clean[key] = _normalize_structured_field_sources(value) if key == "_field_sources" else value
    return clean


def _allowlisted_profile_record(record: dict[str, Any] | None, creator_id: str) -> dict[str, Any] | None:
    if not record or record.get("user_id") != creator_id:
        return None
    allowed = {key: value for key, value in record.items() if key in PROFILE_PUBLIC_RECORD_KEYS and field_value_present(value)}
    allowed["user_id"] = creator_id
    return allowed


def _merged_structured_source(existing_source: Any, incoming_source: str, prefer_incoming: bool) -> str:
    if prefer_incoming:
        return incoming_source
    if existing_source == "DETAIL_INITIAL_STATE":
        return "DETAIL_INITIAL_STATE"
    return incoming_source if not existing_source else str(existing_source)


def _field_sources_for_record(record: dict[str, Any], source: str) -> dict[str, str]:
    return {
        canonical: source
        for key, canonical in STRUCTURED_FIELD_ALIASES.items()
        if field_value_present(record.get(key))
    }


def _normalize_structured_field_sources(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, source in value.items():
        key_text = str(key)
        canonical = key_text if key_text in STRUCTURED_CANONICAL_FIELDS else STRUCTURED_FIELD_ALIASES.get(key_text)
        if canonical not in STRUCTURED_CANONICAL_FIELDS:
            continue
        result[canonical] = _merge_source_labels(result.get(canonical), _normalize_source_label(source))
    return result


def _normalize_source_label(source: Any) -> str:
    parts = [part for part in str(source or "").split("+") if part in STRUCTURED_SOURCE_PRIORITY]
    if not parts:
        return "PAGE_RESPONSE"
    return "+".join(sorted(set(parts), key=lambda item: STRUCTURED_SOURCE_PRIORITY[item]))


def _merge_source_labels(existing: Any, incoming: str) -> str:
    return _normalize_source_label("+".join([str(existing or ""), incoming]))


def _structured_source_priority(label: Any) -> int:
    return max((STRUCTURED_SOURCE_PRIORITY.get(part, 0) for part in str(label or "").split("+")), default=0)


def _should_replace_structured_field(
    existing_source: Any,
    incoming_source: str,
    *,
    prefer_incoming: bool,
    existing_value: Any,
    incoming_value: Any,
    same_source_richer: bool,
) -> bool:
    if not field_value_present(incoming_value):
        return False
    if not field_value_present(existing_value):
        return True
    incoming_priority = _structured_source_priority(incoming_source)
    existing_priority = _structured_source_priority(existing_source)
    if incoming_priority > existing_priority:
        return True
    if incoming_priority < existing_priority:
        return False
    return prefer_incoming or same_source_richer


def _public_note_completeness_score(record: dict[str, Any]) -> int:
    return len(_field_sources_for_record(record, "PAGE_RESPONSE"))


def _public_profile_completeness_score(record: dict[str, Any]) -> int:
    return sum(1 for key in PROFILE_PUBLIC_RECORD_KEYS if key != "user_id" and field_value_present(record.get(key)))


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


def is_comment_response_path(url: str) -> bool:
    path = urlsplit(url).path.lower()
    segments = [segment for segment in path.split("/") if segment]
    return "comment" in segments or path.endswith("/comment/page") or path.endswith("/comment/sub/page")


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
}

PROFILE_PUBLIC_FIELD_ALIASES = {
    "user_id": "user_id",
    "id": "user_id",
    "userId": "user_id",
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
    "ipLocation": "ip_location",
    "ip_location": "ip_location",
    "fans": "followers",
    "followers": "followers",
    "follower_count": "followers",
    "follows": "following",
    "following": "following",
    "liked": "interactions",
    "liked_count": "interactions",
    "interaction_count": "interactions",
    "interaction": "interactions",
    "interactions": "interactions",
    "gender": "gender",
    "tags": "tags",
}

STRUCTURED_TRAVERSAL_LIMIT = object()


def _bounded_walk(value: Any):
    queue = deque([value])
    seen: set[int] = set()
    nodes = 0
    while queue:
        if nodes >= MAX_STRUCTURED_NODES_PER_RESPONSE:
            yield STRUCTURED_TRAVERSAL_LIMIT
            return
        node = queue.popleft()
        if isinstance(node, (dict, list)):
            object_id = id(node)
            if object_id in seen:
                continue
            seen.add(object_id)
        nodes += 1
        yield node
        if isinstance(node, dict):
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)


def _note_candidate_id(value: dict[str, Any]) -> str | None:
    if not _has_note_schema_evidence(value):
        return None
    for key in ("note_id", "noteId", "id"):
        candidate_id = value.get(key)
        if _is_valid_public_id(candidate_id):
            return str(candidate_id)
    return None


def _is_valid_public_id(value: Any) -> bool:
    return isinstance(value, str) and len(value) == NOTE_ID_LENGTH


def _has_note_schema_evidence(value: dict[str, Any]) -> bool:
    has_note_evidence = any(key in value for key in NOTE_SCHEMA_EVIDENCE_KEYS)
    if not has_note_evidence:
        return False
    has_profile_evidence = any(key in value for key in PROFILE_SCHEMA_EVIDENCE_KEYS)
    has_non_profile_note_evidence = any(key in value for key in NOTE_SCHEMA_NON_PROFILE_EVIDENCE_KEYS)
    return not has_profile_evidence or has_non_profile_note_evidence


def _normalize_public_profile_candidate(value: dict[str, Any], creator_id: str) -> dict[str, Any] | None:
    candidate_id = value.get("user_id") or value.get("userId") or value.get("id")
    if candidate_id != creator_id:
        return None
    record: dict[str, Any] = {"user_id": creator_id}
    for key, out_key in PROFILE_PUBLIC_FIELD_ALIASES.items():
        if key not in value or is_sensitive_key(key):
            continue
        item = value.get(key)
        if isinstance(item, (dict, list)) and out_key != "tags":
            continue
        if out_key == "avatar_url":
            record[out_key] = sanitize_url(str(item)) if item else None
        elif out_key == "tags":
            record[out_key] = normalize_profile_tags(item)
        else:
            record[out_key] = sanitize_json(item)
    return _allowlisted_profile_record(record, creator_id)


def extract_public_note_records(value: Any) -> dict[str, dict[str, Any]]:
    records, _ = extract_public_note_records_with_stats(value)
    return records


def extract_public_note_records_with_stats(value: Any) -> tuple[dict[str, dict[str, Any]], bool]:
    records: dict[str, dict[str, Any]] = {}
    limit_reached = False
    for node in _bounded_walk(value):
        if node is STRUCTURED_TRAVERSAL_LIMIT:
            limit_reached = True
            break
        if not isinstance(node, dict):
            continue
        note_id = _note_candidate_id(node)
        if not note_id:
            continue
        record = normalize_public_note_record(node, note_id)
        if not record:
            continue
        records[note_id] = merge_public_note_records(
            records.get(note_id),
            sanitize_json(record),
            note_id,
            prefer_incoming=False,
            incoming_source="PAGE_RESPONSE",
        )
    return records, limit_reached


def extract_public_profile_record(value: Any, creator_id: str) -> dict[str, Any] | None:
    record, _ = extract_public_profile_record_with_stats(value, creator_id)
    return record


def extract_public_profile_record_with_stats(value: Any, creator_id: str) -> tuple[dict[str, Any] | None, bool]:
    merged: dict[str, Any] | None = None
    limit_reached = False
    for node in _bounded_walk(value):
        if node is STRUCTURED_TRAVERSAL_LIMIT:
            limit_reached = True
            break
        if not isinstance(node, dict):
            continue
        record = _normalize_public_profile_candidate(node, creator_id)
        if not record:
            continue
        merged = merge_public_profile_records(
            merged,
            record,
            creator_id,
            prefer_incoming=False,
        )
    return merged, limit_reached


def is_public_profile_payload(value: Any, creator_id: str) -> bool:
    return extract_public_profile_record(value, creator_id) is not None


def merge_profile_with_structured(profile: dict[str, Any], structured: dict[str, Any]) -> dict[str, Any]:
    merged = dict(profile)
    field_sources = dict(merged.get("field_sources") or {})
    if structured.get("nickname") and not merged.get("nickname"):
        merged["nickname"] = structured.get("nickname")
        field_sources["nickname"] = "STRUCTURED_PUBLIC"
    if structured.get("xhs_id") and not merged.get("xhs_id"):
        merged["xhs_id"] = structured.get("xhs_id")
        field_sources["xhs_id"] = "STRUCTURED_PUBLIC"
    if structured.get("bio") and not merged.get("bio"):
        merged["bio"] = structured.get("bio")
        field_sources["description"] = "STRUCTURED_PUBLIC"
    if structured.get("avatar_url") and not merged.get("avatar_url"):
        merged["avatar_url"] = sanitize_url(str(structured.get("avatar_url")))
        field_sources["avatar_url"] = "STRUCTURED_PUBLIC"
    if structured.get("ip_location") and not merged.get("ip_location"):
        merged["ip_location"] = structured.get("ip_location")
        field_sources["ip_location"] = "STRUCTURED_PUBLIC"
    for source_key, prefix in [("following", "following"), ("followers", "followers"), ("interactions", "total_interactions")]:
        if structured.get(source_key) is not None and merged.get(f"{prefix}_value") is None:
            value, raw, exact = parse_count(structured.get(source_key))
            merged[f"{prefix}_value"] = value
            merged[f"{prefix}_raw"] = raw
            merged[f"{prefix}_is_exact"] = exact
            field_sources[{"following": "following", "followers": "followers", "total_interactions": "likes_interaction"}[prefix]] = "STRUCTURED_PUBLIC"
    if structured.get("gender") not in (None, "", 0, "0") and merged.get("gender") is None:
        merged["gender"] = normalize_gender(structured.get("gender"))
        field_sources["gender"] = "STRUCTURED_PUBLIC"
    if structured.get("tags") and not merged.get("profile_tags"):
        merged["profile_tags"] = normalize_profile_tags(structured.get("tags"))
        field_sources["profile_tags"] = "STRUCTURED_PUBLIC"
    merged["field_sources"] = field_sources
    return merged


def normalize_profile_tags(value: Any) -> list[str]:
    return normalize_tag_names(value)


def normalize_gender(value: Any) -> str | None:
    if value in (None, "", 0, "0"):
        return None
    text = str(value)
    if text in {"1", "male", "男"}:
        return "男"
    if text in {"2", "female", "女"}:
        return "女"
    return text

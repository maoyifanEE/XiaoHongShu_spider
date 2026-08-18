from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from pathlib import Path
from typing import Any

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
from .state import LoginStatus, RunStatus
from .time_utils import now_iso
from .utils import canonical_note_url, sanitize_json


class Crawler:
    def __init__(self, app_config: AppConfig, db: Database, logger: logging.Logger):
        self.app_config = app_config
        self.db = db
        self.logger = logger
        self.structured_by_note: dict[str, dict[str, Any]] = {}
        self.structured_profile: dict[str, Any] | None = None

    async def run(self, mode: str, creator_filter: str | None = None, max_notes: int | None = None) -> dict[str, Any]:
        results = {}
        creators = [c for c in self.app_config.creators if c.enabled and self._matches(c, creator_filter)]
        if not creators:
            raise ValueError("没有匹配且启用的 creator")
        for creator in creators:
            result = await self._run_creator(creator, mode, max_notes=max_notes)
            results[creator.name] = result
        return results

    async def _run_creator(self, creator: CreatorConfig, mode: str, max_notes: int | None = None) -> dict[str, Any]:
        user_id = creator.user_id or extract_user_id(creator.url)
        run_id = f"{now_iso().replace(':', '').replace('+', '_')}_{uuid.uuid4().hex[:8]}"
        self.db.start_run(run_id, user_id, creator.name, self.app_config.version)
        checkpoint = Checkpoint(run_id=run_id, creator_id=user_id)
        checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
        self.logger.info("RUN_ID=%s CREATOR=%s USER_ID=%s MODE=%s", run_id, creator.name, user_id, mode)

        if mode == "login-only":
            return await self._login_only(creator, user_id, run_id)

        try:
            async with BrowserSession(self.app_config.base_dir, self.app_config.raw, self.logger) as browser:
                browser.response_callback = lambda url, data: self._capture_structured(run_id, url, data)
                page = await browser.new_page()
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

                note_cards = await self._discover_notes(page, checkpoint)
                self.logger.info("DISCOVERY completed total_unique=%s first_ids=%s", len(note_cards), [item["note_id"] for item in note_cards[:5]])
                configured_limit = self.app_config.raw.get("safety", {}).get("max_notes_per_run")
                limit = max_notes if max_notes is not None else configured_limit
                target_exportable = None
                if mode == "smoke":
                    collection_cfg = self.app_config.raw.get("collection", {})
                    target_exportable = int(collection_cfg.get("smoke_note_limit", 3))
                    limit = int(collection_cfg.get("smoke_max_attempts", max(target_exportable, 12)))
                if limit:
                    note_cards = note_cards[: int(limit)]

                done, failed = await self._collect_notes(page, creator, note_cards, checkpoint, target_exportable=target_exportable)
                offline = run_offline_qa(self.db, user_id, self.logger)
                excel_path = export_excel(self.db, self.app_config.base_dir, user_id, creator.name, self.logger)
                exported_count = len(self.db.current_notes(user_id))
                status = RunStatus.SUCCESS.value if not failed and exported_count > 0 else RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value
                self.db.finish_run(
                    run_id,
                    status,
                    browser_version=browser.browser_version,
                    login_status=LoginStatus.LOGIN_OK.value,
                    notes_discovered=len(note_cards),
                    notes_completed=len(done),
                    notes_failed=len(failed),
                    notes={"completed": done, "failed": failed},
                )
                self.logger.info("RUN finished status=%s discovered=%s completed=%s failed=%s excel=%s", status, len(note_cards), len(done), len(failed), excel_path)
                return {
                    "run_id": run_id,
                    "status": status,
                    "login_status": LoginStatus.LOGIN_OK.value,
                    "notes_discovered": len(note_cards),
                    "notes_completed": len(done),
                    "notes_failed": len(failed),
                    "notes_exportable": exported_count,
                    "excel": str(excel_path),
                    "offline_qa": offline,
                }
        except KeyboardInterrupt:
            checkpoint.safe_stop_reason = "USER_INTERRUPTED"
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

    async def _discover_notes(self, page: Page, checkpoint: Checkpoint) -> list[dict[str, Any]]:
        safety = self.app_config.raw.get("safety", {})
        idle_limit = int(safety.get("scroll_idle_rounds", 5))
        max_rounds = int(safety.get("scroll_max_rounds", 300))
        seen: dict[str, dict[str, Any]] = {}
        idle_rounds = 0
        last_height = 0
        for round_no in range(1, max_rounds + 1):
            status = await detect_page_status(page)
            if status != LoginStatus.LOGIN_OK:
                checkpoint.safe_stop_reason = status.value
                checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
                self.logger.info("DISCOVERY safe_stop status=%s", status.value)
                break
            cards = await discover_note_cards(page)
            before = len(seen)
            for card in cards:
                seen.setdefault(card["note_id"], card)
            height = await page.evaluate("() => document.body ? document.body.scrollHeight : 0")
            added = len(seen) - before
            self.logger.info("DISCOVERY round=%s known=%s added=%s scroll_height=%s", round_no, len(seen), added, height)
            checkpoint.discovered_note_ids = list(seen)
            checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
            if added == 0 and height <= last_height:
                idle_rounds += 1
            else:
                idle_rounds = 0
            if idle_rounds >= idle_limit:
                self.logger.info("DISCOVERY termination_reason=idle_rounds_without_new_notes rounds=%s", idle_rounds)
                break
            last_height = max(last_height, height)
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await self._polite_delay()
        return list(seen.values())

    async def _collect_notes(self, page: Page, creator: CreatorConfig, note_cards: list[dict[str, Any]], checkpoint: Checkpoint, target_exportable: int | None = None) -> tuple[list[str], list[str]]:
        done: list[str] = []
        failed: list[str] = []
        exportable_done: list[str] = []
        errors = 0
        max_errors = int(self.app_config.raw.get("safety", {}).get("max_consecutive_errors", 3))
        top_n = int(self.app_config.raw.get("collection", {}).get("collect_top_comments", 3))
        for index, card in enumerate(note_cards, start=1):
            note_id = card["note_id"]
            checkpoint.current_note_id = note_id
            checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
            try:
                self.logger.info("[%s/%s] NOTE open note_id=%s", index, len(note_cards), note_id)
                page = await self._open_note_from_profile(page, creator.url, card)
                await page.wait_for_timeout(5000)
                status = await detect_page_status(page)
                if status != LoginStatus.LOGIN_OK:
                    checkpoint.safe_stop_reason = status.value
                    checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
                    self.logger.info("NOTE safe_stop note_id=%s status=%s", note_id, status.value)
                    break
                note = await extract_note_dom(page, note_id, top_n)
                if note.get("status") != "OK":
                    self.logger.info("NOTE non_ok note_id=%s status=%s reason=%s", note_id, note.get("status"), note.get("status_note"))
                note.update({k: card.get(k) for k in ("is_pinned",) if card.get(k) is not None})
                note = merge_note_with_structured(note, self.structured_by_note.get(note_id))
                self.db.upsert_note(creator.user_id or extract_user_id(creator.url), note)
                self._write_raw("note", note_id, checkpoint.run_id, note)
                done.append(note_id)
                if note.get("status") in {"OK", "PARSE_PARTIAL"}:
                    exportable_done.append(note_id)
                checkpoint.completed_note_ids = done
                checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
                errors = 0
                self.logger.info("NOTE ok note_id=%s title=%s likes=%s exact=%s comments=%s top_level_comments=%s status=%s", note_id, note.get("title"), note.get("likes_value"), note.get("likes_is_exact"), note.get("comments_value"), len(note.get("top_comments", [])), note.get("status"))
            except PlaywrightTimeoutError as exc:
                errors += 1
                failed.append(note_id)
                await self._save_error_screenshot(page, note_id, "timeout")
                self.logger.warning("NOTE failed note_id=%s error_type=%s", note_id, type(exc).__name__)
            except Exception as exc:
                errors += 1
                failed.append(note_id)
                await self._save_error_screenshot(page, note_id, type(exc).__name__)
                self.logger.exception("NOTE failed note_id=%s error_type=%s", note_id, type(exc).__name__)
            checkpoint.failed_note_ids = failed
            checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
            if errors >= max_errors:
                checkpoint.safe_stop_reason = "MAX_CONSECUTIVE_ERRORS"
                checkpoint.save(self.app_config.base_dir / "data" / "checkpoints")
                self.logger.info("SAFE_STOP reason=MAX_CONSECUTIVE_ERRORS count=%s", errors)
                break
            if target_exportable is not None and len(exportable_done) >= target_exportable:
                self.logger.info("SMOKE target_exportable_reached target=%s attempted=%s exportable=%s", target_exportable, len(done), len(exportable_done))
                break
            await self._polite_delay()
        return done, failed

    async def _open_note_from_profile(self, page: Page, profile_url: str, card: dict[str, Any]) -> Page:
        note_id = card["note_id"]
        await page.goto(profile_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        try:
            found = False
            visible = 0
            for round_no in range(1, 25):
                stats = await page.evaluate(
                    """
                    (noteId) => {
                      const links = Array.from(document.querySelectorAll(`a[href*="${noteId}"]`));
                      const visible = links.filter((el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
                      });
                      return { total: links.length, visible: visible.length };
                    }
                    """,
                    note_id,
                )
                count = int(stats.get("total") or 0)
                visible = int(stats.get("visible") or 0)
                self.logger.info("NOTE profile_find note_id=%s round=%s matched_links=%s visible_links=%s", note_id, round_no, count, visible)
                if count > 0:
                    found = True
                    break
                await page.evaluate("() => window.scrollBy(0, Math.max(600, window.innerHeight * 0.8))")
                await page.wait_for_timeout(1200)
            if not found:
                raise RuntimeError("profile card not found after bounded scroll")
            before_url = page.url
            self.logger.info("NOTE open_strategy=profile_dom_click note_id=%s visible_links=%s", note_id, visible)
            clicked = await page.evaluate(
                """
                (noteId) => {
                  const links = Array.from(document.querySelectorAll(`a[href*="${noteId}"]`));
                  const visible = links.filter((el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
                  });
                  const target = visible[0] || links[0];
                  if (!target) return false;
                  target.scrollIntoView({block: "center", inline: "center"});
                  target.click();
                  return true;
                }
                """,
                note_id,
            )
            if not clicked:
                raise RuntimeError("dom click target missing")
            await page.wait_for_timeout(5000)
            self.logger.info("NOTE click_result note_id=%s before_url=%s after_url=%s", note_id, before_url, page.url)
            return page
        except Exception as exc:
            self.logger.info("NOTE open_strategy=direct_url note_id=%s reason=%s", note_id, type(exc).__name__)
            await page.goto(card.get("access_url") or card.get("canonical_url") or canonical_note_url(note_id), wait_until="domcontentloaded")
            return page

    def _capture_structured(self, run_id: str, url: str, data: Any) -> None:
        clean = sanitize_json(data)
        text = json.dumps(clean, ensure_ascii=False)
        note_ids = set()
        for maybe in self.structured_by_note:
            if maybe in text:
                note_ids.add(maybe)
        import re

        note_ids.update(re.findall(r"\b[0-9a-fA-F]{24}\b", text))
        for note_id in list(note_ids)[:50]:
            self.structured_by_note[note_id] = clean
            self.db.save_raw(run_id, "response", note_id, "browser_response", {"url": url, "data": clean})
        if "user/profile" in url or "user" in text:
            self.structured_profile = clean

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

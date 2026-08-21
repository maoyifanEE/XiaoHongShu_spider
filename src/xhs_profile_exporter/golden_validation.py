from __future__ import annotations

import json
import logging
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
    "note_type",
    "publish_time",
    "like_count",
    "collect_count",
    "comment_count",
    "share_count",
    "tags",
]


def load_golden_fixtures(fixture_dir: Path) -> list[dict[str, Any]]:
    fixtures = []
    for path in sorted(fixture_dir.glob("note_*.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        fixture["_fixture_path"] = str(path)
        fixtures.append(fixture)
    return fixtures


def normalized_extraction_from_note(note: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sources = note.get("field_sources") or {}
    return {
        "title": _field(note.get("title"), sources.get("title")),
        "note_type": _field(note.get("note_type"), sources.get("note_type")),
        "publish_time": _field(note.get("publish_time"), sources.get("publish_time")),
        "like_count": _field(note.get("likes_value"), sources.get("like_count")),
        "collect_count": _field(note.get("collects_value"), sources.get("collect_count")),
        "comment_count": _field(note.get("comments_value"), sources.get("comment_count")),
        "share_count": _field(note.get("shares_value"), sources.get("share_count")),
        "tags": _field(note.get("hashtags") or [], sources.get("tags")),
    }


def compare_golden_expected(note_id: str, expected: dict[str, Any], actual: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    diffs = []
    for field in COMPARE_FIELDS:
        expected_value = expected.get(field)
        actual_field = actual.get(field) or {}
        actual_value = actual_field.get("value")
        if expected_value != actual_value:
            diffs.append(
                {
                    "note_id": note_id,
                    "field": field,
                    "expected": expected_value,
                    "actual": actual_value,
                    "source": actual_field.get("source", "MISSING"),
                }
            )
    return diffs


async def run_live_golden_validation(
    config: AppConfig,
    db: Database,
    logger: logging.Logger,
    fixture_dir: Path,
    creator_filter: str | None = None,
) -> dict[str, Any]:
    fixtures = load_golden_fixtures(fixture_dir)
    logger = logger or logging.getLogger(__name__)
    crawler = Crawler(config, db, logger)
    results = []
    all_diffs = []
    for fixture in fixtures:
        note_id = fixture["note_id"]
        logger.info("GOLDEN_LIVE start note_id=%s fixture=%s", note_id, fixture.get("_fixture_path"))
        extraction = await extract_note_for_validation(crawler, note_id, creator_filter)
        status = extraction.get("status")
        actual = extraction.get("fields") or {}
        diffs = compare_golden_expected(note_id, fixture["expected"], actual) if status == "OK" else []
        all_diffs.extend(diffs)
        results.append(
            {
                "note_id": note_id,
                "status": status,
                "login_status": extraction.get("login_status"),
                "detail_ready": extraction.get("detail_ready"),
                "artifact": extraction.get("artifact"),
                "diffs": diffs,
            }
        )
    passed = all(item.get("status") == "OK" for item in results) and not all_diffs
    return {"mode": "golden-live", "passed": passed, "notes": results, "diffs": all_diffs}


async def extract_note_for_validation(crawler: Crawler, note_id: str, creator_filter: str | None = None) -> dict[str, Any]:
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
    crawler.logger.info("GOLDEN_EXTRACT start run_id=%s note_id=%s creator_id=%s", run_id, note_id, user_id)
    budget = crawler._build_budget()
    checkpoint = Checkpoint(run_id=run_id, creator_id=user_id)
    try:
        async with BrowserSession(crawler.app_config.base_dir, crawler.app_config.raw, crawler.logger) as browser:
            browser.response_callback = lambda url, data: crawler._capture_structured(run_id, url, data)
            page = await browser.new_page()
            budget.count_page_visit("golden_login_check", note_id)
            login_status = await browser.check_login(page, creator.url)
            if login_status in {LoginStatus.LOGIN_EXPIRED, LoginStatus.HUMAN_VERIFICATION_REQUIRED}:
                login_status = await browser.wait_for_login(page, creator.url)
            if login_status != LoginStatus.LOGIN_OK:
                return {"run_id": run_id, "status": login_status.value, "login_status": login_status.value, "note_id": note_id}

            note_cards = await crawler._discover_notes(page, checkpoint, budget, target_unique=60)
            card = next((item for item in note_cards if item.get("note_id") == note_id), None)
            if not card:
                crawler.logger.info("GOLDEN_EXTRACT note_not_found run_id=%s note_id=%s discovered=%s", run_id, note_id, len(note_cards))
                return {"run_id": run_id, "status": "NOTE_NOT_FOUND", "login_status": LoginStatus.LOGIN_OK.value, "note_id": note_id, "notes_discovered": len(note_cards)}

            open_result = await crawler._open_note_from_profile(page, creator.url, card, budget)
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
            return {
                "run_id": run_id,
                "status": "OK",
                "login_status": LoginStatus.LOGIN_OK.value,
                "note_id": note_id,
                "detail_ready": True,
                "fields": normalized_extraction_from_note(note),
            }
    except SafeStopRequested as stop:
        crawler.logger.info("SAFE_STOP golden_extract phase=%s note_id=%s status=%s reason=%s", stop.phase, stop.note_id, stop.status.value, stop.reason)
        return {"run_id": run_id, "status": stop.reason, "login_status": stop.status.value, "note_id": note_id, "safe_stop_reason": stop.reason}
    finally:
        crawler._clear_run_context(run_id, user_id)


def _field(value: Any, source: Any) -> dict[str, Any]:
    if not _present(value):
        return {"value": None, "source": "MISSING"}
    return {"value": value, "source": str(source or "MISSING")}


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True

import asyncio
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

import xhs_profile_exporter.crawler as crawler_module
from xhs_profile_exporter.config import AppConfig, CreatorConfig
from xhs_profile_exporter.crawler import (
    Crawler,
    _normalize_structured_field_sources,
    determine_run_status,
    extract_public_note_records,
    extract_public_profile_record,
    is_comment_response_path,
    merge_public_note_records,
    merge_public_profile_records,
    route_matches_note,
    safe_route_summary,
    select_visible_note_cover_candidate,
    summarize_profile_fields,
)
from xhs_profile_exporter.runtime import CollectionResult, OpenNoteResult, SafeStopRequested
from xhs_profile_exporter.state import LoginStatus, RunStatus


class DummyLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class DummyDb:
    def __init__(self):
        self.finished = None
        self.saved_notes = []

    def start_run(self, *args, **kwargs):
        pass

    def finish_run(self, run_id, status, **fields):
        self.finished = {"run_id": run_id, "status": status, **fields}

    def save_profile_snapshot(self, *args, **kwargs):
        pass

    def save_raw(self, *args, **kwargs):
        pass

    def current_notes(self, creator_id):
        return ["historical"] * 10

    def upsert_note(self, creator_id, note):
        self.saved_notes.append(note)


class FakePage:
    async def wait_for_timeout(self, timeout):
        pass

    async def screenshot(self, *args, **kwargs):
        pass


class FakeBrowserSession:
    def __init__(self, *args, **kwargs):
        self.browser_version = "fake"
        self.response_callback = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def new_page(self):
        return FakePage()

    async def check_login(self, page, url):
        return LoginStatus.LOGIN_OK


def app_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        base_dir=tmp_path,
        raw={
            "program": {"version": "0.1.0"},
            "safety": {"max_consecutive_errors": 3, "max_page_visits_per_run": None, "max_runtime_minutes": None},
            "collection": {"collect_top_comments": 3, "smoke_note_limit": 3, "smoke_max_attempts": 12},
        },
        creators=[
            CreatorConfig(
                name="辣香郭",
                url="https://www.xiaohongshu.com/user/profile/5cfb1f8e00000000100322e4",
                enabled=True,
                user_id="5cfb1f8e00000000100322e4",
            )
        ],
    )


def test_safe_stop_in_discovery_does_not_call_collect(tmp_path: Path, monkeypatch):
    db = DummyDb()
    crawler = Crawler(app_config(tmp_path), db, DummyLogger())
    collect_called = False

    async def fake_profile(*args, **kwargs):
        return {"captured_at": "now", "nickname": "n", "source": "test"}

    async def fake_discover(*args, **kwargs):
        raise SafeStopRequested(LoginStatus.RISK_CONTROL_DETECTED, "RISK_CONTROL_DETECTED", "discovery")

    async def fake_collect(*args, **kwargs):
        nonlocal collect_called
        collect_called = True
        return CollectionResult()

    monkeypatch.setattr(crawler_module, "BrowserSession", FakeBrowserSession)
    monkeypatch.setattr(crawler_module, "extract_profile_dom", fake_profile)
    monkeypatch.setattr(crawler, "_discover_notes", fake_discover)
    monkeypatch.setattr(crawler, "_collect_notes", fake_collect)

    result = asyncio.run(crawler._run_creator(app_config(tmp_path).creators[0], "collect"))
    assert collect_called is False
    assert result["status"] == RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value
    assert db.finished["risk_detected"] == 1


def test_target_not_verified_does_not_parse_or_upsert(tmp_path: Path, monkeypatch):
    db = DummyDb()
    crawler = Crawler(app_config(tmp_path), db, DummyLogger())
    page = FakePage()
    creator = app_config(tmp_path).creators[0]

    async def fake_open(*args, **kwargs):
        return OpenNoteResult(page=page, note_id="66dabcde000000001f01abcd", strategy="test", target_verified=False, reason="TARGET_NOT_VERIFIED")

    async def parser_should_not_run(*args, **kwargs):
        raise AssertionError("parser must not run when target is not verified")

    monkeypatch.setattr(crawler, "_open_note_from_profile", fake_open)
    monkeypatch.setattr(crawler, "_save_error_screenshot", lambda *args, **kwargs: _noop_async())
    monkeypatch.setattr(crawler_module, "extract_note_dom", parser_should_not_run)

    result = asyncio.run(
        crawler._collect_notes(
            page,
            creator,
            [{"note_id": "66dabcde000000001f01abcd"}],
            checkpoint=_checkpoint(tmp_path),
            budget=crawler._build_budget(),
        )
    )
    assert result.navigation_failed_ids == ["66dabcde000000001f01abcd"]
    assert db.saved_notes == []


def test_cover_not_found_does_not_goto_access_or_canonical_url(tmp_path: Path, monkeypatch):
    crawler = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())
    visited = []

    class NavPage(FakePage):
        url = "https://www.xiaohongshu.com/user/profile/5cfb1f8e00000000100322e4"

        async def goto(self, url, wait_until=None):
            visited.append(url)
            self.url = url

        def locator(self, selector):
            return BodyLocator("")

    class BodyLocator:
        async def inner_text(self, timeout=0):
            return ""

    async def no_current_cover(*args, **kwargs):
        return OpenNoteResult(page=args[0], note_id="66dabcde000000001f01abcd", strategy="current_mounted_cover_click", target_verified=False, reason="VISIBLE_COVER_NOT_FOUND")

    async def no_scan_cover(*args, **kwargs):
        return {"locator": None, "rounds": 1, "cover_candidates": 0}

    async def no_safe_stop(*args, **kwargs):
        return None

    monkeypatch.setattr(crawler, "_click_visible_note_cover_on_current_page", no_current_cover)
    monkeypatch.setattr(crawler, "_scan_profile_for_visible_note_cover", no_scan_cover)
    monkeypatch.setattr(crawler, "_raise_if_safe_stop", no_safe_stop)
    card = {
        "note_id": "66dabcde000000001f01abcd",
        "access_url": "https://www.xiaohongshu.com/explore/66dabcde000000001f01abcd?xsec_token=SECRET",
        "canonical_url": "https://www.xiaohongshu.com/explore/66dabcde000000001f01abcd",
    }
    result = asyncio.run(crawler._open_note_from_profile(NavPage(), app_config(tmp_path).creators[0].url, card, crawler._build_budget()))
    assert result.target_verified is False
    assert visited == [app_config(tmp_path).creators[0].url]
    assert all("/explore/" not in url for url in visited)


def test_historical_db_does_not_make_zero_exportable_run_success(tmp_path: Path, monkeypatch):
    db = DummyDb()
    crawler = Crawler(app_config(tmp_path), db, DummyLogger())

    async def fake_profile(*args, **kwargs):
        return {"captured_at": "now", "nickname": "n", "source": "test"}

    async def fake_discover(*args, **kwargs):
        return [{"note_id": "66dabcde000000001f01abcd"}]

    async def fake_collect(*args, **kwargs):
        return CollectionResult(attempted_ids=["66dabcde000000001f01abcd"], non_exportable_ids=["66dabcde000000001f01abcd"])

    monkeypatch.setattr(crawler_module, "BrowserSession", FakeBrowserSession)
    monkeypatch.setattr(crawler_module, "extract_profile_dom", fake_profile)
    monkeypatch.setattr(crawler, "_discover_notes", fake_discover)
    monkeypatch.setattr(crawler, "_collect_notes", fake_collect)
    monkeypatch.setattr(crawler_module, "run_offline_qa", lambda *args, **kwargs: {"passed": True})
    monkeypatch.setattr(crawler_module, "export_excel", lambda *args, **kwargs: tmp_path / "out.xlsx")

    result = asyncio.run(crawler._run_creator(app_config(tmp_path).creators[0], "collect"))
    assert result["status"] == RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value
    assert result["notes_exportable"] == 0
    assert result["database_total_exportable"] == 10


def test_multi_creator_structured_state_isolation(tmp_path: Path, monkeypatch):
    db = DummyDb()
    crawler = Crawler(app_config(tmp_path), db, DummyLogger())
    creator_b = CreatorConfig(
        name="creator_b",
        url="https://www.xiaohongshu.com/user/profile/aaaaaaaaaaaaaaaaaaaaaaaa",
        enabled=True,
        user_id="aaaaaaaaaaaaaaaaaaaaaaaa",
    )
    crawler.structured_profile = {"user_id": "5cfb1f8e00000000100322e4", "nickname": "creator_a"}
    crawler.structured_by_note = {"66dabcde000000001f01abcd": {"note_id": "66dabcde000000001f01abcd", "title": "a"}}

    async def fake_profile(*args, **kwargs):
        assert crawler.structured_profile is None
        assert crawler.structured_by_note == {}
        return {"captured_at": "now", "nickname": "creator_b", "canonical_url": creator_b.url, "source": "test"}

    async def fake_discover(*args, **kwargs):
        raise SafeStopRequested(LoginStatus.RISK_CONTROL_DETECTED, "RISK_CONTROL_DETECTED", "discovery")

    monkeypatch.setattr(crawler_module, "BrowserSession", FakeBrowserSession)
    monkeypatch.setattr(crawler_module, "extract_profile_dom", fake_profile)
    monkeypatch.setattr(crawler, "_extract_initial_state_profile_record", lambda *args, **kwargs: _return_async(None))
    monkeypatch.setattr(crawler, "_discover_notes", fake_discover)

    result = asyncio.run(crawler._run_creator(creator_b, "collect"))
    assert result["status"] == RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value
    assert crawler.current_creator_id is None
    assert "66dabcde000000001f01abcd" not in crawler.structured_by_note


def test_exact_detail_state_overrides_weaker_response_record():
    note_id = "66dabcde000000001f01abcd"
    existing = {
        "note_id": note_id,
        "title": "weak title",
        "liked_count": "1",
        "tags": ["列表"],
    }
    incoming = {
        "note_id": note_id,
        "title": "exact title",
        "liked_count": "12",
        "share_count": "3",
        "tags": ["详情"],
    }
    merged = merge_public_note_records(existing, incoming, note_id, prefer_incoming=True, incoming_source="DETAIL_INITIAL_STATE")
    assert merged["title"] == "exact title"
    assert merged["liked_count"] == "12"
    assert merged["share_count"] == "3"
    assert merged["tags"] == ["列表", "详情"]
    assert merged["_structured_source"] == "DETAIL_INITIAL_STATE"
    assert merged["_field_sources"]["title"] == "DETAIL_INITIAL_STATE"
    assert merged["_field_sources"]["share_count"] == "DETAIL_INITIAL_STATE"


def test_later_partial_response_does_not_erase_existing_public_fields(tmp_path: Path):
    note_id = "66dabcde000000001f01abcd"
    crawler = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())
    crawler.current_run_id = "run"
    crawler.current_creator_id = "5cfb1f8e00000000100322e4"
    crawler._capture_structured(
        "run",
        "https://www.xiaohongshu.com/api/sns/web/v1/feed",
        {"id": note_id, "title": "完整", "interactInfo": {"likedCount": "11", "shareCount": "2"}, "tagList": [{"name": "A"}]},
    )
    crawler._capture_structured(
        "run",
        "https://www.xiaohongshu.com/api/sns/web/v1/search",
        {"id": note_id, "title": "标题-only"},
    )
    record = crawler.structured_by_note[note_id]
    assert record["title"] == "完整"
    assert record["liked_count"] == "11"
    assert record["share_count"] == "2"
    assert record["tags"] == ["A"]

    crawler2 = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())
    crawler2.current_run_id = "run"
    crawler2.current_creator_id = "5cfb1f8e00000000100322e4"
    crawler2._capture_structured("run", "https://www.xiaohongshu.com/api/a", {"noteId": note_id, "title": "标题"})
    crawler2._capture_structured("run", "https://www.xiaohongshu.com/api/b", {"noteId": note_id, "interactInfo": {"likedCount": "9"}})
    assert crawler2.structured_by_note[note_id]["title"] == "标题"
    assert crawler2.structured_by_note[note_id]["liked_count"] == "9"


def test_same_payload_note_records_merge_non_destructively():
    note_id = "66dabcde000000001f01abcd"
    records = extract_public_note_records(
        {
            "items": [
                {"noteId": note_id, "title": "完整", "likedCount": "11", "shareCount": "2", "tagList": [{"name": "A"}]},
                {"noteId": note_id, "title": "标题-only"},
            ]
        }
    )
    assert records[note_id]["title"] == "完整"
    assert records[note_id]["liked_count"] == "11"
    assert records[note_id]["share_count"] == "2"
    assert records[note_id]["tags"] == ["A"]


def test_same_payload_note_merge_is_order_independent():
    note_id = "66dabcde000000001f01abcd"
    rich = {"noteId": note_id, "title": "完整", "likedCount": "11", "shareCount": "2", "tagList": [{"name": "A"}]}
    sparse = {"noteId": note_id, "title": "标题-only"}
    first = extract_public_note_records({"items": [rich, sparse]})[note_id]
    second = extract_public_note_records({"items": [sparse, rich]})[note_id]
    assert first["title"] == second["title"] == "完整"
    assert first["liked_count"] == second["liked_count"] == "11"
    assert first["share_count"] == second["share_count"] == "2"
    assert first["tags"] == second["tags"] == ["A"]


def test_comment_id_not_classified_as_note():
    comment_id = "bbbbbbbbbbbbbbbbbbbbbbbb"
    assert extract_public_note_records({"id": comment_id, "content": "comment body", "time": 123, "likedCount": "3"}) == {}


def test_user_id_not_classified_as_note():
    user_id = "cccccccccccccccccccccccc"
    assert extract_public_note_records({"id": user_id, "nickname": "user", "desc": "bio"}) == {}


def test_explicit_note_id_is_classified():
    note_id = "66dabcde000000001f01abcd"
    records = extract_public_note_records({"noteId": note_id, "title": "T"})
    assert records[note_id]["title"] == "T"


def test_comment_with_parent_note_id_is_not_note():
    note_id = "66dabcde000000001f01abcd"
    comment_id = "bbbbbbbbbbbbbbbbbbbbbbbb"
    records = extract_public_note_records(
        {
            "comments": [
                {
                    "id": comment_id,
                    "note_id": note_id,
                    "content": "这是一条评论",
                    "create_time": 123456,
                    "like_count": "568",
                    "user": {"nickname": "u"},
                }
            ]
        }
    )
    assert note_id not in records
    assert comment_id not in records


def test_comment_with_parent_noteId_is_not_note():
    note_id = "66dabcde000000001f01abcd"
    records = extract_public_note_records({"noteId": note_id, "content": "relationship object", "likedCount": "9"})
    assert records == {}


def test_explicit_note_id_without_note_schema_is_rejected():
    note_id = "66dabcde000000001f01abcd"
    assert extract_public_note_records({"note_id": note_id, "time": 123, "likedCount": "5"}) == {}


def test_explicit_note_id_with_title_is_note():
    note_id = "66dabcde000000001f01abcd"
    records = extract_public_note_records({"note_id": note_id, "title": "标题"})
    assert records[note_id]["title"] == "标题"


def test_explicit_note_id_with_desc_is_note():
    note_id = "66dabcde000000001f01abcd"
    records = extract_public_note_records({"note_id": note_id, "desc": "正文"})
    assert records[note_id]["desc"] == "正文"


def test_generic_id_with_note_schema_is_classified():
    note_id = "66dabcde000000001f01abcd"
    records = extract_public_note_records({"id": note_id, "displayTitle": "T", "interactInfo": {"likedCount": "3"}})
    assert records[note_id]["display_title"] == "T"
    assert records[note_id]["liked_count"] == "3"


def test_structured_note_traversal_is_bounded(tmp_path: Path):
    crawler = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())
    crawler.current_run_id = "run"
    crawler.current_creator_id = "5cfb1f8e00000000100322e4"
    nested = current = {}
    for index in range(6000):
        current["next"] = {}
        current = current["next"]
        current["id"] = f"{index:024x}"
        current["nickname"] = "not-a-note"
    crawler._capture_structured("run", "https://www.xiaohongshu.com/api/large", nested)
    assert crawler.structured_by_note == {}


def test_profile_partial_response_does_not_erase_existing_fields():
    creator_id = "5cfb1f8e00000000100322e4"
    merged = merge_public_profile_records(
        {"user_id": creator_id, "nickname": "昵称", "followers": "1.2万", "bio": "公开简介", "ip_location": "浙江", "avatar_url": "https://img.example/a.jpg"},
        {"user_id": creator_id, "nickname": "新昵称"},
        creator_id,
        prefer_incoming=False,
    )
    assert merged["nickname"] == "昵称"
    assert merged["followers"] == "1.2万"
    assert merged["bio"] == "公开简介"
    assert merged["ip_location"] == "浙江"
    assert merged["avatar_url"] == "https://img.example/a.jpg"


def test_profile_sparse_then_rich_merges():
    creator_id = "5cfb1f8e00000000100322e4"
    record = extract_public_profile_record(
        {
            "items": [
                {"userId": creator_id, "nickname": "稀疏"},
                {"userId": creator_id, "nickname": "完整", "fans": "1.2万", "desc": "公开简介", "ipLocation": "浙江", "tags": [{"name": "A"}]},
            ]
        },
        creator_id,
    )
    assert record["nickname"] == "完整"
    assert record["followers"] == "1.2万"
    assert record["bio"] == "公开简介"
    assert record["ip_location"] == "浙江"
    assert record["tags"] == ["A"]


def test_profile_rich_then_sparse_same_result():
    creator_id = "5cfb1f8e00000000100322e4"
    rich = {"userId": creator_id, "nickname": "完整", "fans": "1.2万", "desc": "公开简介", "ipLocation": "浙江", "tags": [{"name": "A"}]}
    sparse = {"userId": creator_id, "nickname": "稀疏"}
    first = extract_public_profile_record({"items": [sparse, rich]}, creator_id)
    second = extract_public_profile_record({"items": [rich, sparse]}, creator_id)
    assert first == second
    assert first["nickname"] == "完整"


def test_profile_different_creator_never_merges():
    creator_id = "5cfb1f8e00000000100322e4"
    record = extract_public_profile_record(
        {
            "items": [
                {"userId": "aaaaaaaaaaaaaaaaaaaaaaaa", "nickname": "其他", "fans": "99万"},
                {"userId": creator_id, "nickname": "目标"},
            ]
        },
        creator_id,
    )
    assert record == {"user_id": creator_id, "nickname": "目标"}


def test_different_creator_profile_records_never_merge():
    creator_id = "5cfb1f8e00000000100322e4"
    merged = merge_public_profile_records(
        {"user_id": creator_id, "nickname": "A", "followers": "10"},
        {"user_id": "aaaaaaaaaaaaaaaaaaaaaaaa", "nickname": "B", "followers": "99"},
        creator_id,
        prefer_incoming=True,
    )
    assert merged == {"user_id": creator_id, "nickname": "A", "followers": "10"}


def test_missing_profile_field_reason_is_not_observed():
    summary = summarize_profile_fields(
        {
            "nickname": "昵称",
            "canonical_url": "https://www.xiaohongshu.com/user/profile/5cfb1f8e00000000100322e4",
            "field_sources": {"nickname": "DOM"},
        },
        {"user_id": "5cfb1f8e00000000100322e4", "nickname": "昵称"},
    )
    assert summary["following"]["present"] is False
    assert summary["following"]["reason"] == "NOT_OBSERVED"
    assert summary["profile_tags"]["reason"] == "NOT_OBSERVED"


def test_field_level_structured_provenance():
    note_id = "66dabcde000000001f01abcd"
    record = merge_public_note_records(
        {"note_id": note_id, "title": "T", "liked_count": "1"},
        {"note_id": note_id, "share_count": "9"},
        note_id,
        prefer_incoming=False,
        incoming_source="PAGE_RESPONSE",
    )
    record = merge_public_note_records(record, {"note_id": note_id, "liked_count": "2"}, note_id, prefer_incoming=True, incoming_source="DETAIL_INITIAL_STATE")
    assert record["_field_sources"]["title"] == "PAGE_RESPONSE"
    assert record["_field_sources"]["like_count"] == "DETAIL_INITIAL_STATE"
    assert record["_field_sources"]["share_count"] == "PAGE_RESPONSE"


def test_weaker_page_response_cannot_override_detail_initial_state():
    note_id = "66dabcde000000001f01abcd"
    record = merge_public_note_records(
        None,
        {"note_id": note_id, "title": "detail-title", "liked_count": "100"},
        note_id,
        prefer_incoming=True,
        incoming_source="DETAIL_INITIAL_STATE",
    )
    record = merge_public_note_records(
        record,
        {"note_id": note_id, "title": "page-title", "liked_count": "99", "share_count": "3", "tags": ["A"]},
        note_id,
        prefer_incoming=False,
        incoming_source="PAGE_RESPONSE",
    )
    assert record["title"] == "detail-title"
    assert record["liked_count"] == "100"
    assert record["share_count"] == "3"
    assert record["tags"] == ["A"]
    assert record["_field_sources"]["title"] == "DETAIL_INITIAL_STATE"
    assert record["_field_sources"]["like_count"] == "DETAIL_INITIAL_STATE"
    assert record["_field_sources"]["share_count"] == "PAGE_RESPONSE"


def test_stronger_detail_initial_state_overrides_page_response():
    note_id = "66dabcde000000001f01abcd"
    record = merge_public_note_records(
        None,
        {"note_id": note_id, "title": "page-title", "liked_count": "90", "share_count": "3"},
        note_id,
        prefer_incoming=False,
        incoming_source="PAGE_RESPONSE",
    )
    record = merge_public_note_records(
        record,
        {"note_id": note_id, "title": "detail-title", "liked_count": "100"},
        note_id,
        prefer_incoming=True,
        incoming_source="DETAIL_INITIAL_STATE",
    )
    assert record["title"] == "detail-title"
    assert record["liked_count"] == "100"
    assert record["share_count"] == "3"
    assert record["_field_sources"]["title"] == "DETAIL_INITIAL_STATE"
    assert record["_field_sources"]["like_count"] == "DETAIL_INITIAL_STATE"
    assert record["_field_sources"]["share_count"] == "PAGE_RESPONSE"


def test_structured_field_source_normalization_is_idempotent():
    sources = {
        "title": "DETAIL_INITIAL_STATE",
        "body": "PAGE_RESPONSE",
        "note_type": "PAGE_RESPONSE",
        "publish_time": "DETAIL_INITIAL_STATE",
        "like_count": "PAGE_RESPONSE",
        "collect_count": "PAGE_RESPONSE",
        "comment_count": "PAGE_RESPONSE",
        "share_count": "PAGE_RESPONSE",
        "tags": "PAGE_RESPONSE+DETAIL_INITIAL_STATE",
    }
    once = _normalize_structured_field_sources(sources)
    twice = _normalize_structured_field_sources(once)
    assert once == twice == sources


def test_body_provenance_survives_multiple_merges():
    note_id = "66dabcde000000001f01abcd"
    record = merge_public_note_records(None, {"note_id": note_id, "content": "正文"}, note_id, prefer_incoming=False, incoming_source="PAGE_RESPONSE")
    assert record["content"] == "正文"
    assert record["_field_sources"]["body"] == "PAGE_RESPONSE"
    record = merge_public_note_records(record, {"note_id": note_id, "share_count": "3"}, note_id, prefer_incoming=False, incoming_source="PAGE_RESPONSE")
    assert record["content"] == "正文"
    assert record["_field_sources"]["body"] == "PAGE_RESPONSE"
    record = merge_public_note_records(record, {"note_id": note_id, "desc": "详情正文"}, note_id, prefer_incoming=True, incoming_source="DETAIL_INITIAL_STATE")
    assert record["desc"] == "详情正文"
    assert record["_field_sources"]["body"] == "DETAIL_INITIAL_STATE"


def test_tag_provenance_merges_deterministically():
    note_id = "66dabcde000000001f01abcd"
    record = merge_public_note_records(None, {"note_id": note_id, "tags": ["A"]}, note_id, prefer_incoming=False, incoming_source="PAGE_RESPONSE")
    record = merge_public_note_records(record, {"note_id": note_id, "tags": ["B"]}, note_id, prefer_incoming=True, incoming_source="DETAIL_INITIAL_STATE")
    assert record["tags"] == ["A", "B"]
    assert record["_field_sources"]["tags"] == "PAGE_RESPONSE+DETAIL_INITIAL_STATE"


def test_equal_tags_upgrade_or_merge_provenance():
    note_id = "66dabcde000000001f01abcd"
    record = merge_public_note_records(None, {"note_id": note_id, "tags": ["A"]}, note_id, prefer_incoming=False, incoming_source="PAGE_RESPONSE")
    record = merge_public_note_records(record, {"note_id": note_id, "tags": ["A"]}, note_id, prefer_incoming=True, incoming_source="DETAIL_INITIAL_STATE")
    assert record["tags"] == ["A"]
    assert record["_field_sources"]["tags"] == "PAGE_RESPONSE+DETAIL_INITIAL_STATE"


def test_canonical_field_provenance_for_content_body():
    note_id = "66dabcde000000001f01abcd"
    record = merge_public_note_records(None, {"note_id": note_id, "content": "正文"}, note_id, prefer_incoming=False, incoming_source="PAGE_RESPONSE")
    assert record["_field_sources"]["body"] == "PAGE_RESPONSE"
    assert "content" not in record["_field_sources"]


def test_canonical_field_provenance_for_display_title():
    note_id = "66dabcde000000001f01abcd"
    record = merge_public_note_records(None, {"note_id": note_id, "display_title": "标题"}, note_id, prefer_incoming=False, incoming_source="PAGE_RESPONSE")
    assert record["_field_sources"]["title"] == "PAGE_RESPONSE"
    assert "display_title" not in record["_field_sources"]


def test_canonical_field_provenance_for_type_alias():
    note_id = "66dabcde000000001f01abcd"
    record = merge_public_note_records(None, {"note_id": note_id, "model_type": "video"}, note_id, prefer_incoming=True, incoming_source="DETAIL_INITIAL_STATE")
    assert record["_field_sources"]["note_type"] == "DETAIL_INITIAL_STATE"
    assert "model_type" not in record["_field_sources"]


def test_canonical_field_provenance_for_time_alias():
    note_id = "66dabcde000000001f01abcd"
    record = merge_public_note_records(None, {"note_id": note_id, "time": 1234567890}, note_id, prefer_incoming=False, incoming_source="PAGE_RESPONSE")
    assert record["_field_sources"]["publish_time"] == "PAGE_RESPONSE"
    assert "time" not in record["_field_sources"]


def test_canonical_metric_provenance():
    note_id = "66dabcde000000001f01abcd"
    record = merge_public_note_records(
        None,
        {"note_id": note_id, "liked_count": "1", "collected_count": "2", "comment_count": "3", "share_count": "4"},
        note_id,
        prefer_incoming=False,
        incoming_source="PAGE_RESPONSE",
    )
    assert record["_field_sources"]["like_count"] == "PAGE_RESPONSE"
    assert record["_field_sources"]["collect_count"] == "PAGE_RESPONSE"
    assert record["_field_sources"]["comment_count"] == "PAGE_RESPONSE"
    assert record["_field_sources"]["share_count"] == "PAGE_RESPONSE"


def test_comment_response_path_skips_structured_extraction(tmp_path: Path):
    note_id = "66dabcde000000001f01abcd"
    crawler = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())
    crawler.current_run_id = "run"
    crawler.current_creator_id = "5cfb1f8e00000000100322e4"
    crawler._capture_structured(
        "run",
        "https://edith.xiaohongshu.com/api/sns/web/v2/comment/page?xsec_token=SECRET",
        {"noteId": note_id, "title": "should-skip"},
    )
    assert is_comment_response_path("https://edith.xiaohongshu.com/api/sns/web/v2/comment/sub/page?x=1")
    assert crawler.structured_by_note == {}
    assert crawler.structured_profile is None


def _checkpoint(tmp_path: Path):
    from xhs_profile_exporter.checkpoint import Checkpoint

    return Checkpoint(run_id="run", creator_id="creator")


async def _noop_async():
    return None


def test_cover_selection_prefers_visible_profile_cover():
    note_id = "66dabcde000000001f01abcd"
    candidates = [
        {
            "visible": False,
            "width": 0,
            "height": 0,
            "is_card_ancestor": True,
            "is_cover_class": False,
            "href_path_pattern": "/explore_or_discovery/{note_id}",
        },
        {
            "visible": True,
            "width": 240,
            "height": 320,
            "is_card_ancestor": True,
            "is_cover_class": True,
            "href_path_pattern": "/user/profile/{creator_id}/{note_id}",
        },
        {
            "visible": True,
            "width": 300,
            "height": 300,
            "is_card_ancestor": False,
            "is_cover_class": True,
            "href_path_pattern": "other_note_href",
        },
    ]
    selected = select_visible_note_cover_candidate(candidates)
    assert selected is candidates[1]


def test_route_match_and_summary_are_strict_and_sanitized():
    note_id = "66dabcde000000001f01abcd"
    assert route_matches_note(f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=secret&a=private", note_id)
    assert not route_matches_note("https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa", note_id)
    assert safe_route_summary(f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=secret&a=private") == f"/explore/{note_id}?keys=a"
    assert "secret" not in safe_route_summary(f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=secret")


def test_scan_profile_requeries_until_target_mounts(tmp_path: Path, monkeypatch):
    crawler = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())
    calls = []

    class ScanPage:
        async def evaluate(self, script):
            calls.append("scroll")

        async def wait_for_timeout(self, timeout):
            pass

        def locator(self, selector):
            return BodyLocator("")

    class BodyLocator:
        def __init__(self, text):
            self.text = text

        async def inner_text(self, timeout=0):
            return self.text

    async def fake_find(page, creator_id, note_id):
        calls.append("find")
        if calls.count("find") == 1:
            return {"locator": None, "cover_candidates": 0, "href_path_pattern": None}
        return {"locator": object(), "cover_candidates": 1, "href_path_pattern": "/user/profile/{creator_id}/{note_id}"}

    async def no_safe_stop(*args, **kwargs):
        return None

    monkeypatch.setattr(crawler, "_find_visible_note_cover", fake_find)
    monkeypatch.setattr(crawler, "_raise_if_safe_stop", no_safe_stop)
    result = asyncio.run(
        crawler._scan_profile_for_visible_note_cover(
            ScanPage(),
            "5cfb1f8e00000000100322e4",
            "66dabcde000000001f01abcd",
            crawler._build_budget(),
        )
    )
    assert result["locator"] is not None
    assert calls[:3] == ["find", "scroll", "find"]


def test_target_detail_gate_requires_exact_route_and_detail_root(tmp_path: Path, monkeypatch):
    crawler = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())
    note_id = "66dabcde000000001f01abcd"

    class DetailPage:
        def __init__(self, url):
            self.url = url

        async def wait_for_timeout(self, timeout):
            pass

    async def no_safe_stop(*args, **kwargs):
        return None

    monkeypatch.setattr(crawler, "_raise_if_safe_stop", no_safe_stop)
    monkeypatch.setattr(crawler, "_get_note_detail_evidence", lambda page, note_id: _return_async({"verified": True, "detail_root_count": 1}))
    verified, reason, detail_count = asyncio.run(crawler._wait_for_target_note_detail(DetailPage(f"https://www.xiaohongshu.com/explore/{note_id}"), note_id, timeout_ms=100))
    assert verified is True
    assert reason is None
    assert detail_count == 1

    verified, reason, _ = asyncio.run(crawler._wait_for_target_note_detail(DetailPage("https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa"), note_id, timeout_ms=100))
    assert verified is False
    assert reason == "TARGET_MISMATCH"


def test_generic_main_only_is_not_verified(tmp_path: Path, monkeypatch):
    crawler = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())
    note_id = "66dabcde000000001f01abcd"

    class DetailPage:
        url = f"https://www.xiaohongshu.com/explore/{note_id}"

        async def wait_for_timeout(self, timeout):
            pass

    async def no_safe_stop(*args, **kwargs):
        return None

    monkeypatch.setattr(crawler, "_raise_if_safe_stop", no_safe_stop)
    monkeypatch.setattr(crawler, "_get_note_detail_evidence", lambda page, note_id: _return_async({"verified": False, "detail_root_count": 0}))
    verified, reason, detail_count = asyncio.run(crawler._wait_for_target_note_detail(DetailPage(), note_id, timeout_ms=100))
    assert verified is False
    assert reason == "DETAIL_NOT_READY"
    assert detail_count == 0


def test_unavailable_shell_is_not_verified(tmp_path: Path, monkeypatch):
    crawler = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())
    note_id = "66dabcde000000001f01abcd"

    class DetailPage:
        url = f"https://www.xiaohongshu.com/explore/{note_id}"

        async def wait_for_timeout(self, timeout):
            pass

    async def no_safe_stop(*args, **kwargs):
        return None

    monkeypatch.setattr(crawler, "_raise_if_safe_stop", no_safe_stop)
    monkeypatch.setattr(crawler, "_get_note_detail_evidence", lambda page, note_id: _return_async({"verified": False, "unavailable": True, "detail_root_count": 0}))
    verified, reason, _ = asyncio.run(crawler._wait_for_target_note_detail(DetailPage(), note_id, timeout_ms=100))
    assert verified is False
    assert reason == "DETAIL_NOT_READY"


def test_navigation_failures_stop_after_budget(tmp_path: Path, monkeypatch):
    db = DummyDb()
    crawler = Crawler(app_config(tmp_path), db, DummyLogger())
    page = FakePage()
    creator = app_config(tmp_path).creators[0]
    opened = []

    async def fake_open(page, profile_url, card, budget):
        opened.append(card["note_id"])
        return OpenNoteResult(page=page, note_id=card["note_id"], strategy="visible_cover_only", target_verified=False, reason="TARGET_NOT_VERIFIED")

    monkeypatch.setattr(crawler, "_open_note_from_profile", fake_open)
    monkeypatch.setattr(crawler, "_save_error_screenshot", lambda *args, **kwargs: _noop_async())
    cards = [{"note_id": f"{idx:024x}"} for idx in range(4)]
    result = asyncio.run(crawler._collect_notes(page, creator, cards, _checkpoint(tmp_path), crawler._build_budget()))
    assert len(result.navigation_failed_ids) == 3
    assert result.safe_stop_reason == "MAX_CONSECUTIVE_ERRORS"
    assert opened == [cards[0]["note_id"], cards[1]["note_id"], cards[2]["note_id"]]
    checkpoint_path = next((tmp_path / "data" / "checkpoints").glob("*_creator.json"))
    assert "MAX_CONSECUTIVE_ERRORS" in checkpoint_path.read_text(encoding="utf-8")


def test_run_result_and_db_include_collect_safe_stop_reason(tmp_path: Path, monkeypatch):
    db = DummyDb()
    crawler = Crawler(app_config(tmp_path), db, DummyLogger())

    async def fake_profile(*args, **kwargs):
        return {"captured_at": "now", "nickname": "n", "canonical_url": app_config(tmp_path).creators[0].url, "source": "test"}

    async def fake_discover(*args, **kwargs):
        return [{"note_id": f"{idx:024x}"} for idx in range(3)]

    async def fake_collect(*args, **kwargs):
        return CollectionResult(
            attempted_ids=[f"{idx:024x}" for idx in range(3)],
            navigation_failed_ids=[f"{idx:024x}" for idx in range(3)],
            safe_stop_reason="MAX_CONSECUTIVE_ERRORS",
        )

    monkeypatch.setattr(crawler_module, "BrowserSession", FakeBrowserSession)
    monkeypatch.setattr(crawler_module, "extract_profile_dom", fake_profile)
    monkeypatch.setattr(crawler, "_discover_notes", fake_discover)
    monkeypatch.setattr(crawler, "_collect_notes", fake_collect)
    monkeypatch.setattr(crawler_module, "run_offline_qa", lambda *args, **kwargs: {"passed": True})
    monkeypatch.setattr(crawler_module, "export_excel", lambda *args, **kwargs: tmp_path / "out.xlsx")

    result = asyncio.run(crawler._run_creator(app_config(tmp_path).creators[0], "collect"))
    assert result["status"] == RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value
    assert result["safe_stop_reason"] == "MAX_CONSECUTIVE_ERRORS"
    assert db.finished["safe_stop_reason"] == "MAX_CONSECUTIVE_ERRORS"
    assert db.finished["notes"]["safe_stop_reason"] == "MAX_CONSECUTIVE_ERRORS"


def test_collect_safe_stop_returns_partial_stats(tmp_path: Path, monkeypatch):
    db = DummyDb()
    crawler = Crawler(app_config(tmp_path), db, DummyLogger())
    page = FakePage()
    creator = app_config(tmp_path).creators[0]
    cards = [{"note_id": "66dabcde000000001f01abcd"}, {"note_id": "aaaaaaaaaaaaaaaaaaaaaaaa"}]

    async def fake_open(page, profile_url, card, budget):
        return OpenNoteResult(page=page, note_id=card["note_id"], strategy="test", target_verified=True)

    async def fake_extract(page, note_id, top_n):
        return {"note_id": note_id, "status": "OK", "title": "ok", "top_comments": [], "raw_json": {}}

    calls = 0

    async def fake_return(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SafeStopRequested(LoginStatus.RISK_CONTROL_DETECTED, "RISK_CONTROL_DETECTED", "profile_return_after_back", cards[0]["note_id"])
        return {"strategy": "ok", "profile_restored": True, "route_after": "/user/profile/x"}

    monkeypatch.setattr(crawler, "_open_note_from_profile", fake_open)
    monkeypatch.setattr(crawler, "_extract_initial_state_note_record", lambda *args, **kwargs: _return_async(None))
    monkeypatch.setattr(crawler_module, "extract_note_dom", fake_extract)
    monkeypatch.setattr(crawler, "_return_to_creator_profile", fake_return)
    monkeypatch.setattr(crawler_module, "browser_flush_if_available", lambda *args, **kwargs: _noop_async())
    result = asyncio.run(crawler._collect_notes(page, creator, cards, _checkpoint(tmp_path), crawler._build_budget()))
    assert result.safe_stop_reason == "RISK_CONTROL_DETECTED"
    assert result.safe_stop_status == LoginStatus.RISK_CONTROL_DETECTED
    assert result.exportable_ids == [cards[0]["note_id"]]
    assert result.attempted_ids == [cards[0]["note_id"]]


def test_risk_checkpoint_preserves_completed_and_pending_note(tmp_path: Path, monkeypatch):
    db = DummyDb()
    crawler = Crawler(app_config(tmp_path), db, DummyLogger())
    page = FakePage()
    creator = app_config(tmp_path).creators[0]
    completed_note = "66dabcde000000001f01abcd"
    risk_note = "aaaaaaaaaaaaaaaaaaaaaaaa"
    cards = [{"note_id": completed_note}, {"note_id": risk_note}]
    opened = []

    async def fake_open(page, profile_url, card, budget):
        opened.append(card["note_id"])
        if card["note_id"] == risk_note:
            raise SafeStopRequested(LoginStatus.RISK_CONTROL_DETECTED, "RISK_CONTROL_DETECTED", "note_after_open", risk_note)
        return OpenNoteResult(page=page, note_id=card["note_id"], strategy="test", target_verified=True)

    async def fake_extract(page, note_id, top_n):
        return {"note_id": note_id, "status": "OK", "title": "ok", "top_comments": [], "raw_json": {}}

    monkeypatch.setattr(crawler, "_open_note_from_profile", fake_open)
    monkeypatch.setattr(crawler, "_extract_initial_state_note_record", lambda *args, **kwargs: _return_async(None))
    monkeypatch.setattr(crawler_module, "extract_note_dom", fake_extract)
    monkeypatch.setattr(crawler, "_return_to_creator_profile", lambda *args, **kwargs: _return_async({"strategy": "ok", "profile_restored": True, "route_after": "/user/profile/x"}))
    monkeypatch.setattr(crawler_module, "browser_flush_if_available", lambda *args, **kwargs: _noop_async())

    checkpoint = _checkpoint(tmp_path)
    result = asyncio.run(crawler._collect_notes(page, creator, cards, checkpoint, crawler._build_budget()))
    assert result.safe_stop_reason == "RISK_CONTROL_DETECTED"
    assert checkpoint.completed_note_ids == [completed_note]
    assert checkpoint.current_note_id == risk_note
    assert risk_note not in checkpoint.completed_note_ids
    assert opened == [completed_note, risk_note]


def test_risk_safe_stop_never_auto_retries(tmp_path: Path, monkeypatch):
    db = DummyDb()
    crawler = Crawler(app_config(tmp_path), db, DummyLogger())
    page = FakePage()
    risk_note = "66dabcde000000001f01abcd"
    attempts = 0

    async def fake_open(page, profile_url, card, budget):
        nonlocal attempts
        attempts += 1
        raise SafeStopRequested(LoginStatus.RISK_CONTROL_DETECTED, "RISK_CONTROL_DETECTED", "note_after_open", risk_note)

    monkeypatch.setattr(crawler, "_open_note_from_profile", fake_open)
    result = asyncio.run(
        crawler._collect_notes(
            page,
            app_config(tmp_path).creators[0],
            [{"note_id": risk_note}],
            _checkpoint(tmp_path),
            crawler._build_budget(),
        )
    )
    assert result.safe_stop_reason == "RISK_CONTROL_DETECTED"
    assert attempts == 1


def test_completed_run_ignores_late_structured_callback(tmp_path: Path, monkeypatch):
    db = DummyDb()
    crawler = Crawler(app_config(tmp_path), db, DummyLogger())
    note_id = "66dabcde000000001f01abcd"

    async def fake_profile(*args, **kwargs):
        return {"captured_at": "now", "nickname": "n", "canonical_url": app_config(tmp_path).creators[0].url, "source": "test"}

    async def fake_discover(*args, **kwargs):
        return []

    async def fake_collect(*args, **kwargs):
        return CollectionResult()

    monkeypatch.setattr(crawler_module, "BrowserSession", FakeBrowserSession)
    monkeypatch.setattr(crawler_module, "extract_profile_dom", fake_profile)
    monkeypatch.setattr(crawler, "_extract_initial_state_profile_record", lambda *args, **kwargs: _return_async(None))
    monkeypatch.setattr(crawler, "_discover_notes", fake_discover)
    monkeypatch.setattr(crawler, "_collect_notes", fake_collect)
    monkeypatch.setattr(crawler_module, "run_offline_qa", lambda *args, **kwargs: {"passed": True})
    monkeypatch.setattr(crawler_module, "export_excel", lambda *args, **kwargs: tmp_path / "out.xlsx")

    result = asyncio.run(crawler._run_creator(app_config(tmp_path).creators[0], "collect"))
    assert result["status"] == RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value
    crawler._capture_structured("stale-run", "https://www.xiaohongshu.com/api/a", {"noteId": note_id, "title": "late"})
    assert crawler.current_run_id is None
    assert crawler.current_creator_id is None
    assert crawler.structured_by_note == {}
    assert crawler.structured_profile is None


def test_extract_initial_state_note_record_uses_exact_note_detail_map(tmp_path: Path):
    async def run():
        note_id = "66dabcde000000001f01abcd"
        crawler = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content("<html></html>")
            await page.evaluate(
                """
                ([noteId]) => {
                  window.__INITIAL_STATE__ = {
                    note: {
                      noteDetailMap: {
                        [noteId]: {
                          note: {
                            id: noteId,
                            title: "T",
                            desc: "B",
                            type: "normal",
                            time: 1234567890000,
                            interactInfo: {likedCount: "123", collectedCount: "45", commentCount: "6", shareCount: "7"},
                            tagList: [{name: "杭州"}, {name: "旅行"}],
                            privatePayload: {x: 1}
                          }
                        }
                      }
                    }
                  };
                }
                """,
                [note_id],
            )
            record = await crawler._extract_initial_state_note_record(page, note_id)
            await browser.close()
            return record

    record = asyncio.run(run())
    assert record["liked_count"] == "123"
    assert record["collected_count"] == "45"
    assert record["comment_count"] == "6"
    assert record["share_count"] == "7"
    assert record["tags"] == ["旅行", "杭州"]
    assert "privatePayload" not in str(record)


def test_initial_state_fallback_rejects_comment_note_reference(tmp_path: Path):
    async def run():
        note_id = "66dabcde000000001f01abcd"
        crawler = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content("<html></html>")
            await page.evaluate(
                """
                ([noteId]) => {
                  window.__INITIAL_STATE__ = {
                    comments: {
                      items: [
                        {
                          id: "bbbbbbbbbbbbbbbbbbbbbbbb",
                          note_id: noteId,
                          content: "comment",
                          like_count: "99"
                        }
                      ]
                    }
                  };
                }
                """,
                [note_id],
            )
            record = await crawler._extract_initial_state_note_record(page, note_id)
            await browser.close()
            return record

    assert asyncio.run(run()) is None


def test_initial_state_fallback_accepts_real_note_schema(tmp_path: Path):
    async def run():
        note_id = "66dabcde000000001f01abcd"
        crawler = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content("<html></html>")
            await page.evaluate(
                """
                ([noteId]) => {
                  window.__INITIAL_STATE__ = {
                    someOtherPlace: {
                      item: {
                        noteId,
                        title: "T",
                        interactInfo: {likedCount: "10"}
                      }
                    }
                  };
                }
                """,
                [note_id],
            )
            record = await crawler._extract_initial_state_note_record(page, note_id)
            await browser.close()
            return record

    record = asyncio.run(run())
    assert record["note_id"] == "66dabcde000000001f01abcd"
    assert record["title"] == "T"
    assert record["liked_count"] == "10"


def test_extract_initial_state_profile_record_uses_user_page_data(tmp_path: Path):
    async def run():
        creator_id = "5cfb1f8e00000000100322e4"
        crawler = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content("<html></html>")
            await page.evaluate(
                """
                ([creatorId]) => {
                  window.__INITIAL_STATE__ = {
                    user: {
                      userPageData: {
                        userId: creatorId,
                        nickname: "昵称",
                        desc: "公开简介",
                        gender: 1,
                        ipLocation: "浙江",
                        follows: "123",
                        fans: "1.2万",
                        interaction: "8.5万",
                        tags: [{name: "健康"}],
                        privatePayload: {x: 1}
                      }
                    }
                  };
                }
                """,
                [creator_id],
            )
            record = await crawler._extract_initial_state_profile_record(page, creator_id)
            await browser.close()
            return record

    record = asyncio.run(run())
    assert record["user_id"] == "5cfb1f8e00000000100322e4"
    assert record["following"] == "123"
    assert record["followers"] == "1.2万"
    assert record["interactions"] == "8.5万"
    assert record["tags"] == ["健康"]
    assert "privatePayload" not in str(record)


def test_navigation_failure_counter_resets_after_success(tmp_path: Path, monkeypatch):
    db = DummyDb()
    crawler = Crawler(app_config(tmp_path), db, DummyLogger())
    page = FakePage()
    creator = app_config(tmp_path).creators[0]
    cards = [{"note_id": f"{idx:024x}"} for idx in range(5)]
    opened = []

    async def fake_open(page, profile_url, card, budget):
        opened.append(card["note_id"])
        verified = len(opened) in {3, 4}
        return OpenNoteResult(page=page, note_id=card["note_id"], strategy="test", target_verified=verified, reason=None if verified else "TARGET_NOT_VERIFIED")

    async def fake_extract(page, note_id, top_n):
        return {"note_id": note_id, "status": "OK", "title": "ok", "top_comments": [], "raw_json": {}}

    monkeypatch.setattr(crawler, "_open_note_from_profile", fake_open)
    monkeypatch.setattr(crawler, "_extract_initial_state_note_record", lambda *args, **kwargs: _return_async(None))
    monkeypatch.setattr(crawler_module, "extract_note_dom", fake_extract)
    monkeypatch.setattr(crawler, "_return_to_creator_profile", lambda *args, **kwargs: _return_async({"strategy": "ok", "profile_restored": True, "route_after": "/user/profile/x"}))
    monkeypatch.setattr(crawler, "_save_error_screenshot", lambda *args, **kwargs: _noop_async())
    monkeypatch.setattr(crawler_module, "browser_flush_if_available", lambda *args, **kwargs: _noop_async())
    result = asyncio.run(crawler._collect_notes(page, creator, cards, _checkpoint(tmp_path), crawler._build_budget()))
    assert len(result.exportable_ids) == 2
    assert len(opened) == 5


def test_run_status_marks_navigation_unresolved_partial():
    result = CollectionResult(attempted_ids=["1", "2"], exportable_ids=["1"], navigation_failed_ids=["2"])
    assert determine_run_status("collect", result) == RunStatus.PARTIAL_SUCCESS_SAFE_STOP.value
    smoke = CollectionResult(attempted_ids=["1", "2", "3"], exportable_ids=["1", "2", "3"])
    assert determine_run_status("smoke", smoke, 3) == RunStatus.SUCCESS.value


def test_return_to_profile_uses_history_success(tmp_path: Path, monkeypatch):
    crawler = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())

    class CountLocator:
        async def count(self):
            return 1

    class ReturnPage:
        def __init__(self):
            self.url = "https://www.xiaohongshu.com/explore/66dabcde000000001f01abcd"

        async def go_back(self, wait_until=None, timeout=0):
            self.url = "https://www.xiaohongshu.com/user/profile/5cfb1f8e00000000100322e4"

        async def wait_for_timeout(self, timeout):
            pass

        def locator(self, selector):
            return CountLocator()

    async def no_safe_stop(*args, **kwargs):
        return None

    monkeypatch.setattr(crawler, "_raise_if_safe_stop", no_safe_stop)
    result = asyncio.run(crawler._return_to_creator_profile(ReturnPage(), app_config(tmp_path).creators[0].url, "66dabcde000000001f01abcd"))
    assert result["strategy"] == "PROFILE_RETURN_HISTORY_SUCCESS"
    assert result["profile_restored"] is True


def test_safe_stop_during_cover_click_is_not_swallowed(tmp_path: Path, monkeypatch):
    crawler = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())

    class Locator:
        async def scroll_into_view_if_needed(self, timeout=0):
            pass

        async def click(self, timeout=0, no_wait_after=False):
            pass

    async def fake_find(*args, **kwargs):
        return {"locator": Locator(), "cover_candidates": 1, "href_path_pattern": "/user/profile/{creator_id}/{note_id}"}

    async def safe_stop(*args, **kwargs):
        raise SafeStopRequested(LoginStatus.RISK_CONTROL_DETECTED, "RISK_CONTROL_DETECTED", "note_wait_target_detail")

    monkeypatch.setattr(crawler, "_find_visible_note_cover", fake_find)
    monkeypatch.setattr(crawler, "_wait_for_target_note_detail", safe_stop)

    with pytest.raises(SafeStopRequested):
        asyncio.run(
            crawler._click_visible_note_cover_on_current_page(
                FakePage(),
                "5cfb1f8e00000000100322e4",
                "66dabcde000000001f01abcd",
                crawler._build_budget(),
                "current_mounted_cover_click",
            )
        )


def test_safe_stop_during_profile_return_is_not_swallowed(tmp_path: Path, monkeypatch):
    crawler = Crawler(app_config(tmp_path), DummyDb(), DummyLogger())

    class ReturnPage:
        url = "https://www.xiaohongshu.com/user/profile/5cfb1f8e00000000100322e4"

        async def go_back(self, wait_until=None, timeout=0):
            pass

        async def wait_for_timeout(self, timeout):
            pass

    async def safe_stop(*args, **kwargs):
        raise SafeStopRequested(LoginStatus.HUMAN_VERIFICATION_REQUIRED, "HUMAN_VERIFICATION_REQUIRED", "profile_return_after_back")

    monkeypatch.setattr(crawler, "_raise_if_safe_stop", safe_stop)

    with pytest.raises(SafeStopRequested):
        asyncio.run(
            crawler._return_to_creator_profile(
                ReturnPage(),
                app_config(tmp_path).creators[0].url,
                "66dabcde000000001f01abcd",
            )
        )


async def _return_async(value):
    return value

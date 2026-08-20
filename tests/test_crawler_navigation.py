import asyncio
from pathlib import Path

import pytest

import xhs_profile_exporter.crawler as crawler_module
from xhs_profile_exporter.config import AppConfig, CreatorConfig
from xhs_profile_exporter.crawler import (
    Crawler,
    route_matches_note,
    safe_route_summary,
    select_visible_note_cover_candidate,
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
    assert result.non_exportable_ids == ["66dabcde000000001f01abcd"]
    assert db.saved_notes == []


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
    monkeypatch.setattr(crawler, "_visible_detail_root_count", lambda page: _return_async(1))
    verified, reason, detail_count = asyncio.run(crawler._wait_for_target_note_detail(DetailPage(f"https://www.xiaohongshu.com/explore/{note_id}"), note_id, timeout_ms=100))
    assert verified is True
    assert reason is None
    assert detail_count == 1

    verified, reason, _ = asyncio.run(crawler._wait_for_target_note_detail(DetailPage("https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa"), note_id, timeout_ms=100))
    assert verified is False
    assert reason == "TARGET_MISMATCH"


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

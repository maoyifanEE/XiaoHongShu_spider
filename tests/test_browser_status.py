import pytest
import asyncio

from xhs_profile_exporter.browser import BrowserSession, detect_session_indicators
from xhs_profile_exporter.state import LoginStatus


class BodyLocator:
    def __init__(self, text: str):
        self.text = text

    async def inner_text(self, timeout=0):
        return self.text


class FakePage:
    def __init__(self, *, text="", session=None, profile=None):
        self.text = text
        self.session = session or {"ready": False, "loginInputs": 0, "loginText": [], "sessionWords": [], "accountControls": 0}
        self.profile = profile or {"ready": False, "textLength": len(text), "loginInputs": 0, "noteLinks": 0}
        self.url = "https://www.xiaohongshu.com/"
        self.goto_count = 0

    async def goto(self, url, wait_until=None):
        self.goto_count += 1
        self.url = url

    async def wait_for_timeout(self, timeout):
        return None

    def locator(self, selector):
        return BodyLocator(self.text)

    async def evaluate(self, script):
        if "accountControls" in script:
            return self.session
        return self.profile


class CaptureLogger:
    def __init__(self):
        self.messages = []

    def info(self, *args, **kwargs):
        self.messages.append(args)


class FakeResponse:
    url = "https://www.xiaohongshu.com/api/test"
    headers = {"content-type": "application/json"}

    async def json(self):
        return {"ok": True}


def test_unknown_page_is_not_login_ok():
    session = BrowserSession(__import__("pathlib").Path("."), {"browser": {"login_timeout_minutes": 0}}, __import__("logging").getLogger("test"))
    page = FakePage(text="loading", session={"ready": False, "loginInputs": 0, "loginText": [], "sessionWords": [], "accountControls": 0})
    assert asyncio.run(session.inspect_login_state(page)) == LoginStatus.PAGE_NOT_READY


def test_creator_avatar_alone_cannot_prove_login():
    page = FakePage(
        text="小红书号 关注 粉丝 笔记",
        profile={"hasProfileWords": True, "hasNoteArea": True, "ready": True, "textLength": 100, "loginInputs": 0, "noteLinks": 10},
        session={"ready": False, "loginInputs": 0, "loginText": [], "sessionWords": [], "accountControls": 0},
    )
    session = BrowserSession(__import__("pathlib").Path("."), {"browser": {"login_timeout_minutes": 0}}, __import__("logging").getLogger("test"))
    assert asyncio.run(session.inspect_login_state(page)) == LoginStatus.LOGIN_EXPIRED


def test_explicit_session_indicators_login_ok():
    page = FakePage(
        text="发布 消息 通知",
        session={"ready": True, "loginInputs": 0, "loginText": [], "sessionWords": ["发布", "消息"], "accountControls": 1},
    )
    session = BrowserSession(__import__("pathlib").Path("."), {"browser": {"login_timeout_minutes": 0}}, __import__("logging").getLogger("test"))
    assert asyncio.run(session.inspect_login_state(page)) == LoginStatus.LOGIN_OK


def test_login_panel_expired():
    page = FakePage(
        text="登录 注册 手机号 验证码",
        session={"ready": False, "loginInputs": 1, "loginText": ["登录"], "sessionWords": [], "accountControls": 0},
    )
    session = BrowserSession(__import__("pathlib").Path("."), {"browser": {"login_timeout_minutes": 0}}, __import__("logging").getLogger("test"))
    assert asyncio.run(session.inspect_login_state(page)) == LoginStatus.LOGIN_EXPIRED


def test_wait_for_login_does_not_repeat_goto():
    page = FakePage(text="loading")
    session = BrowserSession(__import__("pathlib").Path("."), {"browser": {"login_timeout_minutes": 0}}, __import__("logging").getLogger("test"))
    asyncio.run(session.navigate_to_login_target(page, "https://target"))
    asyncio.run(session.wait_for_login(page, "https://target"))
    assert page.goto_count == 1


def test_response_callback_exception_is_consumed():
    async def run():
        logger = CaptureLogger()
        session = BrowserSession(__import__("pathlib").Path("."), {}, logger)

        def boom(url, data):
            raise RuntimeError("callback failed with private value")

        session.response_callback = boom
        session._schedule_response_capture(FakeResponse())
        await asyncio.sleep(0)
        await session.flush_response_tasks(timeout=0.1)
        assert session._response_tasks == set()
        assert any(item and item[0] == "BROWSER response_task_failed error_type=%s reason=%s" for item in logger.messages)

    asyncio.run(run())


def test_response_task_timeout_is_cancelled_cleanly():
    async def run():
        logger = CaptureLogger()
        session = BrowserSession(__import__("pathlib").Path("."), {}, logger)
        task = asyncio.create_task(asyncio.sleep(60))
        session._response_tasks.add(task)
        await session.flush_response_tasks(timeout=0.01)
        assert task.cancelled()
        assert session._response_tasks == set()

    asyncio.run(run())

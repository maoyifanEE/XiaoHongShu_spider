from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable

from playwright.async_api import BrowserContext, Page, async_playwright

from .state import LoginStatus
from .utils import sanitize_url

VERIFICATION_WORDS = ["验证码", "滑块", "扫码验证", "短信验证", "安全验证", "人机验证", "captcha"]
RISK_WORDS = ["访问频繁", "风险", "异常访问", "当前环境异常", "请稍后再试", "账号异常"]


class BrowserSession:
    def __init__(self, base_dir: Path, config: dict[str, Any], logger: logging.Logger):
        self.base_dir = base_dir
        self.config = config
        self.logger = logger
        self.playwright = None
        self.context: BrowserContext | None = None
        self.browser_version: str | None = None
        self.response_callback: Callable[[str, Any], None] | None = None
        self._response_tasks: set[asyncio.Task] = set()

    async def __aenter__(self) -> "BrowserSession":
        browser_cfg = self.config.get("browser", {})
        self.playwright = await async_playwright().start()
        user_data_dir = self.base_dir / "browser_profile"
        headed = bool(browser_cfg.get("headed", True))
        slow_mo = int(browser_cfg.get("slow_mo_ms", 80) or 0)
        self.logger.info("BROWSER launch_persistent_context headed=%s profile=%s", headed, user_data_dir)
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=not headed,
            slow_mo=slow_mo,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        self.context.set_default_navigation_timeout(int(browser_cfg.get("navigation_timeout_ms", 60000)))
        setattr(self.context, "_xhs_browser_session", self)
        self.browser_version = self.context.browser.version if self.context.browser else None
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.flush_response_tasks(timeout=5)
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        self.logger.info("BROWSER closed")

    async def new_page(self) -> Page:
        if not self.context:
            raise RuntimeError("Browser context 尚未启动")
        page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        page.on("response", self._schedule_response_capture)
        return page

    def _schedule_response_capture(self, response: Any) -> None:
        task = asyncio.create_task(self._capture_response(response))
        self._response_tasks.add(task)
        task.add_done_callback(self._response_tasks.discard)

    async def flush_response_tasks(self, timeout: float = 10) -> None:
        if not self._response_tasks:
            return
        done, pending = await asyncio.wait(self._response_tasks, timeout=timeout)
        for task in done:
            if task.exception():
                self.logger.info("BROWSER response_task_failed error_type=%s", type(task.exception()).__name__)
        if pending:
            self.logger.info("BROWSER response_task_pending count=%s", len(pending))

    async def navigate_to_login_target(self, page: Page, target_url: str) -> None:
        self.logger.info("LOGIN_NAVIGATE target=creator_profile")
        await page.goto(target_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

    async def check_login(self, page: Page, target_url: str) -> LoginStatus:
        await self.navigate_to_login_target(page, target_url)
        return await self.inspect_login_state(page)

    async def inspect_login_state(self, page: Page) -> LoginStatus:
        profile_ready = await detect_profile_ready(page)
        session = await detect_session_indicators(page)
        self.logger.info(
            "LOGIN_CHECK profile_public_visible=%s session_ready=%s current_url=%s body_chars=%s login_form=%s login_text=%s note_links=%s session_words=%s",
            profile_ready["ready"],
            session["ready"],
            sanitize_url(page.url),
            profile_ready["textLength"],
            session["loginInputs"],
            session["loginText"],
            profile_ready["noteLinks"],
            session["sessionWords"],
        )
        if session["ready"]:
            self.logger.info("LOGIN_STATUS=LOGIN_OK account session indicators detected")
            return LoginStatus.LOGIN_OK
        text = await safe_body_text(page)
        if any(word in text for word in ["登录", "注册"]) and any(word in text for word in ["手机号", "扫码"]):
            self.logger.info("LOGIN_STATUS=LOGIN_EXPIRED login panel detected")
            return LoginStatus.LOGIN_EXPIRED
        status = await detect_page_status(page)
        if status in {LoginStatus.RISK_CONTROL_DETECTED, LoginStatus.HUMAN_VERIFICATION_REQUIRED}:
            self.logger.info("LOGIN_STATUS=%s", status.value)
            return status
        if profile_ready["ready"]:
            self.logger.info("LOGIN_STATUS=LOGIN_EXPIRED public profile visible but account session not detected")
            return LoginStatus.LOGIN_EXPIRED
        if len(text.strip()) < 20:
            self.logger.info("LOGIN_STATUS=PAGE_NOT_READY text_length=%s", len(text.strip()))
            return LoginStatus.PAGE_NOT_READY
        self.logger.info("LOGIN_STATUS=LOGIN_UNKNOWN fail_closed")
        return LoginStatus.LOGIN_UNKNOWN

    async def wait_for_login(self, page: Page, target_url: str) -> LoginStatus:
        timeout_minutes = int(self.config.get("browser", {}).get("login_timeout_minutes", 30))
        deadline = asyncio.get_event_loop().time() + timeout_minutes * 60
        print("首次运行需要登录小红书，请在当前浏览器窗口完成登录。")
        print("如果出现扫码、短信、CAPTCHA 或安全验证，请在当前浏览器窗口人工完成。")
        print("完成后无需关闭浏览器，程序会自动检测。")
        self.logger.info("LOGIN_STATUS waiting_for_human_login timeout_minutes=%s", timeout_minutes)
        while asyncio.get_event_loop().time() < deadline:
            status = await self.inspect_login_state(page)
            if status == LoginStatus.LOGIN_OK:
                await self.navigate_to_login_target(page, target_url)
                return await self.inspect_login_state(page)
            if status == LoginStatus.RISK_CONTROL_DETECTED:
                return status
            await page.wait_for_timeout(5000)
        self.logger.info("LOGIN_STATUS=LOGIN_EXPIRED login timeout")
        return LoginStatus.LOGIN_EXPIRED

    async def _capture_response(self, response: Any) -> None:
        if not self.response_callback:
            return
        url = response.url
        if "xiaohongshu.com" not in url:
            return
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            return
        try:
            data = await response.json()
        except Exception:
            return
        self.response_callback(url, data)


async def detect_page_status(page: Page) -> LoginStatus:
    text = await safe_body_text(page)
    lower = text.lower()
    if any(word in text for word in RISK_WORDS):
        return LoginStatus.RISK_CONTROL_DETECTED
    if any(word in text or word in lower for word in VERIFICATION_WORDS):
        return LoginStatus.HUMAN_VERIFICATION_REQUIRED
    return LoginStatus.LOGIN_OK


async def safe_body_text(page: Page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


async def detect_profile_ready(page: Page) -> dict[str, Any]:
    try:
        indicators = await page.evaluate(
            """
            () => {
              const text = document.body ? document.body.innerText : "";
              const noteLinks = document.querySelectorAll('a[href*="/explore/"], a[href*="/discovery/item/"]').length;
              const loginInputs = Array.from(document.querySelectorAll('input')).filter(input => {
                const text = `${input.placeholder || ""} ${input.name || ""} ${input.type || ""}`;
                return /手机号|验证码|password|login|phone/i.test(text);
              }).length;
              const hasProfileWords = text.includes("小红书号") || (text.includes("关注") && text.includes("粉丝"));
              const hasNoteArea = text.includes("笔记") || noteLinks > 0;
              return { hasProfileWords, hasNoteArea, noteLinks, loginInputs, textLength: text.length };
            }
            """
        )
    except Exception:
        return {"ready": False, "textLength": 0, "loginInputs": None, "noteLinks": None}
    ready = bool(
        indicators.get("hasProfileWords")
        and indicators.get("hasNoteArea")
        and int(indicators.get("loginInputs") or 0) == 0
    )
    indicators["ready"] = ready
    return indicators


async def detect_session_indicators(page: Page) -> dict[str, Any]:
    try:
        indicators = await page.evaluate(
            """
            () => {
              const text = document.body ? document.body.innerText : "";
              const inputs = Array.from(document.querySelectorAll('input')).map(input => `${input.placeholder || ""} ${input.name || ""} ${input.type || ""}`);
              const loginInputs = inputs.filter(value => /手机号|验证码|password|login|phone/i.test(value)).length;
              const buttons = Array.from(document.querySelectorAll('button, a, [role=button]')).map(el => (el.innerText || el.getAttribute("aria-label") || "").trim()).filter(Boolean);
              const loginText = buttons.filter(value => /登录|注册|扫码/.test(value)).slice(0, 5);
              const sessionWords = ["发布", "消息", "通知", "创作中心", "我的"].filter(value => text.includes(value));
              const accountControls = Array.from(document.querySelectorAll('header button, header a, nav button, nav a, [data-testid*="user" i], [aria-label*="用户"], [aria-label*="账号"], [aria-label*="个人"]')).length;
              return { loginInputs, loginText, sessionWords, accountControls, textLength: text.length };
            }
            """
        )
    except Exception:
        return {"ready": False, "loginInputs": None, "loginText": [], "sessionWords": [], "accountControls": None}
    has_login_prompt = bool(indicators.get("loginText")) or int(indicators.get("loginInputs") or 0) > 0
    ready = (
        not has_login_prompt
        and len(indicators.get("sessionWords") or []) >= 2
        and int(indicators.get("accountControls") or 0) >= 1
    )
    indicators["ready"] = ready
    return indicators

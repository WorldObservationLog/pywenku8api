"""浏览器兜底层：zendriver 驱动的真实浏览器，用于 HTTP 层无法通过的
Cloudflare Managed Challenge / 登录表单交互等场景。

继承原 Wenku8API 的成熟策略：
- 懒启动常驻浏览器，跨请求复用（cookie/会话保持）。
- 仅当页面确认为 CF 质询页才调 verify_cf，避免对正常页空等 15s。
- 封禁页（1015/1020 / Access denied）直接抛 RateLimitException。
- 渲染后 DOM 已是正确解码的 Unicode；剥离注入的 <tbody> 以免破坏既有 XPath。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

from wenku8.exceptions import CloudflareChallengeException, RateLimitException

_CHALLENGE_MARKERS = (
    "just a moment", "请稍候", "正在进行安全验证",
    "正在验证您是否是真人", "_cf_chl_opt", "cf-mitigated",
    "checking your browser", "verify you are human",
    "challenge-platform",  # Turnstile / 质询 iframe
)
# 封禁页判定必须优先于质询（"Attention Required!" 也可能是 block 页头）
_BLOCK_MARKERS = (
    "access denied", "used cloudflare to restrict access",
    "sorry, you have been blocked", "block_headline",
    "unable to access", "cf-error-details", "error 1015", "error 1020",
    "cf-error-code", "errorcode", "your ip address has been banned",
)


def is_cf_blocked(html: str) -> bool:
    low = (html or "")[:6000].lower()
    return any(m in low for m in _BLOCK_MARKERS)


def is_cf_challenge(html: str) -> bool:
    """判断是否 CF JS/Turnstile 质询（排除封禁页）。"""
    low = (html or "")[:6000].lower()
    if is_cf_blocked(low):
        return False
    return any(m in low for m in _CHALLENGE_MARKERS)


def strip_tbody(html: str) -> str:
    """浏览器渲染后的 DOM 会注入 <tbody>，破坏既有不带 tbody 的 XPath，故剥离。"""
    return re.sub(r"</?tbody[^>]*>", "", html, flags=re.IGNORECASE)


class BrowserFetcher:
    """常驻浏览器通道。同一实例内导航天然串行（单 tab）。"""

    def __init__(self, headless: bool = True, proxy: Optional[str] = None,
                 user_agent: Optional[str] = None, verify_timeout: float = 45.0,
                 language: str = "zh-CN"):
        self.headless = headless
        self.proxy = proxy
        self.user_agent = user_agent
        self.verify_timeout = verify_timeout
        self.language = language
        self._browser = None
        self._browser_lock = asyncio.Lock()
        self._nav_lock = asyncio.Lock()
        self._closed = False

    async def _ensure_browser(self):
        if self._browser is None:
            async with self._browser_lock:
                if self._browser is None:
                    import zendriver
                    browser_args = []
                    if self.proxy:
                        chrome_proxy = self.proxy.replace("socks5h://", "socks5://")
                        browser_args.append(f"--proxy-server={chrome_proxy}")
                    if self.user_agent:
                        browser_args.append(f"--user-agent={self.user_agent}")
                    browser_args.append("--disable-blink-features=AutomationControlled")
                    self._browser = await zendriver.start(
                        config=zendriver.Config(
                            headless=self.headless, sandbox=False,
                            browser_args=browser_args))
        return self._browser

    async def _wait_cf(self, tab, timeout: Optional[float] = None) -> str:
        timeout = timeout or self.verify_timeout
        try:
            await tab.wait_for_ready_state("complete", timeout=15)
        except Exception:
            pass
        deadline = time.monotonic() + timeout
        html = await tab.get_content()
        if is_cf_blocked(html):
            raise RateLimitException(f"Cloudflare 封禁/IP 限流: {html[:2000]}")
        while is_cf_challenge(html) and time.monotonic() < deadline:
            if is_cf_blocked(html):
                raise RateLimitException(f"Cloudflare 封禁/IP 限流: {html[:2000]}")
            try:
                await tab.verify_cf()
            except Exception:
                pass
            await asyncio.sleep(2)
            if not is_cf_challenge(await tab.get_content()):
                try:
                    await tab.wait_for_ready_state("complete", timeout=15)
                except Exception:
                    pass
                return await tab.get_content()
            try:
                await tab.reload()
            except Exception:
                pass
            try:
                await tab.wait_for_ready_state("complete", timeout=15)
            except Exception:
                pass
            html = await tab.get_content()
        if is_cf_blocked(html):
            raise RateLimitException(f"Cloudflare 封禁/IP 限流: {html[:2000]}")
        if is_cf_challenge(html):
            raise CloudflareChallengeException(
                "Cloudflare 质询在限时内未解决", snippet=html[:2000])
        return html

    async def get_html(self, url: str) -> str:
        """导航到 url，处理质询，返回渲染后（去 tbody）的 HTML。"""
        browser = await self._ensure_browser()
        async with self._nav_lock:
            tab = browser.main_tab
            await tab.get(url)
            html = strip_tbody(await self._wait_cf(tab))
            if is_cf_blocked(html):
                raise RateLimitException(f"Cloudflare 封禁/IP 限流: {html[:2000]}")
            if is_cf_challenge(html):
                raise CloudflareChallengeException("未解决的质询残留页", url=url, snippet=html[:2000])
            return html

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._browser is not None:
            try:
                await self._browser.stop()
            finally:
                self._browser = None

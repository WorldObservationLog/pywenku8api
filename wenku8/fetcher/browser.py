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

    async def get_html(self, url: str, timeout: Optional[float] = None) -> str:
        """导航到 url，处理质询，返回渲染后（去 tbody）的 HTML。

        超时/内部异常后**必须丢弃浏览器实例**：实测（2026-09）用
        asyncio.wait_for 取消挂起的 tab.get() 虽能返回，但会令 zendriver 的
        Listener.listener_loop 抛 InvalidStateError 而死亡，该实例内所有
        后续导航永久挂起（常驻复用 → 等于永久卡死）。故超时后重置浏览器，
        由下次请求重建；异常统一转 SourceUnavailableException 供上层 fallback。
        """
        timeout = timeout or (self.verify_timeout + 30.0)
        try:
            return await asyncio.wait_for(self._get_html_impl(url), timeout=timeout)
        except asyncio.TimeoutError:
            await self._reset_browser()
            from wenku8.exceptions import SourceUnavailableException
            raise SourceUnavailableException(
                "browser", f"浏览器兜底超时({timeout:.0f}s): {url}")
        except asyncio.CancelledError:
            raise
        except (RateLimitException, CloudflareChallengeException):
            raise  # 业务语义的过盾失败：浏览器实例本身仍健康，不重置
        except Exception as e:  # zendriver 内部错误 → 状态可能已损坏
            await self._reset_browser()
            from wenku8.exceptions import SourceUnavailableException
            raise SourceUnavailableException(
                "browser", f"浏览器通道异常: {type(e).__name__}: {e}")

    async def _get_html_impl(self, url: str) -> str:
        # 先取导航锁再取浏览器实例：保证拿到的始终是"当前"实例，
        # 避免重置把实例摘掉后旧引用仍被使用（会挂在被 stop 的浏览器上）。
        async with self._nav_lock:
            browser = await self._ensure_browser()
            tab = browser.main_tab
            await tab.get(url)
            html = strip_tbody(await self._wait_cf(tab))
            if is_cf_blocked(html):
                raise RateLimitException(f"Cloudflare 封禁/IP 限流: {html[:2000]}")
            if is_cf_challenge(html):
                raise CloudflareChallengeException("未解决的质询残留页", url=url, snippet=html[:2000])
            return html

    async def _reset_browser(self) -> None:
        """丢弃当前浏览器实例：先抢占导航锁（确保没有在途导航），再摘引用，
        最后限时 stop() —— stop 自身可能因连接损坏挂起，超时则强杀进程兜底。"""
        got_nav = False
        try:
            await asyncio.wait_for(self._nav_lock.acquire(), timeout=5.0)
            got_nav = True
        except Exception:
            got_nav = False  # 僵尸导航仍持锁：仍要摘引用/杀进程
        try:
            async with self._browser_lock:
                browser = self._browser
                self._browser = None
        finally:
            if got_nav:
                self._nav_lock.release()
        if browser is None:
            return
        try:
            await asyncio.wait_for(browser.stop(), timeout=8.0)
        except Exception:
            try:  # stop 挂起/失败 → 直接强杀子进程
                proc = getattr(browser, "_process", None)
                if proc is not None and proc.returncode is None:
                    proc.kill()
            except Exception:
                pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._reset_browser()

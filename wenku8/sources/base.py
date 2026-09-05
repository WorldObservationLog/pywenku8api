"""BaseSource：单一数据来源的抽象基类。

每个来源实例持有：
- profile（FingerprintProfile：preset / UA / referer）
- limiter（来源级限速+熔断）
- fetcher（统一外发通道：HTTP 指纹层 + 浏览器兜底）
- 会话状态（cookie、登录标志）

子类实现：
- capabilities 集合（可选覆写，用于父类能力过滤）
- 一组 fetch_* 方法：fetch_novel_info / fetch_novel_index / fetch_novel_content /
  fetch_full_novel_content / fetch_novel_cover / fetch_search / fetch_novel_list /
  fetch_bookshelf / login / logout / close

语义约定：
- 所有 fetch_* 抛出 wenku8.exceptions 中定义的异常；上层按优先级链 fallback。
- 文本字段返回简体（与旧库一致）；繁体由上层用 Lang.zh_TW 统一转换。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from wenku8.consts import Capability, Lang, Source
from wenku8.fetcher.base import Fetcher
from wenku8.fetcher.browser import BrowserFetcher
from wenku8.fetcher.fingerprint import FingerprintProfile
from wenku8.limiter import RateLimitConfig, SourceRateLimiter


class BaseSource:
    source: Source = Source.web

    def __init__(self, proxy: Optional[str] = None,
                 rate_config: Optional[RateLimitConfig] = None,
                 headless: bool = True,
                 allow_browser_fallback: bool = True,
                 browser: Optional[BrowserFetcher] = None,
                 credentials: Optional[dict] = None,
                 label: str = ""):
        self.proxy = proxy
        self.rate_config = rate_config or RateLimitConfig.conservative()
        self.headless = headless
        self.allow_browser_fallback = allow_browser_fallback
        self.credentials = dict(credentials or {})
        self.label = label or self.source.value
        self._profile = FingerprintProfile.for_source(self.source)
        self._limiter = SourceRateLimiter(
            source=self.source.value, source_config=self.rate_config,
            label=self.label)
        self._browser = browser
        self._fetcher: Optional[Fetcher] = None
        self._session_lock = asyncio.Lock()
        self._closed = False
        # 来源自带浏览器实例时由本类负责生命周期；共享时置 None
        self._owns_browser = browser is None
        self._cookies: dict[str, str] = {}

    @property
    def profile(self) -> FingerprintProfile:
        return self._profile

    @property
    def limiter(self) -> SourceRateLimiter:
        return self._limiter

    @property
    def capabilities(self) -> set[Capability]:
        # 子类可覆写；默认全能力
        from wenku8.consts import DEFAULT_SOURCE_CAPABILITIES
        return DEFAULT_SOURCE_CAPABILITIES.get(self.source, set(Capability))

    async def _ensure_fetcher(self) -> Fetcher:
        if self._fetcher is None:
            async with self._session_lock:
                if self._fetcher is None:
                    # 未显式共享浏览器时，若允许兜底则懒创建一个自管浏览器
                    browser = self._browser
                    if browser is None and self.allow_browser_fallback:
                        from wenku8.fetcher.browser import BrowserFetcher
                        browser = BrowserFetcher(headless=self.headless,
                                                 proxy=self.proxy,
                                                 user_agent=self._profile.user_agent)
                        self._browser = browser
                        self._owns_browser = True
                    self._fetcher = Fetcher(
                        profile=self._profile, limiter=self._limiter,
                        proxy=self.proxy,
                        browser=browser if self.allow_browser_fallback else None)
        return self._fetcher

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._fetcher is not None:
            await self._fetcher.close()
            self._fetcher = None
        if self._owns_browser and self._browser is not None:
            await self._browser.close()
            self._browser = None

    # ---- 由子类实现 ----
    async def login(self, username: str, password: str, **kw) -> bool:  # pragma: no cover
        raise NotImplementedError

    async def logout(self) -> None:  # pragma: no cover
        raise NotImplementedError

    @property
    def is_logged_in(self) -> bool:
        """默认：无登录概念（CDN 等）。子类可覆写。"""
        return False

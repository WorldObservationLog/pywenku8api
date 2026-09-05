"""统一外发通道 Fetcher：限速 → HTTP 指纹层 → 质询判定 → 浏览器兜底 → 退避重试。

职责：
- 按来源持有/共享 HttpCloakSession 与 BrowserFetcher（浏览器可被多个来源共享）。
- 每次请求前先经过 SourceRateLimiter.wait_ready()。
- 优先使用 HTTP 指纹层；若响应为 CF 质询/封禁，自动切换到浏览器通道。
- 429/封禁 记入熔断状态；网络异常按 retry 次数退避重试。
- 单来源操作失败且熔断时抛出 SourceUnavailableException，供上层进入下一来源。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from wenku8.exceptions import (
    CloudflareChallengeException, RateLimitException, SourceUnavailableException,
)
from wenku8.fetcher.browser import BrowserFetcher, is_cf_blocked, is_cf_challenge
from wenku8.fetcher.fingerprint import FingerprintProfile
from wenku8.fetcher.http import HttpCloakSession, HttpResponse
from wenku8.limiter import SourceRateLimiter

logger = logging.getLogger("wenku8.fetcher")


class Fetcher:
    def __init__(self, profile: FingerprintProfile, limiter: SourceRateLimiter,
                 proxy: Optional[str] = None, browser: Optional[BrowserFetcher] = None,
                 timeout: int = 30, retries: int = 1):
        self._profile = profile
        self._limiter = limiter
        self._proxy = proxy
        self._browser = browser
        self._timeout = timeout
        self._retries = retries
        self._http: Optional[HttpCloakSession] = None
        self._http_lock = asyncio.Lock()

    @property
    def source(self) -> str:
        return self._profile.source

    async def _get_http(self) -> HttpCloakSession:
        if self._http is None:
            async with self._http_lock:
                if self._http is None:
                    self._http = HttpCloakSession(profile=self._profile,
                                                  proxy=self._proxy,
                                                  timeout=self._timeout,
                                                  retry=self._retries)
        return self._http

    async def get(self, url: str, no_cache: bool = False, **kwargs) -> HttpResponse:
        return await self._request("GET", url, no_cache=no_cache, **kwargs)

    async def post(self, url: str, **kwargs) -> HttpResponse:
        return await self._request("POST", url, **kwargs)

    async def post_raw(self, url: str, *, data=None,
                       headers: Optional[dict[str, str]] = None,
                       timeout: Optional[int] = None) -> HttpResponse:
        """纯透传 POST（供 API 中继 App 协议：不带浏览器化头）。"""
        await self._limiter.wait_ready()
        http = await self._get_http()
        return await http.post_raw(url, data=data, headers=headers, timeout=timeout)

    async def _request(self, method: str, url: str, *, use_browser: bool = False,
                       no_cache: bool = False, **kwargs) -> HttpResponse:
        """发起请求（方法私有，业务层通过 get/post 调用）。

        use_browser=True 时跳过 HTTP 层直接走浏览器（如登录表单/需要 JS 的场景）。
        """
        await self._limiter.wait_ready()
        if use_browser:
            return await self._request_via_browser(method, url, **kwargs)

        http = await self._get_http()
        last_err: Optional[Exception] = None

        def _do_http():
            if method == "GET":
                return http.get(url, no_cache=no_cache, **kwargs)
            return http.post(url, **kwargs)

        for attempt in range(1 + self._retries):
            try:
                resp = await _do_http()
                return await self._classify_response(resp, url)
            except (RateLimitException, CloudflareChallengeException, SourceUnavailableException) as e:
                # 限流/质询由上层策略处理：先尝试浏览器解决质询
                if isinstance(e, CloudflareChallengeException) and self._browser is not None:
                    try:
                        return await self._request_via_browser(method, url, **kwargs)
                    except (RateLimitException, SourceUnavailableException):
                        raise
                self._limiter.report_rate_limited() if isinstance(e, RateLimitException) \
                    else self._limiter.report_error()
                raise
            except Exception as e:  # noqa: BLE001 网络错误、TLS 等
                last_err = e
                self._limiter.report_error()
                if attempt < self._retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
        raise SourceUnavailableException(self.source, f"HTTP 层失败: {last_err}")

    async def _classify_response(self, resp: HttpResponse, url: str) -> HttpResponse:
        """根据状态码与页面特征分流。

        - 403 + 质询标记 → CloudflareChallengeException（触发浏览器兜底）
        - 429 → RateLimitException
        - 其它按原样返回（业务层再做内容解析校验）
        """
        body_head = resp.body[:6000].decode("utf-8", "ignore")
        if resp.status_code == 429:
            retry_after = None
            if "retry-after" in resp.headers:
                try:
                    retry_after = float(resp.headers["retry-after"])
                except ValueError:
                    pass
            self._limiter.report_rate_limited()
            raise RateLimitException("HTTP 429 限流", retry_after=retry_after,
                                     source=self.source)
        if resp.status_code in (403, 503) and is_cf_challenge(body_head):
            self._limiter.report_error()
            raise CloudflareChallengeException(
                "HTTP 层命中 Cloudflare 质询", url=url, snippet=body_head)
        if resp.status_code in (403, 503) and is_cf_blocked(body_head):
            self._limiter.report_rate_limited()
            raise RateLimitException("Cloudflare 封禁页", source=self.source,
                                     retry_after=None)
        return resp

    async def _request_via_browser(self, method: str, url: str, **kwargs) -> HttpResponse:
        if self._browser is None:
            raise SourceUnavailableException(self.source, "无可用浏览器兜底")
        # 浏览器只做 GET（页面导航）；如有 POST 需求（登录表单），业务层自行处理。
        html = await self._browser.get_html(url)
        return HttpResponse(status_code=200, headers={},
                            body=html.encode("utf-8", "replace"),
                            url=url, protocol="browser")

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None
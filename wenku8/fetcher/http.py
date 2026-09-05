"""HTTP 指纹层：基于 httpcloak 的同步 Session，封装为 asyncio 友好接口。

为什么不用 curl_cffi：实测（见 docs/cf_rate_limit_research.md）在相同出口下，
curl_cffi 对 wenku8.net 返回 403 Managed Challenge，而 httpcloak 全栈指纹
（TLS/HTTP2 帧/优先级/TCP 参数/头顺序）稳定返回 200。因此本项目 HTTP 层
统一使用 httpcloak。

设计：
- 一个来源实例对应一个 httpcloak.Session（cookie jar、TLS 会话、代理均粘性）。
- 阻塞调用放到 asyncio.to_thread 线程池执行，保持对外纯 async 接口。
- 提供 _request 核心：带 UA/Referer 等浏览器化头；返回 (status, headers, body bytes)。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from wenku8.fetcher.fingerprint import FingerprintProfile, normalize_preset


@dataclass
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes
    url: str
    protocol: str = ""

    @property
    def text(self) -> str:
        """按响应 charset 或 UTF-8 解码（wenku8 为 GBK，需调用方自行处理时用）。"""
        ctype = (self.headers.get("content-type") or "").lower()
        if "charset=" in ctype:
            enc = ctype.split("charset=")[-1].strip().strip('"').strip("'")
            try:
                return self.body.decode(enc, errors="replace")
            except LookupError:
                pass
        for enc in ("utf-8", "gbk"):
            try:
                return self.body.decode(enc)
            except UnicodeDecodeError:
                continue
        return self.body.decode("utf-8", errors="replace")


class HttpCloakSession:
    """对 httpcloak.Session 的最小 asyncio 封装。"""

    def __init__(self, profile: FingerprintProfile, proxy: Optional[str] = None,
                 timeout: int = 30, retry: int = 0, verify: bool = True,
                 extra_headers: Optional[dict[str, str]] = None):
        import httpcloak
        self._httpcloak = httpcloak
        preset = normalize_preset(profile.preset)
        self._session = httpcloak.Session(
            preset=preset,
            proxy=proxy,
            timeout=timeout,
            retry=retry,
            verify=verify,
        )
        self._profile = profile
        self._extra = dict(extra_headers or {})
        self._lock = asyncio.Lock()   # 串行化写操作与 cookie 同步
        self._closed = False

    @property
    def profile(self) -> FingerprintProfile:
        return self._profile

    @property
    def session(self):
        return self._session

    def _base_headers(self, referer: Optional[str] = None) -> dict[str, str]:
        h = {
            "User-Agent": self._profile.user_agent,
            "Referer": referer or self._profile.referer,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                      "image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        h.update(self._extra)
        return h

    async def get(self, url: str, *, params: Optional[dict] = None,
                  headers: Optional[dict[str, str]] = None,
                  referer: Optional[str] = None,
                  timeout: Optional[int] = None,
                  no_cache: bool = False) -> HttpResponse:
        return await asyncio.to_thread(
            self._request, "GET", url, params=params,
            headers=headers, referer=referer, timeout=timeout, no_cache=no_cache)

    async def post(self, url: str, *, data=None, json=None,
                   headers: Optional[dict[str, str]] = None,
                   referer: Optional[str] = None,
                   timeout: Optional[int] = None) -> HttpResponse:
        return await asyncio.to_thread(
            self._request, "POST", url, data=data, json=json,
            headers=headers, referer=referer, timeout=timeout)

    async def post_raw(self, url: str, *, data=None,
                       headers: Optional[dict[str, str]] = None,
                       timeout: Optional[int] = None) -> HttpResponse:
        """完全透传的 POST：不加浏览器化头/UA/Referer，headers 仅用调用方提供。

        供 API 中继等「非浏览器、App 协议」场景使用（服务端校验 UA 与头纯净度）。
        """
        return await asyncio.to_thread(
            self._request, "POST", url, data=data, headers=headers,
            timeout=timeout, raw=True)

    def _request(self, method: str, url: str, *, params=None, data=None, json=None,
                 headers=None, referer: Optional[str] = None,
                 timeout: Optional[int] = None,
                 raw: bool = False, no_cache: bool = False) -> HttpResponse:
        if raw:
            # 原始模式：完全按调用方给的 headers（不注入 UA/Referer 等）
            hdrs = dict(headers or {})
        else:
            hdrs = self._base_headers(referer)
            if headers:
                hdrs.update(headers)
        fn = getattr(self._session, method.lower())
        kwargs: dict = {"headers": hdrs, "timeout": timeout}
        if params is not None:
            kwargs["params"] = params
        if method == "GET" and no_cache:
            # 禁用条件缓存（304）：同 session 重复取同一资源时保证拿到 200 body
            try:
                kwargs["disable_conditional_cache"] = True
            except TypeError:
                pass
        if method == "POST":
            if data is not None:
                kwargs["data"] = data
            if json is not None:
                kwargs["json"] = json
        # httpcloak 的 GET/HEAD 等不接受 data/json，避免透传
        resp = fn(url, **kwargs)
        resp_headers = {}
        raw = getattr(resp, "headers", None)
        if isinstance(raw, dict):
            for k, v in raw.items():
                key = str(k).lower()
                if key in ("set-cookie",) and isinstance(v, (list, tuple)):
                    # 保留多值头为 list（调用方如会话捕获需要逐条）
                    resp_headers[key] = [str(x) for x in v]
                else:
                    resp_headers[key] = str(v)
        elif raw is not None:
            # 可能是 list[(name,value)] 或类似结构
            try:
                for pair in raw:
                    if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                        key = str(pair[0]).lower()
                        val = pair[1]
                        if key == "set-cookie":
                            resp_headers.setdefault(key, []).append(str(val))
                        else:
                            resp_headers[key] = str(val)
            except TypeError:
                pass
        body = getattr(resp, "content", b"")
        if body is None:
            body = b""
        if not isinstance(body, bytes):
            try:
                body = bytes(body)
            except Exception:
                body = str(body).encode("utf-8", "replace")
        return HttpResponse(status_code=int(getattr(resp, "status_code", -1)),
                            headers=resp_headers,
                            body=body,
                            url=str(getattr(resp, "url", url)),
                            protocol=str(getattr(resp, "protocol", "")))

    async def set_proxy(self, proxy: Optional[str]) -> None:
        async with self._lock:
            if self._closed:
                return
            await asyncio.to_thread(self._session.set_proxy, proxy or "")

    async def cookies(self) -> list:
        return await asyncio.to_thread(self._session.get_cookies)

    async def close(self) -> None:
        async with self._lock:
            if not self._closed:
                self._closed = True
                await asyncio.to_thread(self._session.close)

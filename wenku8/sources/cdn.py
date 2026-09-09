"""CDN 资源源：img.wenku8.com（封面/插图）与 dlN.wenku8.com（整本 TXT）。

它不参与业务优先级链，作为“能力挂靠”被 Wenku8Client 以统一入口暴露：
- get_novel_cover(aid) → bytes（JPEG）
- get_full_novel_content(aid, lang) → str（UTF-8 TXT，节点 dl1/dl2 回退）
- get_picture(url) → bytes

实测：img.wenku8.com 与 dlN.wenku8.com 无 Cloudflare 质询，httpcloak 直连 200，
可放宽限速（RateLimitConfig.relaxed）。
"""
from __future__ import annotations

import re
from typing import Optional

from wenku8.consts import Capability, Lang, Source
from wenku8.exceptions import PageParseError
from wenku8.limiter import RateLimitConfig, SourceRateLimiter
from wenku8.sources.base import BaseSource
from wenku8.utils import lang_convent

IMG_ENDPOINT = "https://img.wenku8.com"
DL_ENDPOINTS = ("https://dl1.wenku8.com", "https://dl2.wenku8.com")


class CdnSource(BaseSource):
    source = Source.cdn

    def __init__(self, img_endpoint: str = IMG_ENDPOINT,
                 dl_endpoints: tuple[str, ...] = DL_ENDPOINTS,
                 full_txt_ttl: float = 24 * 3600, **kwargs):
        # CDN 静态资源不需要浏览器兜底
        kwargs.setdefault("allow_browser_fallback", False)
        # 主限速器服务封面/图片（img.wenku8.com 小文件）：默认 image()；
        # caller 显式传 rate_config 时尊重传入值（含显式 None → 用 image()）。
        rate_config = kwargs.pop("rate_config", None)
        kwargs["rate_config"] = rate_config or RateLimitConfig.image()
        full_rate = kwargs.pop("full_rate", None)  # 整本下载专用限速
        super().__init__(**kwargs)
        self.img_endpoint = img_endpoint.rstrip("/")
        self.dl_endpoints = dl_endpoints
        # 整本下载独立限速器：封面/图片走来源 limiter(image, 8rps)；
        # 整本大文件按 full_rate 慢速（实测连续 4-5 本会触发 CDN 429）
        self._full_limiter = SourceRateLimiter(
            source=self.source.value,
            source_config=full_rate or RateLimitConfig.full_dl(),
            label=f"{self.source.value}:full_dl")
        # aid -> (expire_monotonic, 简体content)；键不含 lang（共享）
        self._full_cache: dict[int, tuple[float, str]] = {}
        self._full_txt_ttl = full_txt_ttl
        # 封面/图片字节缓存（24h；封面基本不变，重复请求免网络）
        self._image_cache: dict[str, tuple[float, bytes]] = {}
        self._image_ttl = 24 * 3600

    def _image_get(self, key: str) -> Optional[bytes]:
        import time as _t
        hit = self._image_cache.get(key)
        if hit and hit[0] > _t.monotonic():
            return hit[1]
        self._image_cache.pop(key, None)
        return None

    def _image_set(self, key: str, data: bytes) -> None:
        import time as _t
        now = _t.monotonic()
        self._image_cache[key] = (now + self._image_ttl, data)
        # 惰性清理过期项（防无限增长）
        if len(self._image_cache) > 10000:
            for k in [k for k, (exp, _) in self._image_cache.items() if exp <= now]:
                del self._image_cache[k]

    # ---- 能力 ----
    @property
    def capabilities(self) -> set[Capability]:
        return {Capability.NOVEL_COVER, Capability.NOVEL_FULL, Capability.PICTURE}

    # ---- 封面 ----
    def cover_url(self, aid: int) -> str:
        aid = int(aid)
        return f"{self.img_endpoint}/image/{aid // 1000}/{aid}/{aid}s.jpg"

    async def fetch_novel_cover(self, aid: int) -> bytes:
        key = self.cover_url(aid)
        cached = self._image_get(key)
        if cached is not None:
            return cached
        fetcher = await self._ensure_fetcher()
        # no_cache: 同一会话重复取封面时避免 304（httpcloak 条件缓存）
        resp = await fetcher.get(key, no_cache=True)
        if resp.status_code != 200 or not resp.body:
            raise PageParseError(f"封面下载失败 status={resp.status_code}",
                                 url=key, source=self.source.value)
        self._image_set(key, resp.body)
        return resp.body

    async def get_picture(self, url: str) -> bytes:
        cached = self._image_get(url)
        if cached is not None:
            return cached
        fetcher = await self._ensure_fetcher()
        resp = await fetcher.get(url, no_cache=True)
        if resp.status_code != 200 or not resp.body:
            raise PageParseError(f"图片下载失败 status={resp.status_code}",
                                 url=url, source=self.source.value)
        self._image_set(url, resp.body)
        return resp.body

    # ---- 整本 TXT ----
    def full_txt_url(self, node: str, aid: int) -> str:
        aid = int(aid)
        return f"{node}/txtutf8/{aid // 1000}/{aid}.txt"

    async def fetch_full_novel_content(self, aid: int,
                                       lang: Lang = Lang.zh_CN) -> str:
        """整本下载：节点逐一尝试，失败切下一个；简体结果短时缓存
        （键不含 lang —— zh_CN/zh_TW 共享同一份简体，转换在门面做）。"""
        import time
        now = time.monotonic()
        hit = self._full_cache.get(aid)
        if hit and hit[0] > now:
            return hit[1]

        # 整本大文件走专用慢速闸（避开 CDN 短窗多本 429）
        await self._full_limiter.wait_ready()

        fetcher = await self._ensure_fetcher()
        last_err: Optional[Exception] = None
        for node in self.dl_endpoints:
            url = self.full_txt_url(node, aid)
            try:
                # no_cache：同一会话重复下载同一本时避免 httpcloak 304 空体
                resp = await fetcher.get(url, no_cache=True)
                if resp.status_code == 429:
                    last_err = PageParseError(f"整本下载 429", url=url,
                                              source=self.source.value)
                    continue
                if resp.status_code != 200 or not resp.body:
                    last_err = PageParseError(f"整本下载 HTTP {resp.status_code}",
                                              url=url, source=self.source.value)
                    continue
                content = resp.body.decode("utf-8", "replace")  # 简体原文
                self._full_cache[aid] = (now + self._full_txt_ttl, content)
                # 惰性清理过期缓存
                for k in [k for k, (exp, _) in self._full_cache.items() if exp <= now]:
                    del self._full_cache[k]
                return content
            except Exception as e:  # noqa: BLE001 网络/TLS 错误 → 换节点
                last_err = e
        raise last_err or PageParseError("整本下载全部节点失败", source=self.source.value)

    # 便捷别名：旧 get_full_novel_content 语义
    fetch_novel_full = fetch_full_novel_content

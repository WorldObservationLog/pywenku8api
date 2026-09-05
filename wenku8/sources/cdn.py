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
from wenku8.sources.base import BaseSource
from wenku8.utils import lang_convent

IMG_ENDPOINT = "https://img.wenku8.com"
DL_ENDPOINTS = ("https://dl1.wenku8.com", "https://dl2.wenku8.com")


class CdnSource(BaseSource):
    source = Source.cdn

    def __init__(self, img_endpoint: str = IMG_ENDPOINT,
                 dl_endpoints: tuple[str, ...] = DL_ENDPOINTS,
                 full_txt_ttl: float = 30 * 60, **kwargs):
        # CDN 静态资源不需要浏览器兜底
        kwargs.setdefault("allow_browser_fallback", False)
        super().__init__(**kwargs)
        self.img_endpoint = img_endpoint.rstrip("/")
        self.dl_endpoints = dl_endpoints
        # (aid, lang) -> (expire_monotonic, content)
        self._full_cache: dict[tuple[int, Lang], tuple[float, str]] = {}
        self._full_txt_ttl = full_txt_ttl

    # ---- 能力 ----
    @property
    def capabilities(self) -> set[Capability]:
        return {Capability.NOVEL_COVER, Capability.NOVEL_FULL, Capability.PICTURE}

    # ---- 封面 ----
    def cover_url(self, aid: int) -> str:
        aid = int(aid)
        return f"{self.img_endpoint}/image/{aid // 1000}/{aid}/{aid}s.jpg"

    async def fetch_novel_cover(self, aid: int) -> bytes:
        fetcher = await self._ensure_fetcher()
        # no_cache: 同一会话重复取封面时避免 304（httpcloak 条件缓存）
        resp = await fetcher.get(self.cover_url(aid), no_cache=True)
        if resp.status_code != 200 or not resp.body:
            raise PageParseError(f"封面下载失败 status={resp.status_code}",
                                 url=self.cover_url(aid), source=self.source.value)
        return resp.body

    async def get_picture(self, url: str) -> bytes:
        fetcher = await self._ensure_fetcher()
        resp = await fetcher.get(url, no_cache=True)
        if resp.status_code != 200 or not resp.body:
            raise PageParseError(f"图片下载失败 status={resp.status_code}",
                                 url=url, source=self.source.value)
        return resp.body

    # ---- 整本 TXT ----
    def full_txt_url(self, node: str, aid: int) -> str:
        aid = int(aid)
        return f"{node}/txtutf8/{aid // 1000}/{aid}.txt"

    async def fetch_full_novel_content(self, aid: int,
                                       lang: Lang = Lang.zh_CN) -> str:
        """整本下载：节点逐一尝试，失败切下一个；结果短时缓存。"""
        import time
        now = time.monotonic()
        hit = self._full_cache.get((aid, lang))
        if hit and hit[0] > now:
            return hit[1]

        fetcher = await self._ensure_fetcher()
        last_err: Optional[Exception] = None
        for node in self.dl_endpoints:
            url = self.full_txt_url(node, aid)
            try:
                resp = await fetcher.get(url)
                if resp.status_code == 429:
                    last_err = PageParseError(f"整本下载 429", url=url,
                                              source=self.source.value)
                    continue
                if resp.status_code != 200 or not resp.body:
                    last_err = PageParseError(f"整本下载 HTTP {resp.status_code}",
                                              url=url, source=self.source.value)
                    continue
                content = lang_convent(resp.body.decode("utf-8", "replace"), lang)
                self._full_cache[(aid, lang)] = (now + self._full_txt_ttl, content)
                # 惰性清理过期缓存
                for k in [k for k, (exp, _) in self._full_cache.items() if exp <= now]:
                    del self._full_cache[k]
                return content
            except Exception as e:  # noqa: BLE001 网络/TLS 错误 → 换节点
                last_err = e
        raise last_err or PageParseError("整本下载全部节点失败", source=self.source.value)

    # 便捷别名：旧 get_full_novel_content 语义
    fetch_novel_full = fetch_full_novel_content

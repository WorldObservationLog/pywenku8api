"""Web（桌面 HTML 版）来源。

端点结构：
- 首页/普通页：  https://www.wenku8.net/
- 详情：        /modules/article/articleinfo.php?id={aid}&charset=gbk
- 目录：        /modules/article/reader.php?aid={aid}&charset=gbk
- 章节：        /modules/article/reader.php?aid={aid}&cid={cid}&charset=gbk
- 搜索：        /modules/article/search.php?searchtype={method}&searchkey={gbk quote}&page={p}
- 排行：        /modules/article/toplist.php?sort={sort}&page={p}&charset=gbk
- 书架：        /modules/article/bookcase.php?classid={bid}

说明：
- 登录表单在 login.php?do=submit（HTTP 直连实测 200 无质询）；POST 后由
  Set-Cookie 下发 PHPSESSID 与 jieqi* 会话 cookie。
- 深层 module 页在当前出口会被 Cloudflare Managed Challenge 拦截（详见研究文档），
  因此本来源支持 allow_browser_fallback，交由 Fetcher 切换浏览器通道。
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote

from wenku8.consts import Capability, Lang, SearchMethod, Source
from wenku8.exceptions import LoginErrorException, PageParseError
from wenku8.models import (
    Book, NovelContent, NovelIndex, NovelInfo, SearchResult,
)
from wenku8.parsers import html_common
from wenku8.sources.base import BaseSource
from wenku8.utils import lang_convent

WEB_ENDPOINT = "https://www.wenku8.net"


class WebSource(BaseSource):
    source = Source.web

    def __init__(self, endpoint: str = WEB_ENDPOINT, **kwargs):
        super().__init__(**kwargs)
        self.endpoint = endpoint.rstrip("/")

    # ---- URL 构造 ----
    def _info_url(self, aid: int, lang: Lang) -> str:
        return f"{self.endpoint}/modules/article/articleinfo.php?id={aid}&charset={lang.charset}"

    def _reader_url(self, aid: int, lang: Lang, cid: Optional[int] = None) -> str:
        q = f"aid={aid}&charset={lang.charset}"
        if cid is not None:
            q += f"&cid={cid}"
        return f"{self.endpoint}/modules/article/reader.php?{q}"

    def _search_url(self, keyword: str, method: SearchMethod, page: int,
                    lang: Lang) -> str:
        # 注意：search.php 不接受 charset 参数——带 charset=gbk 会触发 Cloudflare
        # 拦截（与 bookcase.php 同理，见 legacy 注释）。搜索结果页固定 GBK 编码。
        kw = quote(keyword.encode("gbk"))
        return (f"{self.endpoint}/modules/article/search.php?searchtype={method.value}"
                f"&searchkey={kw}&page={page}")

    def _toplist_url(self, sort, page: int, lang: Lang) -> str:
        return (f"{self.endpoint}/modules/article/toplist.php?sort={sort}"
                f"&page={page}&charset={lang.charset}")

    def _bookcase_url(self, classid: int = 0) -> str:
        return f"{self.endpoint}/modules/article/bookcase.php?classid={classid}"

    # ---- 数据获取 ----
    async def fetch_novel_info(self, aid: int, lang: Lang = Lang.zh_CN) -> NovelInfo:
        fetcher = await self._ensure_fetcher()
        url = self._info_url(aid, lang)
        html = await self._page(fetcher, url)
        info = html_common.parse_novel_info(html, aid, url=url)
        return lang_convent(info, lang)

    async def fetch_novel_index(self, aid: int, lang: Lang = Lang.zh_CN) -> NovelIndex:
        fetcher = await self._ensure_fetcher()
        url = self._reader_url(aid, lang)
        html = await self._page(fetcher, url)
        index = html_common.parse_novel_index(html, aid, url=url)
        return lang_convent(index, lang)

    async def fetch_novel_content(self, aid: int, cid: int,
                                  lang: Lang = Lang.zh_CN) -> NovelContent:
        fetcher = await self._ensure_fetcher()
        url = self._reader_url(aid, lang, cid=cid)
        html = await self._page(fetcher, url)
        content = html_common.parse_novel_content(html, aid, cid, url=url)
        content.source = self.source.value
        return lang_convent(content, lang)

    async def fetch_search(self, keyword: str, method: SearchMethod, page: int = 1,
                           lang: Lang = Lang.zh_CN) -> SearchResult:
        keyword = lang_convent(keyword, Lang.zh_CN)  # 站点 GBK 只接受简体输入
        fetcher = await self._ensure_fetcher()
        # 站点硬限制：两次搜索间隔不得少于 5 秒（超了返回错误页）。串行化 + 冷却。
        await self._search_gate()
        url = self._search_url(keyword, method, page, lang)
        resp = await fetcher.get(url)
        html = resp.text if hasattr(resp, "text") else resp.body.decode("gbk", "replace")
        # 识别站点“壳页/关闭公告页”：search.php 未登录/受保护时返回此页而非结果
        if "本站正式关闭" in html or "2009.03.16-2015.12.24" in html:
            from wenku8.exceptions import SourceUnavailableException
            raise SourceUnavailableException(
                self.source.value,
                "web search.php 返回站点壳页（该路径受 CF 保护/需登录），建议改用 api 搜索")
        # 搜索间隔超限的错误页
        if "两次搜索的间隔时间" in html:
            from wenku8.exceptions import RateLimitException
            raise RateLimitException("两次搜索间隔不得少于 5 秒", source=self.source.value)
        # 单个结果时站点会 302 到 .htm 详情页
        if resp.url.endswith(".htm") or resp.status_code == 302:
            m = re.search(r"/(\d+)\.htm", resp.url)
            if m:
                info = await self.fetch_novel_info(int(m.group(1)), lang=lang)
                sr = SearchResult(results=[])
                from wenku8.models import SearchItem, PageControl
                sr.results.append(SearchItem(
                    aid=info.aid, title=info.title, author=info.author, press=info.press,
                    last_updated=info.last_updated,
                    word_count=str(info.word_count) if info.word_count else None,
                    status=info.status, tags=info.tags, intro_preview=info.intro,
                    copyright=info.copyright, animation=info.animation, intro=info.intro))
                sr.page_control = PageControl(now=1, end=1)
                return sr
        result = html_common.parse_search_result(html, url=url)
        return lang_convent(result, lang)

    async def _search_gate(self) -> None:
        """保证两次搜索间隔 >= 5 秒（站点硬限制）。"""
        import asyncio as _asyncio
        import time as _time
        now = _time.monotonic()
        last = getattr(self, "_last_search_at", 0.0)
        wait = 5.0 - (now - last)
        if wait > 0:
            await _asyncio.sleep(wait)
        self._last_search_at = _time.monotonic()

    async def fetch_novel_list(self, sort, page: int = 1,
                               lang: Lang = Lang.zh_CN) -> SearchResult:
        fetcher = await self._ensure_fetcher()
        url = self._toplist_url(sort, page, lang)
        html = await self._page(fetcher, url)
        result = html_common.parse_search_result(html, url=url)
        return lang_convent(result, lang)

    async def fetch_bookshelf(self, classid: int = 0,
                              lang: Lang = Lang.zh_CN) -> list[Book]:
        fetcher = await self._ensure_fetcher()
        url = self._bookcase_url(classid)
        html = await self._page(fetcher, url)
        books = html_common.parse_bookshelf(html, url=url)
        return lang_convent(books, lang)

    async def _page(self, fetcher, url: str) -> str:
        """GET 页面并统一解码（站点为 GBK，浏览器渲染层为 UTF-8）。"""
        resp = await fetcher.get(url)
        body = resp.body
        # httpcloak 在 HTML 场景可能已按 charset 解码为 UTF-8（浏览器层）；先探测
        ctype = (resp.headers.get("content-type") or "").lower()
        if "charset=" in ctype:
            enc = ctype.split("charset=")[-1].split(";")[0].strip().strip('"').strip("'")
            try:
                return body.decode(enc, errors="replace")
            except LookupError:
                pass
        # 启发式：有中文字符的 UTF-8 优先
        try:
            text = body.decode("utf-8")
            # GBK 页面按 UTF-8 解码通常会失败或出现替换符
            if "\ufffd" not in text:
                return text
        except UnicodeDecodeError:
            pass
        try:
            return body.decode("gbk", errors="replace")
        except LookupError:
            return body.decode("utf-8", "replace")

    # ---- 登录 ----
    @property
    def is_logged_in(self) -> bool:
        return bool(self._cookies.get("phpsessid"))

    async def _sync_cookies(self) -> None:
        fetcher = await self._ensure_fetcher()
        http = getattr(fetcher, "_http", None)
        if http is None:
            return
        try:
            cks = await http.cookies()
            for c in cks:
                name = str(getattr(c, "name", "")).lower()
                val = str(getattr(c, "value", ""))
                if name in ("phpsessid", "jieqiuserinfo", "jieqivisitinfo"):
                    self._cookies[name] = val
        except Exception:
            pass

    async def login(self, username: str, password: str,
                    validity: str = "2592000", **kw) -> bool:
        fetcher = await self._ensure_fetcher()
        # 1) 先 GET 登录页建立会话并拿 cookie（实测该页无质询）
        await fetcher.get(f"{self.endpoint}/login.php?do=submit")
        # 2) POST 凭据（含隐藏字段 action=login）
        resp = await fetcher.post(
            f"{self.endpoint}/login.php?do=submit",
            data={"action": "login", "username": username, "password": password,
                  "usecookie": validity, "submit": "登录"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            referer=f"{self.endpoint}/login.php?do=submit",
        )
        await self._sync_cookies()
        # 登录成功与否：PHPSESSID 一定下发；再校验书架可达性过于昂贵，依赖 cookie
        # 判断（旧实现仅用 phpsessid 判定）。
        ok = bool(self._cookies.get("phpsessid"))
        if not ok:
            raise LoginErrorException("Web 登录失败（未获得会话 cookie）",
                                      source=self.source.value)
        return True

    async def logout(self) -> None:
        fetcher = await self._ensure_fetcher()
        try:
            await fetcher.get(f"{self.endpoint}/login.php?action=logout")
        except Exception:
            pass
        self._cookies.clear()

    @property
    def capabilities(self) -> set[Capability]:
        return {Capability.NOVEL_INFO, Capability.NOVEL_INDEX, Capability.NOVEL_CONTENT,
                Capability.SEARCH, Capability.NOVEL_LIST, Capability.BOOKSHELF,
                Capability.LOGIN}

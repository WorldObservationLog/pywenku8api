"""Web（桌面 HTML 版）来源。

端点结构：
- 首页/普通页：  https://www.wenku8.net/
- 详情：        /modules/article/articleinfo.php?id={aid}&charset=gbk
- 目录：        /modules/article/reader.php?aid={aid}&charset=gbk
- 章节：        /modules/article/reader.php?aid={aid}&cid={cid}&charset=gbk
- 搜索：        /modules/article/search.php?searchtype={method}&searchkey={gbk quote}&page={p}
- 排行：        /modules/article/toplist.php?sort={sort}&page={p}&charset=gbk
- 书架：        /modules/article/bookcase.php?classid={bid}
- 书评列表：    /modules/article/reviews.php?aid={aid}&type=all&page={p}
- 书评详情：    /modules/article/reviewshow.php?rid={rid}&page={p}
- 加入书架：    GET  /modules/article/addbookcase.php?bid={aid}
- 移出书架：    GET  /modules/article/bookcase.php?delid={bid}（bid 为书架内 id）
- 推荐：        GET  /modules/article/uservote.php?id={aid}
- 登出：        GET  /logout.php

说明：
- 登录表单在 login.php?do=submit（HTTP 直连实测 200 无质询）；POST 后由
  Set-Cookie 下发 PHPSESSID 与 jieqi* 会话 cookie。
- 发书评/回复为表单 POST（GBK 表单编码）；写操作均需登录。
- 深层 module 页在当前出口会被 Cloudflare Managed Challenge 拦截（详见研究文档），
  因此本来源支持 allow_browser_fallback，交由 Fetcher 切换浏览器通道。
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote, urlencode

from wenku8.consts import Capability, Lang, SearchMethod, Source
from wenku8.exceptions import (
    LoginErrorException, NotLoggedInException, OperationFailedException, PageParseError,
)
from wenku8.models import (
    Book, NovelContent, NovelIndex, NovelInfo, ReviewDetail, ReviewPage, SearchResult,
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
    # 语言策略：请求一律简体 GBK（web 服务端 big5 转换会乱码），
    # 解析后由 lang_convent 转目标语言。
    def _info_url(self, aid: int, lang: Lang) -> str:
        # 保持 lang 参数仅为签名兼容（请求端固定 gbk）
        return f"{self.endpoint}/modules/article/articleinfo.php?id={aid}&charset=gbk"

    def _reader_url(self, aid: int, lang: Lang, cid: Optional[int] = None) -> str:
        q = f"aid={aid}&charset=gbk"
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
                f"&page={page}&charset=gbk")

    def _bookcase_url(self, classid: int = 0) -> str:
        return f"{self.endpoint}/modules/article/bookcase.php?classid={classid}"

    def _reviews_url(self, aid: int, page: int = 1) -> str:
        return (f"{self.endpoint}/modules/article/reviews.php"
                f"?aid={aid}&type=all&page={page}")

    def _reviewshow_url(self, rid: int, page: int = 1) -> str:
        return (f"{self.endpoint}/modules/article/reviewshow.php"
                f"?rid={rid}&page={page}")

    # ---- 数据获取 ----
    # 语言策略：请求一律简体(GBK)。fetch 层只返回简体原文，简繁转换由
    # Wenku8Client 门面统一做（见 client.get_*）——缓存因此可按简体共享。
    async def fetch_novel_info(self, aid: int, lang: Lang = Lang.zh_CN) -> NovelInfo:
        fetcher = await self._ensure_fetcher()
        url = self._info_url(aid, lang)
        html = await self._page(fetcher, url)
        info = html_common.parse_novel_info(html, aid, url=url)
        return info

    async def fetch_novel_intro(self, aid: int, lang: Lang = Lang.zh_CN) -> str:
        """完整简介：网页版无独立接口，取自详情页的简介字段。"""
        info = await self.fetch_novel_info(aid, lang)
        return (info.intro or "").strip()

    async def fetch_novel_bookinfo(self, aid: int, lang: Lang = Lang.zh_CN) -> NovelInfo:
        """列表项信息。网页版无轻量接口，退回完整详情页解析（字段更全）。"""
        return await self.fetch_novel_info(aid, lang)

    async def fetch_novel_cover(self, aid: int) -> bytes:
        """封面图：站点封面 CDN（与桌面版页面使用的地址一致）。"""
        fetcher = await self._ensure_fetcher()
        url = f"https://img.wenku8.com/image/{aid // 1000}/{aid}/{aid}s.jpg"
        resp = await fetcher.get(url, no_cache=True)
        if resp.status_code != 200 or not resp.body:
            raise PageParseError(f"封面下载失败 status={resp.status_code}",
                                 url=url, source=self.source.value)
        return resp.body

    async def fetch_novel_index(self, aid: int, lang: Lang = Lang.zh_CN) -> NovelIndex:
        fetcher = await self._ensure_fetcher()
        url = self._reader_url(aid, lang)
        html = await self._page(fetcher, url)
        index = html_common.parse_novel_index(html, aid, url=url)
        return index

    async def fetch_novel_content(self, aid: int, cid: int,
                                  lang: Lang = Lang.zh_CN) -> NovelContent:
        fetcher = await self._ensure_fetcher()
        url = self._reader_url(aid, lang, cid=cid)
        html = await self._page(fetcher, url)
        content = html_common.parse_novel_content(html, aid, cid, url=url)
        content.source = self.source.value
        return content

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
        return result

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
        return result

    async def fetch_bookshelf(self, classid: int = 0,
                              lang: Lang = Lang.zh_CN) -> list[Book]:
        fetcher = await self._ensure_fetcher()
        url = self._bookcase_url(classid)
        html = await self._page(fetcher, url)
        books = html_common.parse_bookshelf(html, url=url)
        return books

    async def fetch_bookshelf_ids(self, lang: Lang = Lang.zh_CN) -> list[int]:
        """轻量书架：仅 aid 列表（从书架页提取）。"""
        return [b.aid for b in await self.fetch_bookshelf(0, lang)]

    # ---- 书评 ----
    async def fetch_reviews(self, aid: int, page: int = 1,
                            lang: Lang = Lang.zh_CN) -> ReviewPage:
        """书评列表（reviews.php）。"""
        fetcher = await self._ensure_fetcher()
        url = self._reviews_url(aid, page)
        html = await self._page(fetcher, url)
        return html_common.parse_review_list(html, aid, url=url)

    async def fetch_review_detail(self, rid: int, page: int = 1,
                                  lang: Lang = Lang.zh_CN,
                                  aid: int = 0) -> ReviewDetail:
        """书评详情与楼层（reviewshow.php）。"""
        fetcher = await self._ensure_fetcher()
        url = self._reviewshow_url(rid, page)
        html = await self._page(fetcher, url)
        return html_common.parse_review_detail(html, rid, aid, url=url)

    # ---- 写操作（需登录）----
    @staticmethod
    def _gbk_form(fields: dict[str, str]) -> str:
        """表单体按站点 GBK 编码（结果为 ASCII 的 %XX 串）。"""
        return urlencode(fields, encoding="gbk")

    def _require_login(self, action: str) -> None:
        if not self.is_logged_in:
            raise NotLoggedInException(f"{action}需先登录 (web)")

    async def post_review(self, aid: int, title: str, content: str) -> int:
        """发表书评（web 表单仅收正文，title 不参与提交）。成功返回 1。

        web 端 reviews.php 只提交 pcontent 字段；title 参数仅为与 api 源
        保持同一签名，网页端会忽略。
        """
        self._require_login("发表书评")
        fetcher = await self._ensure_fetcher()
        referer = self._reviews_url(aid)
        resp = await fetcher.post(
            f"{self.endpoint}/modules/article/reviews.php?aid={aid}",
            data=self._gbk_form({"pcontent": content}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            referer=referer)
        if resp.status_code != 200:
            raise OperationFailedException(
                f"发表书评失败: HTTP {resp.status_code}", source=self.source.value)
        return 1

    async def post_review_reply(self, rid: int, content: str, aid: int = 0) -> int:
        """回复书评（web 表单 pcontent）。成功返回 1。"""
        self._require_login("回复书评")
        fetcher = await self._ensure_fetcher()
        if not aid:                      # 从详情页表单 action 补齐 aid
            detail_url = self._reviewshow_url(rid)
            html = await self._page(fetcher, detail_url)
            m = re.search(r'reviewshow\.php\?rid=' + str(rid) + r'&(?:amp;)?aid=(\d+)', html)
            if m:
                aid = int(m.group(1))
        url = (f"{self.endpoint}/modules/article/reviewshow.php"
               f"?rid={rid}" + (f"&aid={aid}" if aid else ""))
        resp = await fetcher.post(
            url,
            data=self._gbk_form({"pcontent": content}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            referer=self._reviewshow_url(rid))
        if resp.status_code != 200:
            raise OperationFailedException(
                f"回复书评失败: HTTP {resp.status_code}", source=self.source.value)
        return 1

    async def bookshelf_add(self, aid: int) -> int:
        """加入书架（GET addbookcase.php?bid=）。以书架复核为准，成功返回 1。"""
        self._require_login("加入书架")
        fetcher = await self._ensure_fetcher()
        url = f"{self.endpoint}/modules/article/addbookcase.php?bid={aid}"
        await fetcher.get(url, referer=self._info_url(aid, Lang.zh_CN))
        if aid in [b.aid for b in await self.fetch_bookshelf()]:
            return 1
        raise OperationFailedException("加入书架失败（书架中未出现该书）",
                                       source=self.source.value)

    async def bookshelf_del(self, aid: int) -> int:
        """移出书架（需书架内 bid）。以书架复核为准，成功返回 1。"""
        self._require_login("移出书架")
        bid = None
        for b in await self.fetch_bookshelf():
            if b.aid == aid:
                bid = b.bid
                break
        if bid is None:
            return 7                     # 不在书架（与 api 返回码语义一致）
        fetcher = await self._ensure_fetcher()
        url = f"{self.endpoint}/modules/article/bookcase.php?delid={bid}"
        await fetcher.get(url, referer=self._bookcase_url())
        if aid not in [b.aid for b in await self.fetch_bookshelf()]:
            return 1
        raise OperationFailedException("移出书架失败（仍在书架中）",
                                       source=self.source.value)

    async def vote_novel(self, aid: int) -> int:
        """推荐本书（GET uservote.php?id=）。成功返回 1。"""
        self._require_login("推荐")
        fetcher = await self._ensure_fetcher()
        url = f"{self.endpoint}/modules/article/uservote.php?id={aid}"
        resp = await fetcher.get(url, referer=self._info_url(aid, Lang.zh_CN))
        if resp.status_code != 200:
            raise OperationFailedException(
                f"推荐失败: HTTP {resp.status_code}", source=self.source.value)
        return 1

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
        """登出：请求 /logout.php 销毁服务端会话，再清本地 cookie。"""
        fetcher = await self._ensure_fetcher()
        try:
            await fetcher.get(f"{self.endpoint}/logout.php",
                              referer=f"{self.endpoint}/")
        except Exception:
            pass
        self._cookies.clear()

    @property
    def capabilities(self) -> set[Capability]:
        return {Capability.NOVEL_INFO, Capability.NOVEL_INDEX, Capability.NOVEL_CONTENT,
                Capability.SEARCH, Capability.NOVEL_LIST, Capability.BOOKSHELF,
                Capability.REVIEW, Capability.NOVEL_COVER, Capability.LOGIN}

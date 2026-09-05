"""API 来源：wenku8-relay.mewx.org 中继（轻小说文库 Android 官方 App 同款协议）。

协议（参考本地逆向实现，细节见 wenku8/appver.py 与本地 local_appver_impl.py）：
- 端点：POST https://wenku8-relay.mewx.org/
- body 裸拼接（开头带 '&'，request 值不二次 URL 编码）：
      &appver=<version>-<magic>-<hmac8>
      &request=<base64(action=book&do=meta&aid=123&t=0)>
      &timetoken=<毫秒时间戳>
- appver 不是常量：native 层每 60 秒（分钟窗口）动态计算。**本仓库公开发行版
  不内置该算法**（防滥用，见 wenku8/appver.py）。默认 api 来源不可用；
  使用者自行提供正确实现后即可启用。
- 会话：登录后由服务端 Set-Cookie 下发 PHPSESSID，客户端自动捕获并携带。
- 响应：XML（metadata/package/result）或纯文本/JPEG；mewx_articlelist 为 JSON。
- 写操作/登录返回单个整数文本（1=成功，见 ResultCode）。

注意：本来源为第三方公益中继，可能随上游关闭/加鉴权而失效；它不经过
Cloudflare 质询，但需遵守其自身限速（本项目按宽松但非无限的配额处理）。
"""
from __future__ import annotations

import base64
import time
from typing import Optional
from urllib.parse import quote

from wenku8.appver import AppverProvider, normalize_provider
from wenku8.consts import Capability, Lang, SearchMethod, Source
from wenku8.exceptions import (
    CloudflareChallengeException, LoginErrorException, OperationFailedException, PageParseError,
    SourceUnavailableException,
)
from wenku8.fetcher.http import HttpResponse
from wenku8.models import (
    Book, Chapter, NovelContent, NovelIndex, NovelInfo, PageControl, SearchItem,
    SearchResult, Volume,
)
from wenku8.sources.base import BaseSource

RELAY_ENDPOINT = "https://wenku8-relay.mewx.org/"

# 版本常量（仅作协议标注；具体 magic/HMAC 细节见本地实现）
APP_VERSION = "1.30"
# 返回码（服务端纯数字文本）
class ResultCode:
    REQUEST_ERROR = 0
    SUCCEEDED = 1
    ERROR_USERNAME = 2
    ERROR_PASSWORD = 3
    NOT_LOGGED_IN = 4
    ALREADY_IN_BOOKSHELF = 5
    BOOKSHELF_FULL = 6
    NOVEL_NOT_IN_BOOKSHELF = 7
    TOPIC_NOT_EXIST = 8
    SIGN_FAILED = 9
    RECOMMEND_FAILED = 10
    POST_FAILED = 11
    REFER_PAGE_0 = 22


_RESULT_TEXT: dict[int, str] = {
    0: "请求错误", 1: "成功", 2: "用户名错误", 3: "密码错误", 4: "未登录",
    5: "已在书架中", 6: "书架已满", 7: "不在书架中", 8: "主题不存在",
    9: "签名失败", 10: "推荐失败", 11: "发帖失败", 22: "引用页为 0",
}


def _result_text(code: int) -> str:
    return _RESULT_TEXT.get(code, f"未知返回码 {code}")


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _lang_t(lang: Lang) -> int:
    return 1 if lang == Lang.zh_TW else 0


def _review_t(lang: Lang) -> str:
    return "TC" if lang == Lang.zh_TW else "SC"


class ApiRelaySource(BaseSource):
    """MewX relay 数据源。

    appver 计算依赖外部 provider（见 wenku8/appver.py）：公开发行版默认
    不可用（空实现），需使用者注入正确实现后 api 来源才工作。
    """

    source = Source.api
    # 每 item 的短信息是一次单独请求；控制搜索富化的最大条数
    MAX_SEARCH_ENRICH = 20

    def __init__(self, endpoint: str = RELAY_ENDPOINT,
                 appver: Optional[str] = None,
                 appver_provider=None,
                 version: str = APP_VERSION, **kwargs):
        super().__init__(**kwargs)
        self.endpoint = endpoint
        self._fixed_appver = appver
        self.version = version
        # 归一化 provider：函数 / AppverProvider 子类 / None → 默认(空实现或本地)
        self._appver_fn = normalize_provider(appver_provider)

    @property
    def appver(self) -> str:
        """当前 appver。固定 appver= 优先；否则调 provider 现算。

        返回空串表示 provider 未提供实现（api 来源不可用）。
        """
        if self._fixed_appver:
            return self._fixed_appver
        try:
            return self._appver_fn(self.version, None) or ""
        except Exception:
            return ""

    @property
    def appver_available(self) -> bool:
        """provider 是否可算出有效 appver（非空）。"""
        return bool(self._fixed_appver or self.appver)

    # ---- 内部 ----
    def _command(self, cmd: str) -> str:
        """构造与 App 完全一致的 POST body（裸拼接，request 不二次编码）。"""
        return (
            f"&appver={self.appver}"
            f"&request={_b64(cmd)}"
            f"&timetoken={int(time.time() * 1000)}"
        )

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept-Encoding": "gzip",
            "Accept": "text/xml, application/xml, text/plain, */*",
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 15; MewX-Wenku8/1.30.73) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/120.0.0.0"
            ),
        }
        if self._cookies.get("phpsessid"):
            h["Cookie"] = f"PHPSESSID={self._cookies['phpsessid']}"
        return h

    def _check_body(self, body: bytes) -> None:
        head = (body or b"")[:64].strip()
        if head in (b"Bad request.", b"Something went wrong", b"Bad Request") \
                or head.startswith(b"Bad request"):
            # appver 过旧/协议变更 → 非瞬时，抛 SourceUnavailableException 让父类换源
            raise SourceUnavailableException(
                self.source.value, "relay 拒绝请求（appver 过旧或协议变更）")

    async def _post(self, cmd: str, *, need_login: bool = False) -> HttpResponse:
        # 公开发行版默认无 appver 实现 → api 来源不可用，明确抛错让父类 fallback
        if not self.appver_available:
            raise SourceUnavailableException(
                self.source.value,
                "api 来源默认不可用：未提供 appver 实现。"
                "请传入 appver_provider（见 wenku8/appver.py 文档）或本地保留 "
                "wenku8/local_appver_impl.py")
        fetcher = await self._ensure_fetcher()
        resp = await fetcher.post_raw(
            self.endpoint,
            data=self._command(cmd),
            headers=self._headers(),
        )
        if resp.status_code != 200:
            raise PageParseError(f"relay 非 200: {resp.status_code}", page=resp.text,
                                 url=self.endpoint, source=self.source.value)
        self._check_body(resp.body)
        # 捕获 PHPSESSID（登录后 Set-Cookie 下发）
        self._capture_session(resp)
        return resp

    def _capture_session(self, resp: HttpResponse) -> None:
        set_cookie = resp.headers.get("set-cookie")
        if not set_cookie:
            return
        # httpcloak 的 headers 值可能是 list（多值头），统一成 str
        if isinstance(set_cookie, list):
            set_cookie = "; ".join(str(x) for x in set_cookie)
        for part in set_cookie.split(","):
            part = part.strip()
            if part.lower().startswith("phpsessid="):
                val = part[len("phpsessid="):].split(";")[0].strip()
                if val:
                    self._cookies["phpsessid"] = val
                    return

    @staticmethod
    def _decode_xml_text(body: bytes) -> str:
        """relay 响应多为 UTF-8；GBK 出现时做兜底。"""
        for enc in ("utf-8", "gbk"):
            try:
                return body.decode(enc)
            except UnicodeDecodeError:
                continue
        return body.decode("utf-8", "replace")

    # ---- 数据获取 ----
    async def fetch_novel_intro(self, aid: int, lang: Lang = Lang.zh_CN) -> str:
        """获取完整简介纯文本（action=book&do=intro）。"""
        resp = await self._post(f"action=book&do=intro&aid={aid}&t={lang.api_t}")
        return self._decode_xml_text(resp.body).strip()

    async def fetch_novel_info(self, aid: int, lang: Lang = Lang.zh_CN) -> NovelInfo:
        """meta（完整元数据）+ intro（简介）两次请求合成 NovelInfo。"""
        meta_resp = await self._post(f"action=book&do=meta&aid={aid}&t={lang.api_t}")
        meta_text = self._decode_xml_text(meta_resp.body)
        info = self._parse_meta(meta_text, aid)
        if not info.intro:
            try:
                info.intro = await self.fetch_novel_intro(aid, lang=lang)
            except Exception:
                pass  # intro 缺失不致命
        return info

    async def fetch_novel_index(self, aid: int, lang: Lang = Lang.zh_CN) -> NovelIndex:
        resp = await self._post(f"action=book&do=list&aid={aid}&t={lang.api_t}")
        xml = self._decode_xml_text(resp.body)
        return self._parse_index(xml, aid)

    async def fetch_novel_content(self, aid: int, cid: int,
                                  lang: Lang = Lang.zh_CN) -> NovelContent:
        resp = await self._post(f"action=book&do=text&aid={aid}&cid={cid}&t={lang.api_t}")
        text = self._decode_xml_text(resp.body)
        # 解析插图占位与标题头
        images: list[str] = []
        out: list[str] = []
        for line in text.splitlines():
            if line.startswith("<!--image-->") and line.endswith("<!--image-->"):
                img_url = line[len("<!--image-->"):-len("<!--image-->")]
                images.append(img_url)
                out.append(line)
            else:
                out.append(line)
        title = ""
        for line in text.splitlines()[:5]:
            if line.strip() and not line.startswith("<!--image-->"):
                title = line.strip()
                break
        return NovelContent(aid=aid, cid=cid, title=title,
                            text="\n".join(out).strip(),
                            images=images, source=self.source.value)

    async def fetch_search(self, keyword: str, method: SearchMethod,
                           page: int = 1, lang: Lang = Lang.zh_CN) -> SearchResult:
        """搜索：MewX 中继返回富化 XML（每条自带 Title/Author/LastUpdate/Tags/
        IntroPreview），无需逐本二次请求。"""
        st = method.value
        resp = await self._post(
            f"action=search&searchtype={st}&searchkey={quote(keyword)}&t={lang.api_t}")
        text = self._decode_xml_text(resp.body).lstrip()
        if text.startswith("<result>"):
            page_end, items = self._parse_search_result_xml(text)
        else:
            # 兜底：仅 aid 列表 → 逐本富化（上限保护）
            aids = self._parse_aid_list(text)
            items = []
            for i, aid in enumerate(aids[:self.MAX_SEARCH_ENRICH]):
                try:
                    info = await self.fetch_novel_info(aid, lang=lang)
                    items.append(SearchItem(
                        aid=aid, title=info.title, author=info.author, press=info.press,
                        last_updated=info.last_updated,
                        word_count=str(info.word_count) if info.word_count else None,
                        status=info.status, tags=info.tags, intro_preview=info.intro[:120],
                        copyright=info.copyright, animation=info.animation, intro=info.intro))
                except Exception:
                    items.append(SearchItem(aid=aid))
            return SearchResult(results=items, page_control=PageControl(now=1, end=1))
        return SearchResult(results=items,
                            page_control=PageControl(now=1, end=page_end or 1))

    @classmethod
    def _parse_search_result_xml(cls, xml: str) -> tuple[int, list[SearchItem]]:
        """富化搜索 XML：<result><page num/><item aid=..> <data/>...</item></result>"""
        from lxml import etree
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception as e:
            raise PageParseError(f"search XML 解析失败: {e}", page=xml, source=Source.api)
        items: list[SearchItem] = []
        page_end = 1
        page_node = root.xpath(".//page")
        if page_node and page_node[0].get("num"):
            try:
                page_end = int(page_node[0].get("num"))
            except ValueError:
                pass
        for it in root.xpath(".//item"):
            aid_v = it.get("aid")
            if not aid_v or not aid_v.isdigit():
                continue
            fields: dict[str, str] = {}
            title = ""
            intro_preview = ""
            for d in it.xpath("./data"):
                nm = d.get("name") or ""
                if nm == "Title":
                    title = (d.text or "").strip()
                elif nm == "IntroPreview":
                    intro_preview = (d.text or "").strip()
                else:
                    fields[nm] = d.get("value") or (d.text or "").strip()
            items.append(SearchItem(
                aid=int(aid_v),
                title=title,
                author=fields.get("Author", ""),
                press=fields.get("PressId", ""),
                last_updated=fields.get("LastUpdate"),
                word_count=fields.get("BookLength") or fields.get("TotalHitsCount"),
                status=fields.get("BookStatus", ""),
                tags=(fields.get("Tags") or "").split(),
                intro_preview=intro_preview,
                copyright=True, animation=False))
        return page_end, items

    async def fetch_novel_list(self, sort: str, page: int = 1,
                               lang: Lang = Lang.zh_CN) -> SearchResult:
        """排行榜/列表。

        新版中继用 action=mewx_articlelist（返回 JSON，带分页与完整信息）；
        旧版 action=novellist 返回 XML/JSON。先试新版，失败退旧版。
        """
        # 新版 JSON（实测可用，cache HIT 响应快）
        try:
            resp = await self._post(
                f"action=mewx_articlelist&sort={sort}&page={page}&t={lang.api_t}")
            text = self._decode_xml_text(resp.body).lstrip()
            if text.startswith("{"):
                page_end, items = self._parse_novellist_json(text)
                return SearchResult(results=items,
                                    page_control=PageControl(now=page, end=page_end))
            if text.startswith("<"):
                page_end, items = self._parse_novellist_xml(text)
                return SearchResult(results=items,
                                    page_control=PageControl(now=page, end=page_end))
        except Exception:
            pass
        # 旧版回退
        resp = await self._post(f"action=novellist&sort={sort}&page={page}&t={lang.api_t}")
        text = self._decode_xml_text(resp.body).lstrip()
        if text.startswith("{"):
            page_end, items = self._parse_novellist_json(text)
        elif text.startswith("<"):
            page_end, items = self._parse_novellist_xml(text)
        else:
            raise PageParseError("novellist 响应无法识别", page=text,
                                 url=self.endpoint, source=self.source.value)
        return SearchResult(results=items,
                            page_control=PageControl(now=page, end=page_end))

    async def fetch_bookshelf(self, lang: Lang = Lang.zh_CN) -> list[Book]:
        if not self.is_logged_in:
            return []
        resp = await self._post(f"action=bookcase&t={lang.api_t}")
        xml = self._decode_xml_text(resp.body)
        return self._parse_bookshelf(xml)

    # ---- 写操作（需登录；服务端返回纯数字码，1=成功） ----
    async def bookshelf_add(self, aid: int) -> int:
        """加入书架。返回服务端码：1=成功；5=已在书架（视为成功）。"""
        if not self.is_logged_in:
            from wenku8.exceptions import NotLoggedInException
            raise NotLoggedInException("加入书架需先登录 (api)")
        resp = await self._post(f"action=bookcase&do=add&aid={aid}")
        code = self._parse_code(resp)
        if code not in (ResultCode.SUCCEEDED, ResultCode.ALREADY_IN_BOOKSHELF):
            raise OperationFailedException(
                f"加入书架失败: 返回码 {code} ({_result_text(code)})",
                code=code, source=self.source.value)
        return code

    async def bookshelf_del(self, aid: int) -> int:
        """移出书架。1=成功；7=不在书架 / 4=未登录（视为已完成）。"""
        if not self.is_logged_in:
            from wenku8.exceptions import NotLoggedInException
            raise NotLoggedInException("移出书架需先登录 (api)")
        resp = await self._post(f"action=bookcase&do=del&aid={aid}")
        code = self._parse_code(resp)
        if code not in (ResultCode.SUCCEEDED, ResultCode.NOVEL_NOT_IN_BOOKSHELF,
                        ResultCode.NOT_LOGGED_IN):
            raise OperationFailedException(
                f"移出书架失败: 返回码 {code} ({_result_text(code)})",
                code=code, source=self.source.value)
        return code

    async def vote_novel(self, aid: int) -> int:
        """推荐小说（App 日限 5 次）。返回码 1=成功；10=推荐失败（已满）。"""
        if not self.is_logged_in:
            from wenku8.exceptions import NotLoggedInException
            raise NotLoggedInException("推荐需先登录 (api)")
        resp = await self._post(f"action=book&do=vote&aid={aid}")
        code = self._parse_code(resp)
        if code != ResultCode.SUCCEEDED:
            raise OperationFailedException(
                f"推荐失败: 返回码 {code} ({_result_text(code)})",
                code=code, source=self.source.value)
        return code

    def _parse_code(self, resp: HttpResponse) -> int:
        """写操作响应应为纯数字文本；非数字按协议错误处理。"""
        text = self._decode_xml_text(resp.body).strip()
        if not text.isdigit():
            raise PageParseError("写操作响应非数字返回码", page=text,
                                 url=self.endpoint, source=self.source.value)
        return int(text)

    # ---- 登录 ----
    @property
    def is_logged_in(self) -> bool:
        # relay 会话 = 该域下的 PHPSESSID cookie
        return bool(self._cookies.get("phpsessid"))

    async def login(self, username: str, password: str, **kw) -> bool:
        """登录。含 @ 视为邮箱（action=loginemail）；用户名/密码 UTF-8 URL 编码。
        成功返回码 1；PHPSESSID 由 Set-Cookie 自动捕获（_post → _capture_session）。"""
        u = quote(username, safe="", encoding="utf-8")
        p = quote(password, safe="", encoding="utf-8")
        action = "loginemail" if "@" in username else "login"
        resp = await self._post(f"action={action}&username={u}&password={p}")
        body = self._decode_xml_text(resp.body).strip()
        if body == "1":
            return True
        code = int(body) if body.isdigit() else None
        raise LoginErrorException("relay 登录失败", code=code, source=self.source.value)

    async def logout(self) -> None:
        try:
            await self._post("action=logout")
        except Exception:
            pass
        self._cookies.clear()

    # ---- 封面（relay 高清） ----
    async def fetch_novel_cover(self, aid: int) -> bytes:
        """高清封面 JPEG（action=book&do=cover）。"""
        body = await self._binary_post(f"action=book&do=cover&aid={aid}")
        return body

    # ---- 列表项短信息（action=book&do=bookinfo）----
    async def fetch_novel_bookinfo(self, aid: int, lang: Lang = Lang.zh_CN) -> "NovelInfo":
        """bookinfo：与 meta 同构但仅基础字段（单请求、比 meta 轻）。
        返回 NovelInfo（标题/作者/状态/更新 已填，统计字段为空）。"""
        resp = await self._post(f"action=book&do=bookinfo&aid={aid}&t={lang.api_t}")
        xml = self._decode_xml_text(resp.body)
        return self._parse_meta(xml, aid)

    async def _binary_post(self, cmd: str) -> bytes:
        """POST 并校验 JPEG 二进制。httpcloak 存在对小 JPEG 响应做 UTF-8 解码
        损坏的缺陷（0xFF→U+FFFD），故 magic 校验失败时降级用 requests
        （带同一 PHPSESSID）重试，保证返回原始字节。"""
        resp = await self._post(cmd)
        if resp.body and resp.body[:2] == b"\xff\xd8":
            return resp.body
        # 降级：requests 直发（relay 无 CF，仅需 cookie/UA/appver）
        try:
            import requests
            body = self._command(cmd)
            hdr = {"Accept-Encoding": "gzip",
                   "User-Agent": self._headers().get("User-Agent", "")}
            sid = self._cookies.get("phpsessid")
            if sid:
                hdr["Cookie"] = f"PHPSESSID={sid}"
            r = requests.post(self.endpoint, data=body.encode("utf-8"),
                              headers=hdr, timeout=30)
            if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
                return r.content
            # 仍失败 → 明确错误
            from wenku8.exceptions import PageParseError as _PE
            raise _PE("relay 二进制响应校验失败", page=(r.text or "")[:200] if r.status_code != 200 else repr(r.content[:40]),
                      url=self.endpoint, source=self.source.value)
        except ImportError:
            raise PageParseError("relay 二进制响应校验失败(且无 requests 可降级)",
                                 page=repr(resp.body[:60]), url=self.endpoint,
                                 source=self.source.value)

    # ---- 解析 ----
    @classmethod
    def _parse_meta(cls, xml: str, aid: int) -> NovelInfo:
        from lxml import etree
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception as e:
            raise PageParseError(f"meta XML 解析失败: {e}", page=xml, source=Source.api)
        info = NovelInfo(aid=aid)
        for data in root.xpath(".//data"):
            name = data.get("name") or ""
            if name == "Title":
                info.title = (data.text or "").strip()
            elif name == "Author":
                info.author = data.get("value") or ""
            elif name == "DayHitsCount":
                info.day_hits = cls._to_int(data.get("value"))
            elif name == "TotalHitsCount":
                info.total_hits = cls._to_int(data.get("value"))
            elif name == "PushCount":
                info.push_count = cls._to_int(data.get("value"))
            elif name == "FavCount":
                info.fav_count = cls._to_int(data.get("value"))
            elif name == "PressId":
                info.press = data.get("value") or ""
                info.press_id = cls._to_int(data.get("sid"))
            elif name == "BookStatus":
                info.status = data.get("value") or ""
            elif name == "BookLength":
                info.book_length = cls._to_int(data.get("value"))
                info.word_count = info.book_length  # 单位可能不同，保留原值
            elif name == "LastUpdate":
                info.last_updated = data.get("value")
            elif name == "Tags":
                info.tags = [t for t in (data.get("value") or "").split() if t]
            elif name == "LatestSection":
                info.latest_section = (data.text or "").strip()
                info.latest_section_cid = cls._to_int(data.get("cid"))
        if not info.title:
            raise PageParseError("meta 缺少 Title", page=xml, source=Source.api)
        return info

    @classmethod
    def _parse_index(cls, xml: str, aid: int) -> NovelIndex:
        from lxml import etree
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception as e:
            raise PageParseError(f"index XML 解析失败: {e}", page=xml, source=Source.api)
        volumes: list[Volume] = []
        title = ""
        for vol in root.xpath(".//volume"):
            v = Volume(vid=cls._to_int(vol.get("vid")) or 0,
                       title=(vol.text or "").strip() or "")
            for ch in vol.xpath("./chapter"):
                v.chapters.append(Chapter(cid=cls._to_int(ch.get("cid")) or 0,
                                          title=(ch.text or "").strip()))
            if not v.title and v.chapters:
                title = title or v.chapters[0].title
            volumes.append(v)
        if not volumes:
            raise PageParseError("index 缺少 volume", page=xml, source=Source.api)
        return NovelIndex(aid=aid, title=title, volumes=volumes)

    @staticmethod
    def _parse_aid_list(xml: str) -> list[int]:
        from lxml import etree
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception as e:
            raise PageParseError(f"aid list XML 解析失败: {e}", page=xml, source=Source.api)
        aids = []
        for it in root.xpath(".//item"):
            v = it.get("aid")
            if v and v.isdigit():
                aids.append(int(v))
        return aids

    @classmethod
    def _parse_novellist_xml(cls, xml: str) -> tuple[int, list[SearchItem]]:
        """action=novellist XML：<result><page num='N'/><item aid=..> <data .../> ..."""
        from lxml import etree
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception as e:
            raise PageParseError(f"novellist XML 解析失败: {e}", page=xml, source=Source.api)
        page_node = root.xpath(".//page")
        page_end = int(page_node[0].get("num")) if page_node and page_node[0].get("num") else 1
        items: list[SearchItem] = []
        for it in root.xpath(".//item"):
            aid_v = it.get("aid")
            if not aid_v or not aid_v.isdigit():
                continue
            fields: dict[str, str] = {}
            for d in it.xpath("./data"):
                fields[d.get("name") or ""] = d.get("value") or (d.text or "")
            items.append(SearchItem(
                aid=int(aid_v),
                title=fields.get("Title", ""),
                author=fields.get("Author", ""),
                press=fields.get("PressId", ""),
                last_updated=fields.get("LastUpdate"),
                word_count=fields.get("BookLength") or fields.get("TotalHitsCount"),
                status=fields.get("BookStatus", ""),
                tags=fields.get("Tags", "").split(),
                intro_preview=fields.get("IntroPreview", ""),
                copyright=True, animation=False))
        return page_end, items

    @classmethod
    def _parse_novellist_json(cls, text: str) -> tuple[int, list[SearchItem]]:
        import json
        try:
            data = json.loads(text)
        except Exception as e:
            raise PageParseError(f"novellist JSON 解析失败: {e}", page=text, source=Source.api)
        items: list[SearchItem] = []
        for it in data.get("items", []):
            items.append(SearchItem(
                aid=int(it.get("aid", 0)),
                title=it.get("Title", ""),
                author=it.get("Author", ""),
                press=it.get("PressId", ""),
                last_updated=it.get("LastUpdate"),
                word_count=it.get("BookLength") or it.get("TotalHitsCount"),
                status=it.get("BookStatus", ""),
                tags=(it.get("Tags") or "").split(),
                intro_preview=it.get("IntroPreview", ""),
                copyright=True, animation=False))
        return int(data.get("page_num", 1)), items

    @classmethod
    def _parse_bookshelf(cls, xml: str) -> list[Book]:
        from lxml import etree
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception as e:
            raise PageParseError(f"bookshelf XML 解析失败: {e}", page=xml, source=Source.api)
        books: list[Book] = []
        for book in root.xpath(".//book"):
            aid_v = book.get("aid")
            if not aid_v or not aid_v.isdigit():
                continue
            latest_section = None
            latest_cid = None
            name = ""
            for ch in book.xpath(".//chapter"):
                latest_section = (ch.text or "").strip()
                latest_cid = cls._to_int(ch.get("cid"))
            for nm in book.xpath(".//name"):
                name = (nm.text or "").strip()
            books.append(Book(
                aid=int(aid_v),
                title=name,
                latest_section=latest_section,
                latest_section_cid=latest_cid,
                add_date=book.get("date")))
        return books

    @staticmethod
    def _to_int(v) -> Optional[int]:
        if v is None:
            return None
        try:
            return int(str(v).strip())
        except (ValueError, TypeError):
            return None

    # ---- 能力 ----
    @property
    def capabilities(self) -> set[Capability]:
        # relay 支持高清封面（action=book&do=cover）与索引/正文/搜索/书架；
        # 无整本 TXT 下载（NOVEL_FULL 走 CDN 源），故不声明。
        return {Capability.NOVEL_INFO, Capability.NOVEL_INDEX, Capability.NOVEL_CONTENT,
                Capability.NOVEL_COVER, Capability.SEARCH,
                Capability.NOVEL_LIST, Capability.BOOKSHELF, Capability.LOGIN}

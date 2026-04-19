import asyncio
import functools
import re
from urllib.parse import quote

import httpx
import lxml.html
from httpx_curl_cffi import AsyncCurlTransport, CurlOpt
from lxml import etree

from wenku8.consts import LoginValidity, Lang, SearchMethod, NovelSortMethod
from wenku8.exceptions import NotLoggedInException, RateLimitException
from wenku8.models import NovelInfo, _Volume, _Chapter, NovelIndex, SearchItem, SearchResult, PageControl, BookshelfItem
from wenku8.utils import extract_text, cooldown, separate_chinese_colon, get_chapter_content, lang_convent
from wenku8.cache import with_cache


def login_required(func):
    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        if self.session.cookies.get("PHPSESSID") is None:
            raise NotLoggedInException
        return await func(self, *args, **kwargs)

    return wrapper


class Wenku8API:
    ENDPOINT = "https://www.wenku8.net"
    session: httpx.AsyncClient

    def __init__(self, endpoint: str = "https://www.wenku8.net", enable_cache: bool = False,
                 cache_db_path: str = ".wenku8_cache.db"):
        self.ENDPOINT = endpoint
        self.enable_cache = enable_cache
        self.cache_db_path = cache_db_path
        self.session = httpx.AsyncClient(transport=AsyncCurlTransport(
            impersonate="chrome",
            default_headers=True,
            curl_options={CurlOpt.FRESH_CONNECT: True}
        ), follow_redirects=True)
        from .cache import CacheDaemon
        self.cache_daemon = CacheDaemon(self)

    @functools.wraps(httpx.AsyncClient.request)
    async def _request(self, *args, **kwargs):
        cf_bypassed_in_this_request = kwargs.pop('_cf_bypassed', False)
        try:
            result = await self.session.request(*args, **kwargs)
            result.raise_for_status()
            return result
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (403, 503) and not cf_bypassed_in_this_request:
                url = args[1] if len(args) > 1 else kwargs.get('url')
                if url:
                    try:
                        await self.bypass_cloudflare(str(url))
                    except Exception:
                        pass
                    kwargs['_cf_bypassed'] = True
                    return await self._request(*args, **kwargs)

            if e.response.status_code == 429 or e.response.status_code == 403:
                raise RateLimitException
            else:
                raise e

    async def login(self, username: str, password: str, validity: LoginValidity = LoginValidity.NONE) -> str:
        form_data = {
            "username": username,
            "password": password,
            "usercookie": validity,
            "action": "login",
            "submit": "%26%23160%3B%B5%C7%26%23160%3B%26%23160%3B%C2%BC%26%23160%3B"
        }
        resp = await self._request("POST", self.ENDPOINT + "/login.php", data=form_data)
        return resp.cookies.get("PHPSESSID")

    @property
    def is_logged_in(self):
        return bool(self.session.cookies.get("PHPSESSID"))

    async def bypass_cloudflare(self, url: str, timeout: float = 30.0, headless: bool = True, proxy: str = None):
        """
        通过运行本地无头浏览器获取 Cloudflare 的 cf_clearance Cookie 和真实的 User-Agent，
        并将其更新回当前的 HTTPX 会话中。以解决 403 / 503 等质询问题。
        """
        from .cf_solver import get_cloudflare_clearance
        user_agent, cookies = await get_cloudflare_clearance(
            url=url,
            timeout=timeout,
            headless=headless,
            proxy=proxy
        )
        self.session.headers.update({"User-Agent": user_agent})
        self.session.cookies.update(cookies)

    @with_cache(expires_days=None)
    async def get_novel_cover(self, aid: int):
        resp = await self._request("GET", f"https://img.wenku8.com/image/{int(aid) // 1000}/{aid}/{aid}s.jpg")
        return resp.content

    @login_required
    @with_cache(expires_days=None)
    async def get_novel_info(self, aid: int, lang: Lang = Lang.zh_CN) -> NovelInfo:
        resp = await self._request("GET", self.ENDPOINT + f"/modules/article/articleinfo.php?id={aid}&charset=gbk")
        resp.encoding = "gbk"
        parser = etree.HTML(resp.text)

        if bool(len(parser.xpath('//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[2]/b/br'))):
            last_updated = None
            word_count = None
            popularity_level = None
            trending_level = None
            latest_section = None
            intro = "".join(parser.xpath('//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[4]//text()'))
        else:
            last_updated = extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[2]/td[4]', True)
            word_count = int(
                extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[2]/td[5]', True).replace("字", ""))
            popularity_level = separate_chinese_colon(
                extract_text(parser, '//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[2]/b').split("，")[0])[1]
            trending_level = separate_chinese_colon(
                extract_text(parser, '//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[2]/b').split("，")[1])[1]
            latest_section = extract_text(parser, '//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[4]/a')
            intro = "".join(parser.xpath('//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[6]//text()'))

        return lang_convent(NovelInfo(
            aid=aid,
            title=extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[1]/td/table/tr/td[1]/span/b'),
            author=extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[2]/td[2]', True),
            status=extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[2]/td[3]', True),
            last_updated=last_updated,
            intro=intro,
            tags=extract_text(parser, '//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[1]/b', True).split(" "),
            press=extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[2]/td[1]', True),
            word_count=word_count,
            popularity_level=popularity_level,
            trending_level=trending_level,
            latest_section=latest_section,
            copyright=not bool(len(parser.xpath('//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[2]/b/br'))),
            animation=bool(len(parser.xpath('//*[@id="content"]/div[1]/table[2]/tr/td[1]/span/b')))
        ), lang)

    @login_required
    @with_cache(expires_days=None)
    async def get_novel_index(self, aid: int, lang: Lang = Lang.zh_CN) -> NovelIndex:
        resp = await self._request("GET", self.ENDPOINT + f"/modules/article/reader.php?aid={aid}&charset=gbk")
        resp.encoding = "gbk"
        parser = etree.HTML(resp.text)
        volumes = []
        current_vol = None
        xpath_str = '//table[@class="css"]//td[@class="vcss" or @class="ccss"]'
        for td in parser.xpath(xpath_str):
            cls = td.get("class")
            if cls == "vcss":
                if current_vol:
                    volumes.append(current_vol)
                current_vol = _Volume(
                    vid=int(td.get("vid")),
                    title=td.text.strip() if td.text else "",
                    chapters=[]
                )
            elif cls == "ccss":
                if not current_vol:
                    continue
                link = td.find("a")
                if link is None:
                    continue
                href = link.get("href")
                cid = int(re.search(r'cid=(\d+)', href).group(1))
                current_vol.chapters.append(_Chapter(cid=cid, title=link.text))
        if current_vol:
            volumes.append(current_vol)
        return lang_convent(NovelIndex(aid=aid,
                                       title=extract_text(parser, '//*[@id="title"]'),
                                       author=extract_text(parser, '//*[@id="info"]', True),
                                       volumes=volumes), lang)

    @login_required
    @with_cache(expires_days=None)
    async def get_novel_content(self, aid: int, cid: int, lang: Lang = Lang.zh_CN) -> str:
        resp = await self._request("GET",
                                   self.ENDPOINT + f"/modules/article/reader.php?aid={aid}&cid={cid}&charset=gbk")
        resp.encoding = "gbk"
        parser = etree.HTML(resp.text)
        results = []
        for child in parser.xpath('//*[@id="content"]')[0]:
            if child.tag == 'div':
                href = child[0].get('href')
                results.append(f"<!--image-->{href}<!--image-->")
            else:
                pass
            if child.tail:
                results.append(child.tail)

        return lang_convent("".join(results), lang)

    @with_cache(expires_days=None)
    async def get_full_novel_content(self, aid: int, lang: Lang = Lang.zh_CN) -> str:
        resp = await self._request("GET", f"https://dl.wenku8.com/down.php?type=utf8&node=1&id={aid}")
        return lang_convent(resp.text, lang)

    @login_required
    @with_cache(expires_days=None)
    async def get_novel_content_via_full(self, aid: int, cid: int, lang: Lang = Lang.zh_CN) -> str:
        full_content = await self.get_full_novel_content(aid, lang)
        novel_index = await self.get_novel_index(aid, lang)
        return get_chapter_content(full_content, novel_index, cid)

    def _search_page_parser(self, parser: lxml.html.Element):
        results = []
        for novel in parser.xpath('//*[@id="content"]/table/tr/td')[0]:
            if len(novel[1][2].text.split("/")) < 3:
                # 版权本，没有最近更新和字数
                last_updated = None
                word_count = None
                status = novel[1][2].text.split("/")[0]
                animation = len(novel[1][2].text.split("/")) == 2
            else:
                last_updated = novel[1][2].text.split("/")[0].split(":")[1]
                word_count = novel[1][2].text.split("/")[1].split(":")[1]
                status = novel[1][2].text.split("/")[2]
                animation = len(novel[1][2].text.split("/")) == 4

            # Wenku8 繁体网页版似乎存在编码问题，部分字符无法正常以 Big5 显示
            # see also: https://www.wenku8.net/modules/article/articleinfo.php?id=4093&charset=big5
            if "/" in novel[1][1].text:
                press = novel[1][1].text.split("/")[1].split(":")[1]
            else:
                press = novel[1][1].text.split("  ")[1].split(":")[1]

            results.append(SearchItem(aid=re.search(r'(\d+).htm', novel[1][0][0].get("href")).group(1),
                                      title=novel[1][0][0].get("title"),
                                      author=novel[1][1].text.split("/")[0].split(":")[1],
                                      press=press,
                                      last_updated=last_updated,
                                      word_count=word_count,
                                      status=status,
                                      tags=novel[1][3][0].text.split(" "),
                                      intro_preview=novel[1][4].text.split(":", maxsplit=1)[1],
                                      copyright=not novel[1][5].get("class") == "hottext",
                                      animation=animation
                                      ))

        page_control_str = parser.xpath('//*[@id="pagestats"]')[0].text
        return SearchResult(results=results, page_control=PageControl.from_str(page_control_str))

    @login_required
    @cooldown(5)
    @with_cache(expires_days=3)
    async def search_novel(self, keyword: str, method: SearchMethod, page: int = 1,
                           lang: Lang = Lang.zh_CN) -> SearchResult:
        keyword = lang_convent(keyword, Lang.zh_CN)
        resp = await self._request("GET",
                                   self.ENDPOINT + f"/modules/article/search.php?searchtype={method}&searchkey={quote(keyword.encode('gbk'))}&page={page}")
        resp.encoding = "gbk"
        if str(resp.url).endswith(".htm"):  # 只有一个结果时会跳转到对应的页面
            info = await self.get_novel_info(re.search(r"(\d*).htm", str(resp.url)).group(1), lang=lang)
            return lang_convent(SearchResult(
                results=[SearchItem(aid=info.aid, title=info.title, author=info.author, press=info.press,
                                    last_updated=info.last_updated, word_count=info.word_count,
                                    status=info.status, tags=info.tags, intro_preview=info.intro,
                                    copyright=info.copyright, animation=info.animation)],
                page_control=PageControl(now=1, previous=1, next=1, begin=1, end=1)), lang)
        else:
            parser = etree.HTML(resp.text)
            return lang_convent(self._search_page_parser(parser), lang)

    async def search_novel_by_name(self, keyword: str, page: int = 1, lang: Lang = Lang.zh_CN):
        return await self.search_novel(keyword, SearchMethod.NAME, page, lang)

    async def search_novel_by_author(self, keyword: str, page: int = 1, lang: Lang = Lang.zh_CN):
        return await self.search_novel(keyword, SearchMethod.AUTHOR, page, lang)

    @with_cache(expires_days=None)
    async def get_picture(self, url: str):
        return (await self._request("GET", url)).content

    @login_required
    @with_cache(expires_days=3)
    async def get_novel_list(self, sort: NovelSortMethod, page: int = 1, lang: Lang = Lang.zh_CN) -> SearchResult:
        resp = await self._request("GET",
                                   self.ENDPOINT + f"/modules/article/toplist.php?sort={sort}&page={page}&charset=gbk")
        resp.encoding = "gbk"
        parser = etree.HTML(resp.text)
        return lang_convent(self._search_page_parser(parser), lang)

    @login_required
    async def get_bookshelf(self, bid: int = 0, lang: Lang = Lang.zh_CN) -> list[BookshelfItem]:
        resp = await self._request("GET", self.ENDPOINT + f"/modules/article/bookcase.php?classid={bid}&charset=gbk")
        resp.encoding = "gbk"
        parser = etree.HTML(resp.text)
        results = []
        for novel in parser.xpath('//*[@id="checkform"]/table')[0]:
            if novel.get("align") == "center":
                continue
            if len(novel) == 1:
                continue

            updated_after_last_reading = False
            finished = False
            title_elem = novel[1][0]
            if novel[1][0].text == "新":
                updated_after_last_reading = True
                finished = False
                title_elem = novel[1][1]
            if novel[1][0].text.startswith("["):
                finished = True
                title_elem = novel[1][1]
                if novel[1][1].text == "新":
                    updated_after_last_reading = True
                    title_elem = novel[1][2]

            aid = int(re.search(r'aid=(\d+)', title_elem.get("href")).group(1))
            bid = int(re.search(r'bid=(\d+)', title_elem.get("href")).group(1))

            latest_section = novel[3][0].text
            latest_section_cid = int(re.search(r'cid=(\d+)', novel[3][0].get("href")).group(1))

            bookmark = novel[4][0].text
            if bookmark:
                bookmark_cid = int(re.search(r'cid=(\d+)', novel[4][0].get("href")).group(1))
            else:
                bookmark_cid = None

            results.append(BookshelfItem(aid=aid, bid=bid, title=title_elem.text,
                                         author=novel[2][0].text, latest_section=latest_section,
                                         latest_section_cid=latest_section_cid, bookmark=bookmark,
                                         bookmark_cid=bookmark_cid, last_updated=novel[5].text.strip(),
                                         finished=finished, updated_after_last_reading=updated_after_last_reading))

        return lang_convent(results, lang)

    def start_cache_daemon(self, loop: asyncio.AbstractEventLoop, interval: int = 3600):
        self.cache_daemon.start(loop, interval)

    def stop_cache_daemon(self):
        self.cache_daemon.stop()

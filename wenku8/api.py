import asyncio
import functools
import re
import time
from urllib.parse import quote

import httpx
import lxml.html
import zendriver
from lxml import etree

from wenku8.consts import LoginValidity, Lang, SearchMethod, NovelSortMethod
from wenku8.exceptions import NotLoggedInException
from wenku8.models import NovelInfo, _Volume, _Chapter, NovelIndex, SearchItem, SearchResult, PageControl, BookshelfItem
from wenku8.utils import extract_text, cooldown, separate_chinese_colon, get_chapter_content, lang_convent


def login_required(func):
    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        if not self.is_logged_in:
            raise NotLoggedInException
        return await func(self, *args, **kwargs)

    return wrapper


class Wenku8API:
    ENDPOINT = "https://www.wenku8.net"
    # CDN 二进制资源仅校验 UA，用真实浏览器 UA 直连即可
    _USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

    def __init__(self, endpoint: str = "https://www.wenku8.net", headless: bool = True):
        self.ENDPOINT = endpoint
        self.headless = headless
        # 常驻浏览器，懒启动；_browser_lock 仅保护启动，_nav_lock 串行化页面导航
        self._browser = None
        self._browser_lock = asyncio.Lock()
        self._nav_lock = asyncio.Lock()
        self._phpsessid: str | None = None

    async def _ensure_browser(self):
        """懒启动常驻浏览器（双重检查锁）。必须在 _nav_lock 之外调用以避免嵌套死锁。"""
        if self._browser is None:
            async with self._browser_lock:
                if self._browser is None:
                    self._browser = await zendriver.start(
                        config=zendriver.Config(headless=self.headless, sandbox=False))
        return self._browser

    async def close(self):
        """关闭常驻浏览器，释放资源。"""
        if self._browser is not None:
            await self._browser.stop()
            self._browser = None
            self._phpsessid = None

    @staticmethod
    def _strip_tbody(html: str) -> str:
        """浏览器渲染后的 DOM 会被注入 <tbody>，破坏现有不带 tbody 的 XPath，故剥离。"""
        return re.sub(r"</?tbody[^>]*>", "", html, flags=re.IGNORECASE)

    @staticmethod
    def _is_cf_challenge(html: str) -> bool:
        """判断是否为 Cloudflare 质询/拦截页。

        正常页面也会注入 challenge-platform 脚本，故只能以质询页特有的 <title>
        作为判据，避免对正常页面误报触发无谓的 reload。
        """
        head = html[:2048].lower()
        return ("<title>just a moment" in head
                or "<title>attention required" in head
                or "正在验证您是否是真人" in head)

    async def _wait_cf(self, tab, timeout: float = 60.0) -> str:
        """等待页面加载完成并处理 Cloudflare 质询，返回最终 HTML。

        tab.get() 对 wenku8 常在 body 渲染前就返回，故先等 readyState=complete，
        否则会拿到只有 <head> 的空壳页面。
        """
        try:
            await tab.wait_for_ready_state("complete", timeout=15)
        except Exception:
            pass
        deadline = time.monotonic() + timeout
        html = await tab.get_content()
        while self._is_cf_challenge(html) and time.monotonic() < deadline:
            try:
                await tab.verify_cf()
            except Exception:
                pass
            await asyncio.sleep(2)
            await tab.reload()
            try:
                await tab.wait_for_ready_state("complete", timeout=15)
            except Exception:
                pass
            html = await tab.get_content()
        return html

    async def _refresh_cookies(self, browser) -> None:
        """从浏览器 cookie jar 同步 PHPSESSID 到 self._phpsessid。"""
        for cookie in await browser.cookies.get_all():
            if cookie.name == "PHPSESSID":
                self._phpsessid = cookie.value
                return

    async def _navigate(self, url: str, *, want_url: bool = False):
        """导航到 url，处理 Cloudflare 质询，返回剥离 tbody 后的渲染 HTML。

        渲染后的 DOM 已是正确解码的 Unicode（规避了 CDP getResponseBody 对 GBK 的误解码），
        调用方直接 etree.HTML(html) 即可，无需再设置 encoding。
        """
        browser = await self._ensure_browser()
        async with self._nav_lock:
            tab = browser.main_tab
            await tab.get(url)
            try:
                await tab.verify_cf()
            except Exception:
                pass
            html = self._strip_tbody(await self._wait_cf(tab))
            await self._refresh_cookies(browser)
            if want_url:
                final_url = await tab.evaluate("window.location.href")
                return html, final_url
            return html

    async def _fetch_binary(self, url: str) -> bytes:
        """通过 httpx 直接抓取二进制资源（封面图、整本 TXT 等）。

        二进制资源位于 CDN（img.wenku8.com / dlN.wenku8.com），仅需浏览器 UA 即可通过，
        无需浏览器/CDP。调用方传入的应是可直接访问的 CDN URL（见 get_novel_cover /
        get_full_novel_content 的 URL 构造）。
        """
        async with httpx.AsyncClient(
            headers={"User-Agent": self._USER_AGENT},
            follow_redirects=True,
            timeout=30.0,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    async def login(self, username: str, password: str, validity: LoginValidity = LoginValidity.NONE) -> str:
        """访问登录页面，自动填充用户名密码并点击提交完成登录，返回 PHPSESSID。"""
        browser = await self._ensure_browser()
        async with self._nav_lock:
            tab = browser.main_tab
            # 裸 login.php 的 body 为空，登录表单在 login.php?do=submit
            await tab.get(self.ENDPOINT + "/login.php?do=submit")
            await self._wait_cf(tab)
            await tab.wait_for("input[name=username]", timeout=15)
            await (await tab.select("input[name=username]")).send_keys(username)
            await (await tab.select("input[name=password]")).send_keys(password)
            # usecookie 是 <select>，选项值与 LoginValidity 一致（内部枚举，无注入风险）
            await tab.evaluate(
                f'document.querySelector("select[name=usecookie]").value="{validity.value}"')
            await (await tab.select("input[name=submit]")).click()
            await asyncio.sleep(1)  # click 不自动等待导航完成
            await self._wait_cf(tab)  # 跳转后可能再次遇到 Cloudflare
            await self._refresh_cookies(browser)
        return self._phpsessid

    @property
    def is_logged_in(self):
        return bool(self._phpsessid)

    async def get_novel_cover(self, aid: int):
        return await self._fetch_binary(f"https://img.wenku8.com/image/{int(aid) // 1000}/{aid}/{aid}s.jpg")

    @login_required
    async def get_novel_info(self, aid: int, lang: Lang = Lang.zh_CN) -> NovelInfo:
        html = await self._navigate(self.ENDPOINT + f"/modules/article/articleinfo.php?id={aid}&charset=gbk")
        parser = etree.HTML(html)

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
    async def get_novel_index(self, aid: int, lang: Lang = Lang.zh_CN) -> NovelIndex:
        html = await self._navigate(self.ENDPOINT + f"/modules/article/reader.php?aid={aid}&charset=gbk")
        parser = etree.HTML(html)
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
    async def get_novel_content(self, aid: int, cid: int, lang: Lang = Lang.zh_CN) -> str:
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/reader.php?aid={aid}&cid={cid}&charset=gbk")
        parser = etree.HTML(html)
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

    async def get_full_novel_content(self, aid: int, lang: Lang = Lang.zh_CN) -> str:
        """下载整本小说（UTF-8 TXT）。直接访问 CDN 静态文件，绕开 dl.wenku8.com 的 Cloudflare 质询。"""
        body = await self._fetch_binary(f"https://dl1.wenku8.com/txtutf8/{int(aid) // 1000}/{aid}.txt")
        return lang_convent(body.decode("utf-8"), lang)

    @login_required
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
    async def search_novel(self, keyword: str, method: SearchMethod, page: int = 1,
                           lang: Lang = Lang.zh_CN) -> SearchResult:
        keyword = lang_convent(keyword, Lang.zh_CN)
        html, final_url = await self._navigate(
            self.ENDPOINT + f"/modules/article/search.php?searchtype={method}&searchkey={quote(keyword.encode('gbk'))}&page={page}",
            want_url=True)
        if final_url.endswith(".htm"):  # 只有一个结果时会跳转到对应的页面
            info = await self.get_novel_info(re.search(r"(\d*).htm", final_url).group(1), lang=lang)
            return lang_convent(SearchResult(
                results=[SearchItem(aid=info.aid, title=info.title, author=info.author, press=info.press,
                                    last_updated=info.last_updated, word_count=info.word_count,
                                    status=info.status, tags=info.tags, intro_preview=info.intro,
                                    copyright=info.copyright, animation=info.animation)],
                page_control=PageControl(now=1, previous=1, next=1, begin=1, end=1)), lang)
        else:
            parser = etree.HTML(html)
            return lang_convent(self._search_page_parser(parser), lang)

    async def search_novel_by_name(self, keyword: str, page: int = 1, lang: Lang = Lang.zh_CN):
        return await self.search_novel(keyword, SearchMethod.NAME, page, lang)

    async def search_novel_by_author(self, keyword: str, page: int = 1, lang: Lang = Lang.zh_CN):
        return await self.search_novel(keyword, SearchMethod.AUTHOR, page, lang)

    async def get_picture(self, url: str):
        return await self._fetch_binary(url)

    @login_required
    async def get_novel_list(self, sort: NovelSortMethod, page: int = 1, lang: Lang = Lang.zh_CN) -> SearchResult:
        html = await self._navigate(
            self.ENDPOINT + f"/modules/article/toplist.php?sort={sort}&page={page}&charset=gbk")
        parser = etree.HTML(html)
        return lang_convent(self._search_page_parser(parser), lang)

    @login_required
    async def get_bookshelf(self, bid: int = 0, lang: Lang = Lang.zh_CN) -> list[BookshelfItem]:
        html = await self._navigate(self.ENDPOINT + f"/modules/article/bookcase.php?classid={bid}&charset=gbk")
        parser = etree.HTML(html)
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

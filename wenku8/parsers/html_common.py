"""Wenku8 Web(桌面 HTML) 页面解析器。基于旧版 api.py 中被验证有效的 XPath 提炼，
改为纯函数，便于离线存档样本测试。

页面清单：
- articleinfo.php          → parse_novel_info
- reader.php (无 cid)      → parse_novel_index（目录）
- reader.php?cid=…         → parse_novel_content（章节正文）
- search.php / toplist.php → parse_search_result（列表，含分页）
- bookcase.php             → parse_bookshelf
- reviews.php              → parse_review_list（书评列表）
- reviewshow.php?rid=…     → parse_review_detail（书评楼层）
"""
from __future__ import annotations

import re
from typing import Optional

from lxml import etree

from wenku8.exceptions import PageParseError
from wenku8.models import (
    Book, Chapter, LibraryCategory, NovelContent, NovelIndex, NovelInfo, PageControl,
    Review, ReviewDetail, ReviewPage, ReviewReply, SearchItem, SearchResult, UserInfo,
    Volume,
)
from wenku8.utils import extract_text, separate_chinese_colon


def _root(html: str):
    return etree.HTML(html)


# ---------- 小说详情 ----------
def parse_novel_info(html: str, aid: int, *, url: str = "") -> NovelInfo:
    parser = _root(html)
    has_br = bool(parser.xpath('//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[2]/b/br'))
    if has_br:
        # 版权受限的页面结构：无更新/字数/热度
        last_updated: Optional[str] = None
        word_count: Optional[int] = None
        popularity_level = None
        trending_level = None
        latest_section = None
        intro = "".join(parser.xpath('//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[4]//text()'))
    else:
        last_updated = extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[2]/td[4]', True)
        wc_str = extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[2]/td[5]', True)
        wc_str = wc_str.replace("字", "") if wc_str else ""
        try:
            word_count = int(wc_str) if wc_str else None
        except ValueError:
            word_count = None
        rating_parts = extract_text(
            parser, '//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[2]/b').split("，")
        popularity_level = separate_chinese_colon(rating_parts[0])[1] if rating_parts else None
        trending_level = separate_chinese_colon(rating_parts[1])[1] if len(rating_parts) > 1 else None
        latest_section = extract_text(parser, '//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[4]/a')
        intro = "".join(parser.xpath('//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[6]//text()'))

    title = extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[1]/td/table/tr/td[1]/span/b')
    if not title:
        # 可能是版权受限页或异常页
        title_alt = parser.xpath('//*[@id="content"]//h1//text()')
        title = "".join(title_alt).strip() if title_alt else ""
    if not title:
        raise PageParseError("详情页缺少标题", html, url=url, xpath="//title")

    return NovelInfo(
        aid=aid,
        title=title,
        author=extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[2]/td[2]', True),
        status=extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[2]/td[3]', True),
        last_updated=last_updated,
        intro=intro,
        tags=[t for t in extract_text(
            parser, '//*[@id="content"]/div[1]/table[2]/tr/td[2]/span[1]/b', True).split(" ") if t],
        press=extract_text(parser, '//*[@id="content"]/div[1]/table[1]/tr[2]/td[1]', True),
        word_count=word_count,
        popularity_level=popularity_level,
        trending_level=trending_level,
        latest_section=latest_section,
        copyright=not has_br,
        animation=bool(parser.xpath('//*[@id="content"]/div[1]/table[2]/tr/td[1]/span/b')),
    )


# ---------- 目录 ----------
def parse_novel_index(html: str, aid: int, *, url: str = "") -> NovelIndex:
    parser = _root(html)
    volumes: list[Volume] = []
    current_vol: Optional[Volume] = None
    for td in parser.xpath('//table[@class="css"]//td[@class="vcss" or @class="ccss"]'):
        cls = td.get("class")
        if cls == "vcss":
            if current_vol:
                volumes.append(current_vol)
            vid = td.get("vid")
            current_vol = Volume(vid=int(vid) if vid and vid.isdigit() else 0,
                                 title=(td.text or "").strip(),
                                 chapters=[])
        elif cls == "ccss":
            if current_vol is None:
                continue
            link = td.find("a")
            if link is None:
                continue
            href = link.get("href") or ""
            m = re.search(r"cid=(\d+)", href)
            if not m:
                continue
            current_vol.chapters.append(Chapter(cid=int(m.group(1)),
                                                title=(link.text or "").strip()))
    if current_vol:
        volumes.append(current_vol)
    if not volumes:
        # 版权受限（目录被拦）→ 返回空目录并标记，供上层回退到其它源
        if _is_copyright_blocked(html):
            return NovelIndex(
                aid=aid,
                title=extract_text(parser, '//*[@id="title"]'),
                author=extract_text(parser, '//*[@id="info"]', True),
                volumes=[],
                copyright=False,
            )
        raise PageParseError("目录页缺少卷/章节点", html, url=url,
                             xpath='//table[@class="css"]')
    return NovelIndex(
        aid=aid,
        title=extract_text(parser, '//*[@id="title"]'),
        author=extract_text(parser, '//*[@id="info"]', True),
        volumes=volumes,
    )


# ---------- 章节正文 ----------
_COPYRIGHT_MARKERS = ("因版权问题", "版权原因", "不再提供该小说的阅读")


def _is_copyright_blocked(html: str) -> bool:
    """检测版权拦截标记（web 对受限书正文/目录返回提示页）。"""
    return any(m in html for m in _COPYRIGHT_MARKERS)


def parse_novel_content(html: str, aid: int, cid: int, *, url: str = "") -> NovelContent:
    parser = _root(html)
    nodes = parser.xpath('//*[@id="content"]')
    if not nodes:
        raise PageParseError("章节页面缺少 #content 节点", html, url=url,
                             xpath='//*[@id="content"]')
    out: list[str] = []
    images: list[str] = []
    for child in nodes[0]:
        if child.tag == "div":
            hrefs = child.xpath(".//a/@href") or []
            for h in hrefs:
                out.append(f"<!--image-->{h}<!--image-->")
                images.append(h)
        if child.tail:
            out.append(child.tail)
    copyright_ok = not _is_copyright_blocked(html)
    return NovelContent(aid=aid, cid=cid, text="".join(out).strip(),
                        images=images, copyright=copyright_ok)


# ---------- 搜索/排行列表 ----------
def parse_search_result(html: str, *, url: str = "") -> SearchResult:
    parser = _root(html)
    content_nodes = parser.xpath('//*[@id="content"]/table/tr/td')
    if not content_nodes:
        raise PageParseError("搜索/列表页面缺少内容节点", html, url=url,
                             xpath='//*[@id="content"]/table/tr/td')
    results: list[SearchItem] = []
    for novel in content_nodes[0]:
        try:
            results.append(_parse_search_row(novel))
        except (IndexError, AttributeError):
            continue  # 防御：异常行跳过
    stats = parser.xpath('//*[@id="pagestats"]')
    if not stats or not stats[0].text:
        raise PageParseError("搜索/列表页面缺少 #pagestats", html, url=url,
                             xpath='//*[@id="pagestats"]')
    return SearchResult(results=results,
                        page_control=PageControl.from_str(stats[0].text))


def _parse_search_row(novel) -> SearchItem:
    """解析搜索/排行结果表格中的一行。"""
    seg3 = novel[1][2].text or ""
    parts3 = seg3.split("/")
    if len(parts3) < 3:
        last_updated = None
        word_count = None
        status = parts3[0]
        animation = len(parts3) == 2
    else:
        last_updated = parts3[0].split(":", 1)[1] if ":" in parts3[0] else parts3[0]
        word_count = parts3[1].split(":", 1)[1] if ":" in parts3[1] else parts3[1]
        status = parts3[2]
        animation = len(parts3) == 4

    seg2 = novel[1][1].text or ""
    if "/" in seg2:
        press = seg2.split("/")[1].split(":", 1)[1] if ":" in seg2 else seg2.split("/")[1]
    else:
        press = seg2.split("  ")[1].split(":", 1)[1] if ":" in seg2 else ""

    link = novel[1][0][0]
    title_link = link.get("title") or (link.text or "").strip()
    href = link.get("href") or ""
    m = re.search(r"(\d+)\.htm", href)
    aid = int(m.group(1)) if m else 0
    seg1 = novel[1][1].text or ""
    author = seg1.split("/")[0].split(":", 1)[1] if ":" in seg1 else ""

    tags = []
    try:
        tags = [t for t in (novel[1][3][0].text or "").split(" ") if t]
    except (IndexError, AttributeError):
        pass
    intro_preview = ""
    try:
        intro_preview = (novel[1][4].text or "").split(":", 1)[1]
    except (IndexError, AttributeError):
        pass
    try:
        copyright = not (novel[1][5].get("class") == "hottext")
    except (IndexError, AttributeError):
        copyright = True
    return SearchItem(aid=aid, title=title_link, author=author, press=press,
                      last_updated=last_updated, word_count=word_count,
                      status=status, tags=tags, intro_preview=intro_preview,
                      copyright=copyright, animation=animation)


# ---------- 书架 ----------
def parse_bookshelf(html: str, *, url: str = "") -> list[Book]:
    parser = _root(html)
    tables = parser.xpath('//*[@id="checkform"]/table')
    if not tables:
        raise PageParseError("书架页面缺少 #checkform/table", html, url=url,
                             xpath='//*[@id="checkform"]/table')
    books: list[Book] = []
    for novel in tables[0]:
        if novel.get("align") == "center" or len(novel) <= 1:
            continue
        updated_after_last_reading = False
        finished = False
        title_elem = novel[1][0]
        first_text = novel[1][0].text or ""
        if first_text == "新":
            updated_after_last_reading = True
            title_elem = novel[1][1]
            first_text = novel[1][1].text or ""
        if first_text.startswith("["):
            finished = True
            title_elem = novel[1][1]
            if (novel[1][1].text or "").strip() == "新":
                updated_after_last_reading = True
                title_elem = novel[1][2]

        href = title_elem.get("href") or ""
        m_aid = re.search(r"aid=(\d+)", href)
        m_bid = re.search(r"bid=(\d+)", href)
        if not m_aid:
            continue
        latest_section = latest_cid = None
        try:
            latest_section = novel[3][0].text
            m_cid = re.search(r"cid=(\d+)", novel[3][0].get("href") or "")
            if m_cid:
                latest_cid = int(m_cid.group(1))
        except (IndexError, AttributeError):
            pass
        bookmark = bookmark_cid = None
        try:
            bookmark = novel[4][0].text
            m_cid = re.search(r"cid=(\d+)", novel[4][0].get("href") or "")
            if m_cid:
                bookmark_cid = int(m_cid.group(1))
        except (IndexError, AttributeError):
            pass
        last_updated = ""
        try:
            last_updated = (novel[5].text or "").strip()
        except (IndexError, AttributeError):
            pass
        author = ""
        try:
            author = novel[2][0].text or ""
        except (IndexError, AttributeError):
            pass
        books.append(Book(
            aid=int(m_aid.group(1)),
            bid=int(m_bid.group(1)) if m_bid else None,
            title=(title_elem.text or "").strip(),
            author=author,
            latest_section=latest_section,
            latest_section_cid=latest_cid,
            bookmark=bookmark,
            bookmark_cid=bookmark_cid,
            last_updated=last_updated,
            finished=finished,
            updated_after_last_reading=updated_after_last_reading,
        ))
    return books


# ---------- 书评 ----------
def _text(el) -> str:
    """元素纯文本（etree 元素无 text_content，用 XPath string() 取）。"""
    try:
        return el.xpath("string(.)")
    except Exception:
        return ""


def _inner_html(el) -> str:
    """取元素内部 HTML（保留标签与链接），与 API 侧 content 语义一致。"""
    parts = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(etree.tostring(child, encoding="unicode", method="html"))
    return "".join(parts).strip()


def parse_review_list(html: str, aid: int = 0, *, url: str = "") -> ReviewPage:
    """reviews.php 书评列表页：每行 → rid/标题/回复数/作者/回复时间。"""
    parser = _root(html)
    reviews: list[Review] = []
    for tr in parser.xpath("//tr"):
        link = tr.xpath('.//a[contains(@href,"reviewshow.php?rid=")]')
        if not link:
            continue
        m = re.search(r"rid=(\d+)", link[0].get("href") or "")
        if not m:
            continue
        replies = 0
        reply_time = ""
        user_uid = 0
        user_name = ""
        for td in tr.xpath("./td"):
            txt = _text(td).strip()
            mr = re.match(r"^(\d+)/(\d+)$", txt)          # 回复/查看
            if mr:
                replies = int(mr.group(1))
                continue
            ua = td.xpath('.//a[contains(@href,"userpage.php?uid=")]')
            if ua:
                user_name = (ua[0].text or "").strip()
                mu = re.search(r"uid=(\d+)", ua[0].get("href") or "")
                user_uid = int(mu.group(1)) if mu else 0
                continue
            mt = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", txt)
            if mt:
                reply_time = mt.group(1)
        reviews.append(Review(rid=int(m.group(1)), aid=aid,
                              title=(link[0].text or "").strip(),
                              replies=replies, reply_time=reply_time,
                              user_uid=user_uid, user_name=user_name))
    if not reviews:
        raise PageParseError("书评列表页未解析到条目", html, url=url,
                             xpath='//a[contains(@href,"reviewshow.php")]')
    stats = parser.xpath('//*[@id="pagestats"]')
    page_control = PageControl.from_str(stats[0].text or "1/1") if stats else PageControl()
    return ReviewPage(aid=aid, reviews=reviews, page_control=page_control)


def parse_review_detail(html: str, rid: int = 0, aid: int = 0, *,
                        url: str = "") -> ReviewDetail:
    """reviewshow.php 书评详情：每个 table.grid 为一个楼层（含正文 HTML）。"""
    parser = _root(html)
    title = ""
    ths = parser.xpath('//table[@class="grid"]//th//strong')
    if ths:
        title = re.sub(r"^主题[：:]\s*", "", _text(ths[0]).strip())
    floors: list[ReviewReply] = []
    for table in parser.xpath('//table[@class="grid"]'):
        if table.xpath(".//textarea"):
            continue                       # 回复表单区，非楼层
        if not table.xpath("./tr/td"):
            continue                       # 标题行
        ua = table.xpath('.//a[contains(@href,"userpage.php?uid=")]')
        user_uid = 0
        user_name = ""
        if ua:
            mu = re.search(r"uid=(\d+)", ua[0].get("href") or "")
            user_uid = int(mu.group(1)) if mu else 0
            user_name = (ua[0].text or "").strip()
        flat = re.sub(r"\s+", " ", _text(table))
        mt = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", flat)
        floor_no = len(floors)          # 与 api 源一致：0-based（0 为书评本体）
        mf = re.search(r"(\d+)#", flat)
        if mf:
            try:
                floor_no = int(mf.group(1)) - 1   # 页面自 #1 标注，转为 0-based
            except ValueError:
                pass
        content = ""
        divs = table.xpath('.//div[contains(@style,"width:100%")]')
        if divs:
            content = _inner_html(divs[-1])
        floors.append(ReviewReply(rid=rid, floor=floor_no,
                                  timestamp=mt.group(1) if mt else "",
                                  user_uid=user_uid, user_name=user_name,
                                  content=content))
    if not floors:
        raise PageParseError("书评详情页未解析到楼层", html, url=url,
                             xpath='//table[@class="grid"]')
    stats = parser.xpath('//*[@id="pagestats"]')
    page_control = PageControl.from_str(stats[0].text or "1/1") if stats else PageControl()
    return ReviewDetail(rid=rid, aid=aid, title=title, floors=floors,
                        page_control=page_control)


# ---------- 文库分类 / 分类列表 ----------
def parse_library_list(html: str, *, url: str = "") -> list[LibraryCategory]:
    """从列表页的“分类阅读”链接提取文库分类（articlelist.php?class=N）。"""
    parser = _root(html)
    seen: set[int] = set()
    out: list[LibraryCategory] = []
    for a in parser.xpath('//a[contains(@href,"articlelist.php?class=")]'):
        href = a.get("href") or ""
        if "charset=" in href:          # 语言切换链接（繁體版等），非分类
            continue
        m = re.search(r"class=(\d+)", href)
        name = (a.text or "").strip()
        if not m or not name:
            continue
        sid = int(m.group(1))
        if sid in seen:
            continue
        seen.add(sid)
        out.append(LibraryCategory(sort_id=sid, name=name))
    if not out:
        raise PageParseError("未解析到文库分类", html, url=url,
                             xpath='//a[contains(@href,"articlelist.php?class=")]')
    return out


def parse_novel_card_list(html: str, *, url: str = "") -> SearchResult:
    """分类列表页的书籍卡片：标题/作者/文库/更新/字数/状态/Tags/简介。

    页面把多个卡片放在同一个 <tr> 内，故以卡片容器（div[width:373px]）
    为单位解析，找不到时回退到按行解析。
    """
    parser = _root(html)
    cards = parser.xpath('//div[contains(@style,"width:373px")]')
    if not cards:
        cards = [tr for tr in parser.xpath("//tr")
                 if tr.xpath('.//a[contains(@href,"/book/")]')]
    results: list[SearchItem] = []
    for card in cards:
        links = card.xpath('.//a[contains(@href,"/book/")]')
        if not links:
            continue
        m = re.search(r"/book/(\d+)\.htm", links[0].get("href") or "")
        if not m:
            continue
        title = ""
        for a in links:
            if a.get("title"):
                title = a.get("title").strip()
                break
        if not title:
            title = (links[0].text or "").strip()
        text = _text(card)
        author = press = last_updated = word_count = status = ""
        if (mm := re.search(r"作者[:：]\s*([^/\n]+)", text)):
            author = mm.group(1).strip()
        if (mm := re.search(r"分类[:：]\s*([^/\n]+)", text)):
            press = mm.group(1).strip()
        if (mm := re.search(r"更新[:：]\s*([\d-]+)", text)):
            last_updated = mm.group(1)
        if (mm := re.search(r"字数[:：]\s*([0-9KkMm.]+)", text)):
            word_count = mm.group(1)
        if (mm := re.search(r"(连载中|連載中|已完成)", text)):
            status = mm.group(1)
        tags: list[str] = []
        spans = card.xpath(".//p//span")
        if spans:
            tags = [t for t in _text(spans[0]).split() if t]
        intro_preview = ""
        if (mm := re.search(r"简介[:：](.*)", text)):
            intro_preview = mm.group(1).strip()
        results.append(SearchItem(
            aid=int(m.group(1)), title=title, author=author, press=press,
            last_updated=last_updated, word_count=word_count, status=status,
            tags=tags, intro_preview=intro_preview[:160]))
    if not results:
        raise PageParseError("分类列表页未解析到书籍", html, url=url,
                             xpath='//a[contains(@href,"/book/")]')
    stats = parser.xpath('//*[@id="pagestats"]')
    page_control = PageControl.from_str(stats[0].text or "1/1") if stats else PageControl()
    return SearchResult(results=results, page_control=page_control)


# ---------- 用户信息 ----------
def parse_user_info(html: str, *, url: str = "") -> UserInfo:
    """用户面板（userdetail.php）的键值表 → UserInfo。"""
    parser = _root(html)

    def _cell_value(cell) -> str:
        vals = cell.xpath(".//input/@value")      # 昵称等可能位于 input value
        if vals and vals[0].strip():
            return vals[0].strip()
        return _text(cell).strip()

    fields: dict[str, str] = {}
    for tr in parser.xpath("//tr"):
        tds = tr.xpath("./td")
        if len(tds) < 2:
            continue
        key = re.sub(r"\s+", "", _text(tds[0])).rstrip("：:")
        if not key:
            continue
        # 值取“标签后第一个非空单元格”（部分行标签后跟多个 td，末位可能为空）
        value = ""
        for td in tds[1:]:
            v = _cell_value(td)
            if v:
                value = v
                break
        fields.setdefault(key, value)

    def _int(*keys: str) -> int:
        for k in keys:
            digits = re.sub(r"[^\d]", "", fields.get(k, ""))
            if digits:
                return int(digits)
        return 0

    info = UserInfo(uid=_int("用户ID", "UID"),
                    username=fields.get("用户名", ""),
                    nickname=fields.get("昵称", ""),
                    score=_int("现有积分", "积分"),
                    experience=_int("经验值", "经验"),
                    rank=fields.get("等级", "") or fields.get("头衔", ""))
    if not (info.uid or info.username):
        raise PageParseError("未解析到用户信息（可能未登录）", html, url=url,
                             xpath='//tr/td')
    return info

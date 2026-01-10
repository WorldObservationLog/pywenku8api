from dataclasses import dataclass
from typing import Optional


@dataclass
class NovelInfo:
    """书籍详细信息数据类。

    Attributes:
        aid: 文章唯一标识符 (Article ID)。
        title: 书名。
        author: 作者姓名。
        status: 文章状态。
        last_updated: 最后更新日期 (YYYY-MM-DD)。
        intro: 内容简介。
        tags: 作品标签列表。
        press: 文库分类 (如: 讲谈社小说)。
        word_count: 全文总字数。
        popularity_level: 作品当前热度评级 (如: E级)。
        trending_level: 热度上升指数评级 (如: B级)。
        latest_section: 最新更新章节的标题。
        copyright: 是否未被版权下架
        animation: 是否动画化
    """
    aid: int
    title: str
    author: str
    status: str
    last_updated: str
    intro: str
    tags: list[str]
    press: str
    word_count: int
    popularity_level: str
    trending_level: str
    latest_section: str
    copyright: bool
    animation: bool


@dataclass
class _Chapter:
    cid: int
    title: str


@dataclass
class _Volume:
    vid: int
    title: str
    chapters: list[_Chapter]


@dataclass
class NovelIndex:
    aid: int
    title: str
    author: str
    volumes: list[_Volume]


@dataclass
class SearchItem:
    aid: int
    title: str
    author: str
    press: str
    last_updated: Optional[str]
    word_count: Optional[str]
    status: str
    tags: list[str]
    intro_preview: str
    copyright: bool
    animation: bool


@dataclass
class PageControl:
    now: int
    previous: int
    next: int
    begin: int
    end: int

    @classmethod
    def from_str(cls, text: str):
        now = int(text.split("/")[0])
        if now == 1:
            previous = 1
        else:
            previous = now - 1
        end = int(text.split("/")[1])
        return cls(now=now,
                   previous=previous,
                   next=now+1,
                   begin=1,
                   end=end)

@dataclass
class SearchResult:
    results: list[SearchItem]
    page_control: PageControl


@dataclass
class BookshelfItem:
    aid: int
    bid: int
    title: str
    author: str
    latest_section: str
    latest_section_cid: int
    bookmark: Optional[str]
    bookmark_cid: Optional[int]
    last_updated: str
    finished: bool
    updated_after_last_reading: bool

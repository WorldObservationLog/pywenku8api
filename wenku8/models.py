"""统一数据模型：跨 Web / API 多来源。

设计原则：
- 保持与原 Wenku8API 返回的核心 dataclass 字段名与语义一致（兼容上层调用方），
  仅新增可选字段以承载不同来源可提供的额外信息（如 API 的统计字段）。
- 一切文本字段均可由上层做简繁转换（见 wenku8.utils.lang_convent）。
- NovelContent 统一承载正文/插图占位（HTML 来源为 `<!--image-->URL<!--image-->`）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class NovelInfo:
    """书籍详细信息（融合 Web 详情页 / API bookinfo / meta 三类数据源可提供的字段）。"""

    aid: int
    title: str = ""
    author: str = ""
    status: str = ""                     # 连载中 / 已完成
    last_updated: Optional[str] = None   # YYYY-MM-DD
    intro: str = ""                      # 完整简介
    tags: list[str] = field(default_factory=list)
    press: str = ""                      # 文库/出版社（如 电击文库 / 小学馆）
    word_count: Optional[int] = None
    popularity_level: Optional[str] = None   # Web 热度评级（E级…）
    trending_level: Optional[str] = None     # Web 上升评级
    latest_section: Optional[str] = None     # 最新章节标题
    copyright: bool = True                   # True=正常阅读(未因版权下架)；False=版权受限
    animation: bool = False                  # 是否动画化
    # ---- 以下为可选扩展字段（API 中继等来源提供） ----
    press_id: Optional[int] = None       # 文库 sid（API meta 的 sid）
    day_hits: Optional[int] = None       # 今日点击数
    total_hits: Optional[int] = None      # 总点击数
    push_count: Optional[int] = None      # 推荐数
    fav_count: Optional[int] = None       # 收藏数
    latest_section_cid: Optional[int] = None  # 最新章节 cid
    book_length: Optional[int] = None     # 原始 BookLength（可能与 word_count 不同单位）


@dataclass
class Chapter:
    cid: int
    title: str


@dataclass
class Volume:
    vid: int
    title: str
    chapters: list[Chapter] = field(default_factory=list)


@dataclass
class NovelIndex:
    """小说目录（卷 -> 章）。api=list 与 web reader.php 均映射到此结构。"""

    aid: int
    title: str = ""
    author: str = ""
    volumes: list[Volume] = field(default_factory=list)
    copyright: bool = True        # False=版权受限（web 目录被拦时）


@dataclass
class NovelContent:
    """单章正文。

    text: 正文纯文本；插图以占位符 `<!--image-->URL<!--image-->` 出现（与旧实现一致）。
    source: 实际返回该内容的来源（用于上层排查/日志）。
    """

    aid: int
    cid: int
    title: str = ""              # 章节标题（尽力而为）
    text: str = ""
    images: list[str] = field(default_factory=list)  # 解析出的插图 URL 列表
    source: str = ""
    copyright: bool = True        # False=版权受限（正文被站点拦截，仅 web 可能发生）


@dataclass
class SearchItem:
    aid: int
    title: str = ""
    author: str = ""
    press: str = ""
    last_updated: Optional[str] = None
    word_count: Optional[str] = None
    status: str = ""
    tags: list[str] = field(default_factory=list)
    intro_preview: str = ""
    copyright: bool = True
    animation: bool = False
    intro: str = ""               # 完整简介（个别来源一次带全）


@dataclass
class PageControl:
    """分页控制信息（1-based）。"""

    now: int = 1
    previous: int = 1
    next: int = 1
    begin: int = 1
    end: int = 1

    @classmethod
    def from_str(cls, text: str) -> "PageControl":
        """解析 '当前/总数' 形式。"""
        try:
            parts = text.strip().split("/")
            now = int(parts[0])
            end = int(parts[1])
        except (IndexError, ValueError):
            return cls()
        return cls(now=now,
                   previous=max(1, now - 1),
                   next=min(end, now + 1),
                   begin=1,
                   end=end)


@dataclass
class SearchResult:
    results: list[SearchItem] = field(default_factory=list)
    page_control: PageControl = field(default_factory=PageControl)


@dataclass
class Review:
    """书评列表条目（对应 API review&do=list / Web reviews.php 的一行）。"""

    rid: int
    aid: int = 0
    title: str = ""                 # 书评标题
    post_time: str = ""             # YYYYMMDDHHMMSS 或格式化时间
    replies: int = 0                # 回复数
    reply_time: str = ""            # 最后回复时间
    user_uid: int = 0
    user_name: str = ""
    content: str = ""               # 摘要/正文（列表页多为标题，详情页为全文）


@dataclass
class ReviewReply:
    """书评楼层（API review&do=show / Web reviewshow.php）。

    楼层 0 为书评本体，其后为回复。
    """

    rid: int = 0
    floor: int = 0
    timestamp: str = ""
    user_uid: int = 0
    user_name: str = ""
    content: str = ""


@dataclass
class ReviewPage:
    """一页书评列表。"""

    aid: int = 0
    reviews: list[Review] = field(default_factory=list)
    page_control: PageControl = field(default_factory=PageControl)


@dataclass
class ReviewDetail:
    """单条书评详情（含楼层）。"""

    rid: int = 0
    aid: int = 0
    title: str = ""
    floors: list[ReviewReply] = field(default_factory=list)
    page_control: PageControl = field(default_factory=PageControl)


@dataclass
class LibraryCategory:
    """文库分类（API action=xml&item=sort；Web 对应文库分类页）。"""

    sort_id: int = 0
    name: str = ""


@dataclass
class UserInfo:
    """当前登录用户信息（API action=userinfo；Web 用户资料页）。"""

    uid: int = 0
    username: str = ""
    nickname: str = ""
    score: int = 0              # 积分
    experience: int = 0         # 经验
    rank: str = ""              # 等级称号


@dataclass
class Book:
    """书架条目。"""

    aid: int
    bid: Optional[int] = None          # 书架内自增 id（Web 特有）
    title: str = ""
    author: str = ""
    latest_section: Optional[str] = None
    latest_section_cid: Optional[int] = None
    bookmark: Optional[str] = None     # 阅读进度章节标题
    bookmark_cid: Optional[int] = None
    last_updated: Optional[str] = None  # YYYY-MM-DD
    finished: bool = False
    updated_after_last_reading: bool = False
    add_date: Optional[str] = None      # API 的 date 属性


# 旧名别名：保证既有引用（如 OPDS 层）不中断
_Volume = Volume
_Chapter = Chapter

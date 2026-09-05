"""常量与枚举。"""
from __future__ import annotations

from enum import StrEnum


class Source(StrEnum):
    """三种数据来源 + 特殊值。

    web: wenku8.net 桌面 HTML 版（www）
    api: wenku8-relay.mewx.org 中继 API（Base64 指令，XML/JSON 响应）
    cdn: img.wenku8.com / dlN.wenku8.com 静态资源（封面/插图/整本 TXT），
         通常作为某一页面来源的伴随资源使用，不作为独立业务来源参与优先级链。
    """

    web = "web"
    api = "api"
    cdn = "cdn"

    # 参与业务优先级链的“页面类”来源
    @classmethod
    def page_sources(cls) -> tuple["Source", ...]:
        return (cls.web, cls.api)


class LoginValidity(StrEnum):
    """登录有效期（登录表单 usecookie 值，秒）。"""

    NONE = "0"
    ONE_DAY = "86400"
    ONE_MONTH = "2592000"
    ONE_YEAR = "315360000"


class Lang(StrEnum):
    """语言/简繁。内部转换为站点参数（charset / t 值）。"""

    zh_CN = "zh_CN"   # 简体
    zh_TW = "zh_TW"   # 繁体

    @property
    def charset(self) -> str:
        return "gbk" if self == Lang.zh_CN else "big5"

    @property
    def api_t(self) -> int:
        """API 中继的 t 参数（0=简, 1=繁）。"""
        return 0 if self == Lang.zh_CN else 1


class SearchMethod(StrEnum):
    NAME = "articlename"
    AUTHOR = "author"


class NovelSortMethod(StrEnum):
    """Web toplist 排序；API articlelist/novellist 复用同名 sort 参数。"""

    allVisit = "allvisit"
    allVote = "allvote"
    monthVisit = "monthvisit"
    monthVote = "monthvote"
    weekVisit = "weekvisit"
    weekVote = "weekvote"
    dayVisit = "dayvisit"
    dayVote = "dayvote"
    postDate = "postdate"
    lastUpdate = "lastupdate"
    goodNum = "goodnum"
    size = "size"
    fullFlag = "fullflag"
    anime = "anime"


# ---- 来源能力注册 ----
class Capability(StrEnum):
    """业务能力。供父类做来源能力过滤（同一方法不同来源能力差异时）。"""

    NOVEL_INFO = "novel_info"
    NOVEL_INDEX = "novel_index"
    NOVEL_CONTENT = "novel_content"
    NOVEL_FULL = "novel_full"        # 整本 TXT 下载
    NOVEL_COVER = "novel_cover"
    SEARCH = "search"
    NOVEL_LIST = "novel_list"
    BOOKSHELF = "bookshelf"
    LOGIN = "login"
    PICTURE = "picture"


# 缺省每来源能力（后续可由子类覆盖）
DEFAULT_SOURCE_CAPABILITIES: dict[Source, set[Capability]] = {
    Source.web: {c for c in Capability},
    Source.api: {c for c in Capability},
    Source.cdn: {Capability.NOVEL_COVER, Capability.NOVEL_FULL, Capability.PICTURE},
}

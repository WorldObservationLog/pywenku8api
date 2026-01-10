from enum import StrEnum, IntEnum


class LoginValidity(StrEnum):
    NONE = "0"
    ONE_DAY = "86400"
    ONE_MONTH = "2592000"
    ONE_YEAR = "315360000"


class Lang(StrEnum):
    zh_CN = "gbk"
    zh_TW = "big5"


class SearchMethod(StrEnum):
    NAME = "articlename"
    AUTHOR = "articleauthor"


class NovelSortMethod(StrEnum):
    """
    Attributes:
        allVisit: 总排行榜
        allVote: 总推荐榜
        monthVisit: 月排行榜
        monthVote: 月推荐榜
        weekVisit: 周排行榜
        weekVote: 周推荐榜
        dayVisit: 日排行榜
        dayVote: 日推荐榜
        postDate: 最新入库
        lastUpdate: 最近更新
        goodNum: 总收藏榜
        size: 字数排行
        anime: 已动画化
    """
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

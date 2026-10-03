"""返回字段统一策略：两来源共有字段为默认，单来源独有字段需显式启用。

背景
----
api（中继）与 web（网页解析）能提供的字段并不相同，例如：

- 书籍详情：api 元数据带统计数据（点击数/推荐数/收藏数…），web 详情页带
  站内评级（热度/上升）——两者互相没有。
- 列表项信息：web 无轻量接口，退回详情页解析，因此比 api 多出文库/字数等。
- 书评列表：api 只给正文，web 只给标题。

为了让「同一方法在两个来源返回同样的字段集」，这里按**调用方法 + 模型类型**
声明“来源独有字段”（extra）。默认这些字段被置为模型定义中的空值；只有调用方
显式传入 extra_fields 时才填充：

    await client.get_novel_info(aid)                        # 仅两源共有字段
    await client.get_novel_info(aid, extra_fields=True)     # 该源全部额外字段
    await client.get_novel_info(aid, extra_fields=["day_hits", "popularity_level"])

也可在构造时给出全局默认：

    Wenku8Client(extra_fields=True)      # 整个客户端默认返回全量字段

缓存始终保存**完整对象**，裁剪发生在返回前，因此切换 extra_fields 不会
产生额外请求；source 层的 fetch_* 始终返回完整数据（原始视图）。
"""
from __future__ import annotations

from dataclasses import MISSING, fields, is_dataclass, replace
from typing import Any, Iterable, Optional, Union

from wenku8.models import (
    Book, NovelIndex, NovelInfo, Review, ReviewDetail, SearchItem,
)

# 额外字段声明：方法名 -> {模型类型: 该类型在本方法下的来源独有字段}
# 未登记的方法/类型不做裁剪（两源结构本就一致，如 UserInfo / LibraryCategory）。
EXTRA_FIELDS: dict[str, dict[type, frozenset[str]]] = {
    "fetch_novel_info": {
        NovelInfo: frozenset({
            # 仅 api（元数据统计）
            "press_id", "day_hits", "total_hits", "push_count", "fav_count",
            "latest_section_cid", "book_length",
            # 仅 web（站内评级）
            "popularity_level", "trending_level", "animation",
        }),
    },
    "fetch_novel_shortinfo": {
        # api 的短信息只有 7 个字段；web 退回详情页，故这些均为 web 独有
        NovelInfo: frozenset({
            "tags", "press", "word_count", "latest_section", "animation",
            "popularity_level", "trending_level",
            "press_id", "day_hits", "total_hits", "push_count", "fav_count",
            "latest_section_cid", "book_length",
        }),
    },
    "fetch_novel_bookinfo": {
        NovelInfo: frozenset({
            "intro", "press", "word_count", "latest_section", "animation",
            "popularity_level", "trending_level",
            "press_id", "day_hits", "total_hits", "push_count", "fav_count",
            "latest_section_cid", "book_length",
        }),
    },
    "fetch_novel_index": {
        # api 的目录接口不返回书名/作者
        NovelIndex: frozenset({"title", "author"}),
    },
    "fetch_search": {
        # api 的富化搜索带完整简介
        SearchItem: frozenset({"animation", "intro"}),
    },
    "fetch_novel_list": {
        # web 排行榜带文库/字数/动画化标记
        SearchItem: frozenset({"press", "word_count", "animation", "intro"}),
    },
    "fetch_novel_list_by_library": {
        SearchItem: frozenset({"press", "animation", "intro"}),
    },
    "fetch_reviews": {
        # api 给正文与发布时间；web 给标题
        Review: frozenset({"content", "post_time", "title"}),
    },
    "fetch_review_detail": {
        ReviewDetail: frozenset({"title"}),
    },
    "fetch_bookshelf": {
        Book: frozenset({
            "author", "bid", "last_updated", "updated_after_last_reading",
            "add_date", "bookmark", "bookmark_cid", "finished",
        }),
    },
}

ExtraPolicy = Union[bool, Iterable[str], None]


def _empty_value(model_field) -> Any:
    """取字段的定义默认值（无默认则 None）。"""
    if model_field.default is not MISSING:
        return model_field.default
    if model_field.default_factory is not MISSING:  # type: ignore[misc]
        return model_field.default_factory()         # type: ignore[misc]
    return None


def normalize_policy(policy: ExtraPolicy) -> Union[bool, frozenset[str]]:
    """把 extra_fields 归一化为 True（全量）或需保留的额外字段名集合。"""
    if policy is True:
        return True
    if not policy:
        return frozenset()
    if isinstance(policy, str):                      # 单个字段名也接受
        return frozenset({policy})
    return frozenset(policy)


def apply_policy(value: Any, method: str, policy: Union[bool, frozenset[str]]) -> Any:
    """按方法声明的额外字段，把未启用的来源独有字段置空（递归内容器）。

    policy 为 True 时原样返回（保留该来源的全部字段）。
    """
    if policy is True:
        return value
    spec = EXTRA_FIELDS.get(method)
    if not spec:
        return value
    return _prune(value, spec, policy)


def _prune(value: Any, spec: dict[type, frozenset[str]], policy: frozenset[str]) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        extra = spec.get(type(value))
        changes: dict[str, Any] = {}
        for f in fields(value):
            current = getattr(value, f.name)
            if extra and f.name in extra and f.name not in policy:
                changes[f.name] = _empty_value(f)
                continue
            pruned = _prune(current, spec, policy)
            if pruned is not current:
                changes[f.name] = pruned
        return replace(value, **changes) if changes else value
    if isinstance(value, list):
        out = [_prune(v, spec, policy) for v in value]
        return out if any(a is not b for a, b in zip(out, value)) else value
    if isinstance(value, tuple):
        out = tuple(_prune(v, spec, policy) for v in value)
        return out if any(a is not b for a, b in zip(out, value)) else value
    return value


def extra_field_names(method: str, model_type: Optional[type] = None) -> frozenset[str]:
    """查询某方法（可选限定模型）可被动启用的额外字段名，便于文档/自省。"""
    spec = EXTRA_FIELDS.get(method) or {}
    if model_type is not None:
        return spec.get(model_type, frozenset())
    out: set[str] = set()
    for names in spec.values():
        out |= names
    return frozenset(out)

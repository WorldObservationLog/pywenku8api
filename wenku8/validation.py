"""提交内容本地校验：最小长度与不宜词过滤。

站点对书评/回复有服务端校验（内容过短、命中词表会被拒绝或封号），
这里在**发出请求前**做一次本地预检，避免无谓请求与账号风险。

不宜词表：
- 内置一份“通用不宜词”（灌水、广告、辱骂、违禁品类）。
- 可通过 add_bad_words() / set_bad_words() 追加或替换为自有词表
  （库不代为收集任何特定立场词表，按使用场景自行维护）。
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from wenku8.exceptions import ContentValidationException

# 书评/回复的最小有效长度（站点限制：不少于 7 个字符）
MIN_REVIEW_TEXT = 7

# 内置通用不宜词（灌水/广告/辱骂/违禁品类）
_DEFAULT_BAD_WORDS: frozenset[str] = frozenset({
    "刷分", "路过路过", "路過路過", ".......", "。。。。", "blablabla",
    "色情", "迷魂药", "迷魂藥", "催情药", "催情藥", "毒品",
    "吃屎", "你妈", "你媽", "他妈", "他媽", "她妈", "她媽", "操你",
    "垃圾", "去死",
})

_bad_words: set[str] = set(_DEFAULT_BAD_WORDS)


def add_bad_words(words: Iterable[str]) -> None:
    """追加不宜词（进程内生效）。"""
    _bad_words.update(w for w in words if w)


def set_bad_words(words: Iterable[str]) -> None:
    """替换全部不宜词（含内置项）。"""
    global _bad_words
    _bad_words = {w for w in words if w}


def reset_bad_words() -> None:
    """恢复为内置不宜词表。"""
    global _bad_words
    _bad_words = set(_DEFAULT_BAD_WORDS)


def bad_words() -> frozenset[str]:
    """当前生效的不宜词（只读快照）。"""
    return frozenset(_bad_words)


def _strip_spaces(text: str) -> str:
    return re.sub(r"\s", "", text or "")


def find_bad_word(text: str) -> Optional[str]:
    """返回命中的不宜词；无命中返回 None（与站点一致：忽略空白后匹配）。"""
    flat = _strip_spaces(text)
    for w in _bad_words:
        if w and w in flat:
            return w
    return None


def validate_review_text(text: str, *, min_length: int = MIN_REVIEW_TEXT) -> None:
    """校验书评/回复内容；不通过抛 ContentValidationException。

    规则：去除空白后长度 >= min_length；且不含不宜词。
    """
    flat = _strip_spaces(text)
    if len(flat) < min_length:
        raise ContentValidationException(
            f"内容过短：去除空白后需至少 {min_length} 个字符（当前 {len(flat)}）",
            reason="too_short")
    hit = find_bad_word(text)
    if hit:
        raise ContentValidationException(
            f"内容包含不宜词：{hit}", hit=hit, reason="bad_word")

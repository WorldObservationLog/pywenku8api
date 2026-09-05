"""浏览器指纹与 UA 池管理。

httpcloak 的 preset 已精确复刻 TLS/HTTP2/TCP 全栈指纹与默认头顺序，本模块只负责：
- 按目标（web/api/cdn）选择合适 preset。
- 提供可选的额外请求头/UA 覆盖，维持会话内粘性，避免指纹漂移。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from wenku8.consts import Source

# httpcloak 可用 preset 由 available_presets() 动态列出；这里列出本项目用到的稳定项。
# windows 后缀用于声明 TCP/IP 指纹为 Windows（wenku8 目标国内 CDN 多按 UA/平台行为放行）。
PRESET_BY_SOURCE: dict[Source, str] = {
    Source.web: "chrome-latest-windows",
    Source.api: "chrome-latest-windows",   # relay 无质询，任何现代指纹均可
    Source.cdn: "chrome-latest-windows",
}

# UA 池（粘性：每个来源实例固定一个，避免同一会话内 UA 漂移触发风控）
_DESKTOP_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
]


@dataclass
class FingerprintProfile:
    """一个来源实例的指纹画像：preset + 粘性 UA + 固定 referer 域等。"""

    source: Source
    preset: str
    user_agent: str
    referer: str = "https://www.wenku8.net/"
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def for_source(cls, source: Source, preset_override: str | None = None,
                   ua_override: str | None = None) -> "FingerprintProfile":
        preset = preset_override or PRESET_BY_SOURCE.get(source, "chrome-latest-windows")
        if source == Source.api:
            ua = ua_override or _DESKTOP_UAS[0]
            referer = "https://wenku8-relay.mewx.org/"
        else:
            ua = ua_override or random.choice(_DESKTOP_UAS)
            referer = "https://www.wenku8.net/"
        return cls(source=source, preset=preset, user_agent=ua, referer=referer)


def normalize_preset(preset: str) -> str:
    """确保 preset 在本机 httpcloak 中可用；不可用则回退 chrome-latest-windows。"""
    try:
        from httpcloak import available_presets
        pool = set(available_presets())
        if preset in pool:
            return preset
        # 去掉平台后缀再试（如 chrome-latest-android -> chrome-latest）
        base = preset.split("-")
        for i in range(len(base), 0, -1):
            cand = "-".join(base[:i])
            if cand in pool:
                return cand
    except Exception:
        pass
    return "chrome-latest"

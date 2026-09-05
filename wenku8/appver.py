"""appver 计算契约（公开发行版）。

背景与设计意图
-------------
`api` 来源（wenku8-relay.mewx.org）的 `appver` 参数由官方 App native 层每 60 秒
动态计算（HMAC-SHA256 + magic 表）。该算法是对官方客户端逆向而来 —— **在公开
发行版中内置它存在被滥用于大规模抓取的风险**，因此本仓库的公开版本不携带真实
算法，仅提供调用契约与一个「默认不可用」的空实现：

    AppverProvider.compute(...)   # 默认返回 ""（表示未实现）

行为
----
- ApiRelaySource 每次请求经 appver 属性调用 provider；得到空串时视该来源
  “未配置”，请求前抛 SourceUnavailableException —— 默认优先级链会自动
  fallback 到 web，不影响库的其他功能。
- 使用者如需启用 api 来源，可自行实现正确算法后注入。参考签名：

    from wenku8.appver import AppverProvider

    class MyProvider(AppverProvider):
        @staticmethod
        def compute(version: str, at: float | None = None) -> str:
            # 在此放你自己的实现
            ...

    client = Wenku8Client(appver_provider=MyProvider.compute)

- 本地开发可把真实算法放到 `wenku8/local_appver_impl.py`（已被 .gitignore
  排除，不入公开库），AppverProvider.from_local() 会尝试加载它。
"""
from __future__ import annotations

import inspect
import sys
from typing import Callable, Optional


def _try_local_impl():
    """尝试加载本地私有实现 wenku8.local_appver_impl（gitignore 不入库）。

    返回可调用 compute(version, at)->str；文件缺失时返回 None。
    """
    try:
        from wenku8 import local_appver_impl as _impl  # type: ignore
        return getattr(_impl, "local_compute_appver", None)
    except Exception:
        return None


class AppverProvider:
    """appver 计算提供者（公开契约）。

    默认 compute() 返回 ""（未实现 → api 来源不可用）。用户继承并覆写
    compute，或将任意同签名可调用对象传给 Wenku8Client(appver_provider=...)。
    """

    @staticmethod
    def compute(version: str, at: Optional[float] = None) -> str:
        """返回该 (version, at) 下的 appver 字符串。

        at 为 Unix 秒时间戳（默认当前时间）；实现应按“分钟窗口”计算。
        返回 "" 表示未实现 / 无法计算。
        """
        return ""

    @classmethod
    def from_local(cls) -> "AppverProvider":
        """构造一个 provider：优先本地私有实现，否则回退到空实现。"""
        fn = _try_local_impl()
        if fn is not None:

            class _Local(cls):  # type: ignore[misc, valid-type]
                @staticmethod
                def compute(version: str = "1.30",
                            at: Optional[float] = None) -> str:
                    return fn(version, at)

            return _Local()
        return cls()


def default_appver_provider() -> Callable[[str, Optional[float]], str]:
    """返回默认 appver 计算函数。

    - 本地存在 wenku8/local_appver_impl.py → 用真实实现（开发环境）。
    - 否则返回空实现（公开发行环境；api 来源默认不可用）。
    """
    fn = _try_local_impl()
    if fn is not None:
        return fn

    def _empty(version: str = "1.30", at: Optional[float] = None) -> str:
        return ""

    return _empty


def normalize_provider(provider) -> Callable[[str, Optional[float]], str]:
    """把用户可能传入的多种形态归一为 (version, at)->str 可调用。

    支持：
    - 可调用对象（函数 / staticmethod / 类方法）
    - AppverProvider 子类（取其 compute）
    - None → default_appver_provider()
    """
    if provider is None:
        return default_appver_provider()
    if isinstance(provider, type) and issubclass(provider, AppverProvider):
        # 子类：取类方法 compute
        return provider.compute
    if callable(provider):
        sig = inspect.signature(provider)
        # 兼容只收 version 的简化函数：包一层
        n_params = sum(1 for p in sig.parameters.values()
                       if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
        if n_params <= 1:

            def _wrap(version: str = "1.30", at: Optional[float] = None) -> str:
                return provider(version)

            return _wrap
        return provider
    raise TypeError(f"appver_provider 必须是可调用或 AppverProvider 子类, 得到 {type(provider)!r}")

"""异常体系（面向三来源统一错误语义）。

层级：
- Wenku8Error（基类）
  - NotLoggedInException   需要登录而当前未登录
  - SourceUnavailableException  指定来源整体不可用（被熔断/全部失败）
  - AllSourcesBlockedException  优先级链全部来源都失败
  - CloudflareChallengeException  CF 质询页/Managed Challenge（无法用 HTTP 层解决）
  - RateLimitException      来源限流（HTTP 429 或 CF 1015/封禁页）
  - PageParseError          HTML/XML 解析失败（携带页面片段便于调试）
  - LoginErrorException     登录失败
"""
from __future__ import annotations


class Wenku8Error(Exception):
    """所有 wenku8 异常的基类。"""


class NotLoggedInException(Wenku8Error):
    """调用需要登录的接口时未登录。"""


class SourceUnavailableException(Wenku8Error):
    """指定来源当前不可用（熔断中 / 网络失败 / 全部重试耗尽）。"""

    def __init__(self, source: str | None = None, message: str = ""):
        self.source = source
        super().__init__(message or f"来源不可用: {source}")


class AllSourcesBlockedException(Wenku8Error):
    """优先级链上所有来源均失败（含熔断），无法完成请求。"""

    def __init__(self, operation: str = "", errors: dict | None = None):
        self.operation = operation
        self.errors = errors or {}
        super().__init__(f"所有来源均不可用 (op={operation}, sources={list(self.errors)})")


class CloudflareChallengeException(Wenku8Error):
    """请求命中 Cloudflare 质询/Managed Challenge，且当前通道（HTTP 指纹层）无法解决。

    一般应触发调用方切换到浏览器兜底通道或换来源。
    """

    def __init__(self, message: str = "Cloudflare 质询", url: str = "", snippet: str = ""):
        self.url = url
        self.snippet = snippet
        super().__init__(f"{message} url={url}" + (f" page={snippet[:200]}" if snippet else ""))


class RateLimitException(Wenku8Error):
    """触发限流：HTTP 429，或 CF 封禁/IP 限流页（错误码 1015/1020 等）。"""

    def __init__(self, message: str = "触发速率限制", retry_after: float | None = None,
                 source: str | None = None):
        self.retry_after = retry_after
        self.source = source
        super().__init__(f"{message} source={source} retry_after={retry_after}")


class PageParseError(Wenku8Error):
    """HTML/XML 结构解析失败：期望节点缺失或页面异常（质询残留/404/改版）。"""

    def __init__(self, message: str, page: str = "", *, xpath: str = "", url: str = "",
                 source: str | None = None):
        self.page = page
        self.xpath = xpath
        self.url = url
        self.source = source
        detail = (page or "")[:2000] if page else "(无页面内容)"
        super().__init__(f"{message} [url={url or 'N/A'} xpath={xpath or 'N/A'}] 页面片段: {detail}")


class LoginErrorException(Wenku8Error):
    """登录失败（凭据错误 / 需要人工验证等）。"""

    def __init__(self, message: str = "登录失败", code: int | None = None,
                 source: str | None = None):
        self.code = code
        self.source = source
        super().__init__(f"{message} (code={code}, source={source})")


class OperationFailedException(Wenku8Error):
    """写操作未成功（书架满/已在书架/推荐失败等业务码）。保留服务端返回码。"""

    def __init__(self, message: str = "写操作失败", code: int | None = None,
                 source: str | None = None):
        self.code = code
        self.source = source
        super().__init__(f"{message} (code={code}, source={source})")

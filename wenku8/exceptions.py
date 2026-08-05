class NotLoggedInException(Exception):
    pass


class RateLimitException(Exception):
    pass


class CloudflareChallengeException(Exception):
    """CDN 资源被 Cloudflare 防火墙拦截，返回质询页而非目标内容。

    当前策略：检测到即抛错，不尝试用浏览器完成质询（httpx 的 TLS 指纹与
    真实浏览器不同，质询也无法可靠通过）。
    """


class PageParseError(Exception):
    """页面解析失败：期望的节点/结构在页面中缺失。

    常见于页面返回 CF 质询残留页、等待页、404 或 wenku8 结构变化。
    异常消息携带页面 HTML 片段以供调试。
    """

    def __init__(self, message: str, html: str = "", *, xpath: str = ""):
        self.html = html
        self.xpath = xpath
        detail = html[:2000] if html else "(无页面内容)"
        super().__init__(f"{message} [xpath={xpath or 'N/A'}] 页面片段: {detail}")

class NotLoggedInException(Exception):
    pass


class RateLimitException(Exception):
    pass


class CloudflareChallengeException(Exception):
    """CDN 资源被 Cloudflare 防火墙拦截，返回质询页而非目标内容。

    当前策略：检测到即抛错，不尝试用浏览器完成质询（httpx 的 TLS 指纹与
    真实浏览器不同，质询也无法可靠通过）。
    """

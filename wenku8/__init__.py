"""pywenku8api：Wenku8 轻小说文库多来源统一客户端（Web / API 中继）。

对外主入口为 wenku8.client.Wenku8Client（父类门面），统一管理：
- 来源优先级链与自动 fallback
- 来源级/全局限速、退避、熔断
- 反反爬：httpcloak 全栈浏览器指纹（HTTP 层） + zendriver 真实浏览器兜底（CF 质询）
- 会话登录与 cookie 隔离

注意：api 来源的 appver 算法不随公开发行版提供（防滥用）。默认 api 不可用，
需经 Wenku8Client(appver_provider=...) 注入实现（见 wenku8.appver）。
"""
from wenku8.appver import AppverProvider, default_appver_provider
from wenku8.client import Wenku8Client

__all__ = ["Wenku8Client", "AppverProvider", "default_appver_provider"]
__version__ = "0.2.0"

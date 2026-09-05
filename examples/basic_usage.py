"""pywenku8api v2 三来源使用示例。

前置：
- 一个“可过盾”的出口（住宅代理 / 已人工过 CF 的 IP）才能读到 /modules/article/ 深页；
  首页/登录/书架/封面等路径默认直连即可。
- 账号：构造 Wenku8Client 时经 credentials 注入（不落盘）。
- api 来源：公开发行版默认不可用（appver 算法不入库，防滥用）。
  启用需注入正确实现，例如：
      from wenku8 import AppverProvider
      class MyAppver(AppverProvider):
          @staticmethod
          def compute(version, at=None): ...   # 你的实现
      Wenku8Client(appver_provider=MyAppver.compute)
  本地开发时 wenku8/local_appver_impl.py 存在则 api 自动可用。

运行：
    python examples/basic_usage.py
"""
import asyncio
import os

from wenku8 import Wenku8Client, AppverProvider
from wenku8.consts import Lang, SearchMethod, Source

USERNAME = os.environ.get("WENKU8_USER", "")
PASSWORD = os.environ.get("WENKU8_PASS", "")


async def main():
    # 方式 A：桌面 Web 优先（默认优先级链 web→api；api 未注入 appver 实现会自动跳过）
    async with Wenku8Client(credentials={"web": {"username": USERNAME, "password": PASSWORD}}) as client:
        # 1) 登录 Web（也可指定 source="api"）
        result = await client.login(source="web")
        print("登录结果:", result, "已登录来源:", client.logged_in_sources)

        # 2) 封面（CDN 直连，无 CF 问题）
        cover = await client.get_novel_cover(2580)
        print(f"封面字节数: {len(cover)}")

        # 3) 整本 TXT（CDN 直连；注意量级，单次可能数 MB）
        # full = await client.get_full_novel_content(2580)
        # print(f"整本长度: {len(full)}")

        # 4) 详情 / 目录 / 正文 —— 深页需可过盾出口；失败会抛异常
        try:
            info = await client.get_novel_info(2580, source=Source.web)
            print(f"书名: {info.title}\n作者: {info.author}\n状态: {info.status}")
            index = await client.get_novel_index(2580, source=Source.web)
            first = index.volumes[0]
            print(f"目录: {len(index.volumes)} 卷 / 首卷 {first.title}")
            cid = first.chapters[0].cid
            content = await client.get_novel_content(2580, cid, source=Source.web)
            print(f"章节 {cid} 正文前 120 字:\n{content.text[:120]}")
        except Exception as e:
            print(f"[深页不可用，已优雅报错] {type(e).__name__}: {e}")
            print("  提示：正文/目录路径受 Cloudflare Managed Challenge 保护，请配置可过盾代理。")

        # 5) 搜索（HTTP 层可达路径）
        try:
            sr = await client.search_novel_by_name("天使", page=1)
            print(f"搜索命中 {len(sr.results)} 条")
            for item in sr.results[:3]:
                print(f"  - {item.title} ({item.author})")
        except Exception as e:
            print(f"[搜索不可用] {type(e).__name__}: {e}")

    # 方式 B：指定优先级链与每来源代理
    # async with Wenku8Client(
    #     priority=[Source.api, Source.web],   # 想优先用 API 中继
    #     proxies={"web": "http://127.0.0.1:7890"},
    # ) as client:
    #     ...


if __name__ == "__main__":
    asyncio.run(main())

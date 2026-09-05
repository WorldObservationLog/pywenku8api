# pywenku8api

面向[轻小说文库（Wenku8）](https://www.wenku8.net) 的 Python 异步客户端，为 [Wenku8-OPDS](https://github.com/WorldObservationLog/wenku8-opds-readme) 等上层应用提供统一阅读接口：书籍信息、目录、正文、封面、整本下载、搜索、书架与登录。

## 数据来源

| 来源 | 说明 |
|---|---|
| `web` | wenku8.net 网页版 |
| `api` | 官方 App 数据通道（默认不启用，见下） |
| `cdn` | 封面 / 整本 TXT 等静态资源 |

- 方法默认沿 `web → api` 优先级自动选择可用来源，也可用 `source=` 显式指定。
- `api` 来源默认不可用；如需启用，请向 `Wenku8Client(appver_provider=...)` 注入你自己的实现后即可使用。

## 安装

```bash
pip install -e .
```

需要 Python 3.11+。

## 快速开始

```python
import asyncio, os
from wenku8 import Wenku8Client

async def main():
    async with Wenku8Client(
        credentials={"web": {"username": os.environ["WENKU8_USER"],
                             "password": os.environ["WENKU8_PASS"]}},
        cache=True,
    ) as client:
        await client.login(source="web")

        info = await client.get_novel_info(2580)
        print(info.title, info.author, info.status)

        idx = await client.get_novel_index(2580)
        ch0 = idx.volumes[0].chapters[0]
        chap = await client.get_novel_content(2580, ch0.cid)
        print(chap.text[:200])

        cover = await client.get_novel_cover(2580)          # 封面
        full = await client.get_full_novel_content(2580)    # 整本 TXT

asyncio.run(main())
```

账号通过环境变量 `WENKU8_USER` / `WENKU8_PASS` 提供。完整示例见 [examples/basic_usage.py](examples/basic_usage.py)。

## 主要接口

- 阅读：`get_novel_info` / `get_novel_intro` / `get_novel_index` / `get_novel_content` / `get_full_novel_content`
- 资源：`get_novel_cover` / `get_novel_bookinfo` / `get_picture`
- 发现：`search_novel`（按名/按作者）/ `get_novel_list`
- 用户：`login` / `get_bookshelf` / `bookshelf_add` / `bookshelf_del` / `vote_novel`
- 模型见 `wenku8/models.py`，支持简繁（`Lang.zh_CN` / `Lang.zh_TW`）。

## 限制

- 版权受限书目无法阅读；日本 IP 无法使用（站点侧限制）。
- 当前出口对部分页面可能遇到反爬质询；如遇大量失败请考虑更换出口或降低调用频率。

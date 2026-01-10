# pywenku8api

此项目提供基于[轻小说文库（Wenku8）](https://www.wenku8.net)网页版的API实现，可绕过其 Cloudflare 防火墙与官方 API 的限制。

为支持 [Wenku8-OPDS](https://github.com/WorldObservationLog/wenku8-opds-readme) 而开发

## 功能列表
- 小说部分
  - 获取信息 (`get_novel_info`)
  - 获取封面 (`get_novel_cover`)
  - 获取目录 (`get_novel_index`)
  - 获取内容 (`get_novel_content`)
  - 搜索小说（书名/作者）(`search_novel/search_novel_by_name/search_novel_by_author`)
  - 获取小说列表 (`get_novel_list`)
  - 搜索小说（TAG）(*TODO*)
  - 收藏小说 (*TODO*)
  - 推荐小说 (*TODO*)
- 用户部分
  - 登录 (`login`)
  - 获取书架 (`get_bookshelf`)
  - 获取个人信息 (*TODO*)
- 评论部分 (*TODO*)
  - 获取书评
  - 发表书评
  - 回复书评
- 杂项
  - 简繁转换

## 限制
- 可能会绕不过 Cloudflare 防火墙
- 版权书目无法阅读
- 日本 IP 无法使用
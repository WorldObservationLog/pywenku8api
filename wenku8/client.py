"""Wenku8Client —— 多来源统一门面（父类）。

职责：
- 装配各来源（web / api）+ CDN 资源源；
- 按优先级链自动 fallback；每个方法可指定 source 强制使用某来源；
- 来源级限速/熔断由各 source 自带 limiter 承担；跨源全熔断由 ChainCircuitBreaker 判定；
- 登录与会话由各来源独立管理（cookie 隔离）；
- 封面/整本/图片等 CDN 能力由 cdn 源提供。

方法签名统一 (…, source: Source|None = None)。source=None → 默认优先级链。

默认优先级链：web → api
（理由：web 桌面版结构最完整、经验证解析器最稳；api 不依赖 CF、速度最快，
作为第二候选；api 默认需注入 appver 实现，未注入时自动跳过。）

注意：web 深页命中 Cloudflare Managed Challenge 时浏览器兜底也未必能解；
链式 fallback 会把 SourceUnavailableException 向上冒泡，调用方可自行处理。
"""
from __future__ import annotations

import asyncio
from typing import Iterable, Optional

from wenku8.consts import Capability, Lang, SearchMethod, Source
from wenku8.exceptions import (
    AllSourcesBlockedException, SourceUnavailableException, Wenku8Error,
)
from wenku8.limiter import ChainCircuitBreaker, RateLimitConfig
from wenku8.models import (
    Book, NovelContent, NovelIndex, NovelInfo, SearchResult,
)
from wenku8.sources.api import ApiRelaySource
from wenku8.sources.base import BaseSource
from wenku8.sources.cdn import CdnSource
from wenku8.sources.web import WebSource

DEFAULT_PRIORITY = (Source.web, Source.api)


class Wenku8Client:
    def __init__(
        self,
        *,
        priority: Iterable[Source] = DEFAULT_PRIORITY,
        proxies: Optional[dict[str, str]] = None,     # {source: proxy_url}
        global_rate: Optional[RateLimitConfig] = None,
        rate_limits: Optional[dict[str, RateLimitConfig]] = None,  # {source: cfg}
        credentials: Optional[dict[str, dict]] = None,   # {source: {username,password}}
        headless: bool = True,
        browser: Optional[object] = None,   # 共享 BrowserFetcher（可选）
        api_endpoint: str = "https://wenku8-relay.mewx.org/",
        api_appver: Optional[str] = None,   # 固定 appver（一般不设）
        appver_provider=None,               # appver 计算实现（见 wenku8/appver.py）
        enable_cdn: bool = True,
        default_lang: Lang = Lang.zh_CN,
        cache: bool = False,
        cache_dir: Optional[str] = None,
        cache_ttl_overrides: Optional[dict[str, float]] = None,
    ):
        self.priority = list(priority)
        self.default_lang = default_lang
        proxies = proxies or {}
        rate_limits = rate_limits or {}
        credentials = credentials or {}

        # 缓存层（默认关，开启需显式 cache=True）
        self._cache_enabled = cache
        if cache:
            from wenku8.cache import Cache
            self._cache = Cache(ttl_overrides=cache_ttl_overrides,
                                disk_dir=cache_dir)
        else:
            self._cache = None

        # 装配来源
        self._sources: dict[Source, BaseSource] = {}
        self._add_source(WebSource(proxy=proxies.get(Source.web),
                                   rate_config=rate_limits.get(Source.web),
                                   credentials=credentials.get(Source.web),
                                   browser=browser, headless=headless))
        self._add_source(ApiRelaySource(endpoint=api_endpoint, appver=api_appver,
                                        appver_provider=appver_provider,
                                        proxy=proxies.get(Source.api),
                                        rate_config=rate_limits.get(Source.api)
                                        or RateLimitConfig.relaxed(),
                                        credentials=credentials.get(Source.api),
                                        allow_browser_fallback=False))
        if enable_cdn:
            self._add_source(CdnSource(proxy=proxies.get(Source.cdn),
                                       rate_config=rate_limits.get(Source.cdn)
                                       or RateLimitConfig.relaxed(),
                                       allow_browser_fallback=False))

        # 全局熔断协调（把每个来源的 limiter 注册进来）
        self._breaker = ChainCircuitBreaker([s.value for s in self.priority])
        for src in self._sources.values():
            if src.source in self.priority:
                self._breaker.register(src.limiter)

        # CDN 快捷引用
        self.cdn = self._sources.get(Source.cdn)

    def _add_source(self, src: BaseSource) -> None:
        self._sources[src.source] = src

    def source(self, name: Source | str) -> Optional[BaseSource]:
        return self._sources.get(Source(name) if isinstance(name, str) else name)

    @property
    def sources(self) -> dict[Source, BaseSource]:
        return dict(self._sources)

    # ---- 来源选择与 fallback ----
    def _chain_for(self, op: Capability, source: Optional[Source | str]) -> list[BaseSource]:
        """返回某操作的候选来源链（按优先级；source 指定则单来源）。"""
        if source is not None:
            s = Source(source) if isinstance(source, str) else source
            cand = self._sources.get(s)
            return [cand] if cand else []
        chain = []
        for s in self.priority:
            src = self._sources.get(s)
            if src and op in src.capabilities:
                chain.append(src)
        # CDN 源不在业务优先级链上；由各自方法单独处理
        return chain

    async def _try_chain(self, op: Capability, method_name: str,
                         source: Optional[Source | str], *args, **kwargs):
        """沿链执行直到成功；全部失败抛 AllSourcesBlockedException。

        errors 收集每个来源的失败原因。
        """
        chain = self._chain_for(op, source)
        if not chain:
            raise SourceUnavailableException(str(source), "该来源未启用或不支持此操作")
        errors: dict[str, str] = {}
        last_exc: Optional[BaseException] = None
        chain_sources = [src.source.value for src in chain]
        # 熔断恢复：全部来源失败后，若源于熔断则等待最短冷却自动重试
        recovery_rounds = 0
        max_recovery_wait = 30.0
        while True:
            # 全熔断时：若最短冷却可接受 → 等待恢复；否则直接抛错
            cooldown = self._breaker.min_cooldown(chain_sources)
            if cooldown > 0:
                if recovery_rounds >= 1 or cooldown > max_recovery_wait:
                    raise AllSourcesBlockedException(
                        method_name, {s: f"circuit_open(cooldown={cooldown:.0f}s)"
                                      for s in chain_sources})
                recovery_rounds += 1
                await asyncio.sleep(cooldown + 0.5)
                continue
            errors.clear()
            last_exc = None
            for src in chain:
                method = getattr(src, method_name, None)
                if method is None:
                    # 能力声明与方法实现不一致（防御）：跳过该源
                    errors[src.source.value] = f"缺少方法 {method_name}"
                    last_exc = SourceUnavailableException(
                        src.source.value, f"缺少方法 {method_name}")
                    continue
                try:
                    # 成功：清除该来源熔断计数
                    src.limiter.report_success()
                    return await method(*args, **kwargs)
                except SourceUnavailableException as e:
                    errors[src.source.value] = f"unavailable: {e}"
                    last_exc = e
                except Wenku8Error as e:
                    # 登录失败/解析失败等：若来源可用性没问题则不继续换源（同源也会失败）
                    # 但限流/封禁类异常应继续尝试下一来源
                    from wenku8.exceptions import RateLimitException, CloudflareChallengeException
                    if isinstance(e, (RateLimitException, CloudflareChallengeException)):
                        errors[src.source.value] = f"{type(e).__name__}: {e}"
                        last_exc = e
                        continue
                    raise
                except Exception as e:  # noqa: BLE001 网络等偶发 → 尝试下一来源
                    errors[src.source.value] = f"{type(e).__name__}: {e}"
                    last_exc = e
            # 本轮全部失败（无熔断冷却可等）→ 收尾
            if len(chain) == 1 and isinstance(last_exc, Wenku8Error):
                raise last_exc
            raise AllSourcesBlockedException(method_name, errors)

    # ============ 业务接口 ============
    async def login(self, source: Optional[Source | str] = None,
                    username: Optional[str] = None, password: Optional[str] = None,
                    **kw) -> dict[str, bool]:
        """登录指定来源（默认登录优先级链中第一个支持 LOGIN 的来源）。

        返回 {source: 是否成功}。未提供凭据时从 sources 的 credentials 读取。
        """
        chain = self._chain_for(Capability.LOGIN, source)
        if not chain:
            raise SourceUnavailableException(str(source), "无可用登录来源")
        out: dict[str, bool] = {}
        for src in chain:
            cred = src.credentials or {}
            u = username or cred.get("username")
            p = password or cred.get("password")
            if not u or not p:
                out[src.source.value] = False
                continue
            try:
                ok = await src.login(u, p, **kw)
                out[src.source.value] = bool(ok)
            except Exception as e:  # noqa: BLE001
                out[src.source.value] = False
        return out

    async def logout(self, source: Optional[Source | str] = None) -> None:
        chain = self._chain_for(Capability.LOGIN, source) if source else \
            [s for s in self._sources.values() if hasattr(s, "logout")]
        for src in chain:
            try:
                await src.logout()
            except Exception:
                pass

    @property
    def logged_in_sources(self) -> list[str]:
        return [s.source.value for s in self._sources.values()
                if getattr(s, "is_logged_in", False)]

    # ---- 读操作 ----
    async def _cached_call(self, method: str, coro_factory, args: tuple = (),
                           kwargs: Optional[dict] = None, use_cache: Optional[bool] = None):
        """若缓存启用则先查缓存；未命中执行 coro_factory 并回填。"""
        if use_cache is None:
            use_cache = self._cache_enabled
        cache = self._cache if use_cache else None
        if cache is None:
            return await coro_factory()
        hit = await cache.get(method, args, kwargs)
        if hit is not None:
            return hit
        val = await coro_factory()
        await cache.set(method, val, args, kwargs)
        return val

    async def _copyright_fallback(self, method_name: str, result,
                                  source, *args):
        """web 源返回版权受限结果时，自动用 api 源重取同内容。

        仅当未显式指定 source（走默认链）且 api 源存在可用时触发；
        返回 (最终结果, 是否发生了回退)。
        """
        if source is not None or getattr(result, "copyright", True):
            return result, False
        api = self._sources.get(Source.api)
        if api is None or not hasattr(api, method_name):
            return result, False
        try:
            alt = await getattr(api, method_name)(*args)
            return alt, True
        except Exception:
            return result, False  # api 也失败 → 保留 web 结果

    async def get_novel_info(self, aid: int, source: Optional[Source | str] = None,
                             lang: Optional[Lang] = None,
                             use_cache: Optional[bool] = None) -> NovelInfo:
        lang = lang or self.default_lang

        async def _impl():
            info = await self._try_chain(Capability.NOVEL_INFO, "fetch_novel_info",
                                         source, aid, lang)
            info, _ = await self._copyright_fallback("fetch_novel_info", info,
                                                     source, aid, lang)
            return info

        return await self._cached_call(
            "fetch_novel_info", _impl,
            (aid, source.value if isinstance(source, Source) else source, lang.value),
            None, use_cache)

    async def get_novel_intro(self, aid: int, source: Optional[Source | str] = None,
                              lang: Optional[Lang] = None,
                              use_cache: Optional[bool] = None) -> str:
        """获取小说完整简介文本。

        默认优先 web 详情页的 intro 字段（无需 api appver、页面含完整简介），
        web 不可用时再回退 api 的独立 do=intro 接口。
        """
        lang = lang or self.default_lang
        return await self._cached_call(
            "fetch_novel_intro",
            lambda: self._intro_impl(aid, source, lang),
            (aid, source.value if isinstance(source, Source) else source, lang.value),
            None, use_cache)

    async def _intro_impl(self, aid: int, source, lang: Lang) -> str:
        """取简介：source 指定则用该源；默认 web → 沿链。"""
        if source is not None:
            s = self.source(source) if isinstance(source, str) else self._sources.get(source)
            if s is not None and hasattr(s, "fetch_novel_intro"):
                return await s.fetch_novel_intro(aid, lang=lang)
            # 该源无独立简介接口 → 取其 info.intro
            info = await self._try_chain(Capability.NOVEL_INFO, "fetch_novel_info",
                                         source, aid, lang)
            return (info.intro or "").strip()
        # 默认：先 web 详情页 intro（无需 api appver），再沿链 info.intro
        web = self._sources.get(Source.web)
        if web is not None:
            try:
                info = await web.fetch_novel_info(aid, lang=lang)
                if info.intro:
                    return info.intro.strip()
            except Exception:
                pass  # web 失败 → 沿默认链
        # 回退：api 独立 do=intro（若可用），否则链上任意 info.intro
        api = self._sources.get(Source.api)
        if api is not None and hasattr(api, "fetch_novel_intro"):
            try:
                return await api.fetch_novel_intro(aid, lang=lang)
            except Exception:
                pass  # api 不可用 → 沿默认链 info.intro
        info = await self._try_chain(Capability.NOVEL_INFO, "fetch_novel_info",
                                     None, aid, lang)
        return (info.intro or "").strip()

    async def get_novel_index(self, aid: int, source: Optional[Source | str] = None,
                              lang: Optional[Lang] = None,
                              use_cache: Optional[bool] = None) -> NovelIndex:
        lang = lang or self.default_lang

        async def _impl():
            idx = await self._try_chain(Capability.NOVEL_INDEX, "fetch_novel_index",
                                        source, aid, lang)
            idx, _ = await self._copyright_fallback("fetch_novel_index", idx,
                                                    source, aid, lang)
            return idx

        return await self._cached_call(
            "fetch_novel_index", _impl,
            (aid, source.value if isinstance(source, Source) else source, lang.value),
            None, use_cache)

    async def get_novel_content(self, aid: int, cid: int,
                                source: Optional[Source | str] = None,
                                lang: Optional[Lang] = None,
                                use_cache: Optional[bool] = None) -> NovelContent:
        lang = lang or self.default_lang

        async def _impl():
            ct = await self._try_chain(Capability.NOVEL_CONTENT, "fetch_novel_content",
                                       source, aid, cid, lang)
            ct, _ = await self._copyright_fallback("fetch_novel_content", ct,
                                                   source, aid, cid, lang)
            return ct

        return await self._cached_call(
            "fetch_novel_content", _impl,
            (aid, cid, source.value if isinstance(source, Source) else source, lang.value),
            None, use_cache)

    async def get_full_novel_content(self, aid: int,
                                     source: Optional[Source | str] = None,
                                     lang: Optional[Lang] = None) -> str:
        """整本 TXT。默认走 CDN 源（若启用），否则沿业务链的 NOVEL_FULL。"""
        if self.cdn is not None and source is None:
            return await self.cdn.fetch_full_novel_content(aid, lang or self.default_lang)
        return await self._try_chain(Capability.NOVEL_FULL, "fetch_novel_full",
                                     source, aid, lang or self.default_lang)

    async def get_novel_cover(self, aid: int,
                              source: Optional[Source | str] = None) -> bytes:
        if self.cdn is not None and source is None:
            return await self.cdn.fetch_novel_cover(aid)
        src = self.source(source) if source else None
        if src is None:
            raise SourceUnavailableException(str(source), "无封面来源")
        if Capability.NOVEL_COVER not in src.capabilities:
            raise SourceUnavailableException(str(source), "该来源不支持封面")
        # api 源走 relay 高清封面；其它源若有 fetch_novel_cover 亦调用
        fn = getattr(src, "fetch_novel_cover", None)
        if fn is None:
            raise SourceUnavailableException(str(source), "该来源未实现 fetch_novel_cover")
        return await fn(aid)

    async def get_picture(self, url: str,
                          source: Optional[Source | str] = None) -> bytes:
        """下载任意图片 URL（插图/封面原图）。默认走 CDN 源。

        api 源只提供 relay 字节流（fetch_novel_cover），不下载外部 URL；
        如需通过某来源下载图片请传 cdn/web 源。
        """
        if self.cdn is not None and source is None:
            return await self.cdn.get_picture(url)
        src = self.source(source) if source else None
        if src is None:
            raise SourceUnavailableException(str(source), "无图片来源")
        fn = getattr(src, "get_picture", None)
        if fn is None:
            # api 源无 get_picture → 明确错误提示改用 cdn
            raise SourceUnavailableException(
                str(source), "该来源不支持外部图片下载（请用 cdn 源）")
        return await fn(url)

    async def get_novel_bookinfo(self, aid: int,
                                 source: Optional[Source | str] = None,
                                 lang: Optional[Lang] = None,
                                 use_cache: Optional[bool] = None):
        """列表项短信息（relay do=bookinfo，比 meta 轻）。api 源实现。"""
        lang = lang or self.default_lang
        return await self._cached_call(
            "fetch_novel_bookinfo",
            lambda: self._try_chain(Capability.NOVEL_INFO, "fetch_novel_bookinfo",
                                    source, aid, lang),
            (aid, source.value if isinstance(source, Source) else source, lang.value),
            None, use_cache)

    async def search_novel(self, keyword: str, method: SearchMethod = SearchMethod.NAME,
                           page: int = 1, source: Optional[Source | str] = None,
                           lang: Optional[Lang] = None,
                           use_cache: Optional[bool] = None) -> SearchResult:
        lang = lang or self.default_lang
        return await self._cached_call(
            "fetch_search",
            lambda: self._try_chain(Capability.SEARCH, "fetch_search",
                                    source, keyword, method, page, lang),
            (keyword, method.value, page,
             source.value if isinstance(source, Source) else source, lang.value),
            None, use_cache)

    async def search_novel_by_name(self, keyword: str, page: int = 1,
                                   source: Optional[Source | str] = None,
                                   lang: Optional[Lang] = None) -> SearchResult:
        return await self.search_novel(keyword, SearchMethod.NAME, page, source, lang)

    async def search_novel_by_author(self, keyword: str, page: int = 1,
                                     source: Optional[Source | str] = None,
                                     lang: Optional[Lang] = None) -> SearchResult:
        return await self.search_novel(keyword, SearchMethod.AUTHOR, page, source, lang)

    async def get_novel_list(self, sort, page: int = 1,
                             source: Optional[Source | str] = None,
                             lang: Optional[Lang] = None,
                             use_cache: Optional[bool] = None) -> SearchResult:
        lang = lang or self.default_lang
        return await self._cached_call(
            "fetch_novel_list",
            lambda: self._try_chain(Capability.NOVEL_LIST, "fetch_novel_list",
                                    source, sort, page, lang),
            (sort, page, source.value if isinstance(source, Source) else source, lang.value),
            None, use_cache)

    async def get_bookshelf(self, source: Optional[Source | str] = None,
                            lang: Optional[Lang] = None,
                            use_cache: Optional[bool] = None) -> list[Book]:
        lang = lang or self.default_lang
        return await self._cached_call(
            "fetch_bookshelf",
            lambda: self._try_chain(Capability.BOOKSHELF, "fetch_bookshelf",
                                    source, lang),
            (source.value if isinstance(source, Source) else source, lang.value),
            None, use_cache)

    # ---- 写操作（书架增删/推荐）----
    # 写操作目前仅 api(relay) 源实现；web 网页版写操作未实现（表单 CSRF 等），
    # 故这里明确只用 api 源，若调用方指定其它源则报清晰错误而非空转。
    async def bookshelf_add(self, aid: int,
                            source: Optional[Source | str] = None) -> int:
        """加入书架（api 源）。返回服务端码，1=成功。"""
        return await self._write_op("bookshelf_add", aid, source, "加入书架")

    async def bookshelf_del(self, aid: int,
                            source: Optional[Source | str] = None) -> int:
        """移出书架（api 源）。返回服务端码，1=成功。"""
        return await self._write_op("bookshelf_del", aid, source, "移出书架")

    async def vote_novel(self, aid: int,
                         source: Optional[Source | str] = None) -> int:
        """推荐小说（api 源，App 日限 5 次）。返回服务端码，1=成功。"""
        return await self._write_op("vote_novel", aid, source, "推荐")

    async def _write_op(self, method_name: str, aid: int,
                        source: Optional[Source | str],
                        label: str) -> int:
        """执行仅 api 源支持的写操作。source 未指定→api；指定非 api→明确报错。"""
        if source is not None:
            src = self.source(source) if isinstance(source, str) else \
                self._sources.get(source)
            if src is None or not hasattr(src, method_name):
                raise SourceUnavailableException(
                    str(source), f"{label}: 该来源不支持（仅 api 源实现）")
            return await getattr(src, method_name)(aid)
        api = self._sources.get(Source.api)
        if api is None or not hasattr(api, method_name):
            raise SourceUnavailableException("api", f"{label}: api 源不可用")
        return await getattr(api, method_name)(aid)

    async def clear_cache(self) -> None:
        if self._cache is not None:
            await self._cache.clear()

    # ---- 会话 ----
    async def close(self) -> None:
        for src in self._sources.values():
            try:
                await src.close()
            except Exception:
                pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()
        return False

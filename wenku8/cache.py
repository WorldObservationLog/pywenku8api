import functools
import hashlib
import inspect
import pickle
import random
import time
import zlib

import aiosqlite


def with_cache(expires_days=None):
    """
    基于 aiosqlite 的异步缓存装饰器。
    :param expires_days: 缓存有效期（天）。若为None则不过期。
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            # 1. 如果缓存被禁用或者未配置，直接调用原函数
            if not getattr(self, "enable_cache", False):
                return await func(self, *args, **kwargs)

            # 获取数据库路径，默认为 .wenku8_cache.db
            db_path = getattr(self, "cache_db_path", ".wenku8_cache.db")

            # 2. 统一并正规化调用参数
            sig = inspect.signature(func)
            try:
                bound_args = sig.bind(self, *args, **kwargs)
                bound_args.apply_defaults()
            except TypeError:
                # 绑定失败时直接透传给原函数
                return await func(self, *args, **kwargs)

            # 移除 self 避免由于其内存地址变化导致缓存键改变
            if 'self' in bound_args.arguments:
                bound_args.arguments.pop('self')

            # 3. 构建确定性的字符串表示形式
            args_list = []
            for k, v in sorted(bound_args.arguments.items()):
                args_list.append(f"{k}={repr(v)}")

            args_str = "::".join(args_list)
            cache_key_str = f"{func.__name__}::{args_str}"
            # 生成固定长度的数据库主键
            key_hash = hashlib.sha256(cache_key_str.encode('utf-8')).hexdigest()

            # 4. 尝试读取缓存
            try:
                async with aiosqlite.connect(db_path, timeout=10.0) as db:
                    await db.execute(
                        "CREATE TABLE IF NOT EXISTS cache ("
                        "key TEXT PRIMARY KEY, "
                        "value BLOB, "
                        "expires_at REAL"
                        ")"
                    )

                    async with db.execute("SELECT value, expires_at FROM cache WHERE key = ?", (key_hash,)) as cursor:
                        row = await cursor.fetchone()
                        if row:
                            value_blob, expires_at = row
                            # 如果未过期或者无过期时间，则尝试解压及反序列化
                            if expires_at is None or expires_at > time.time():
                                try:
                                    decompressed = zlib.decompress(value_blob)
                                    return pickle.loads(decompressed)
                                except Exception:
                                    # 缓存损坏或反序列化失败，直接发起新请求
                                    pass
            except Exception:
                # 数据库异常不应该阻断正常请求流程
                pass

            # 5. 执行原始方法以获取最新数据
            result = await func(self, *args, **kwargs)

            # 6. 异步写入缓存
            try:
                value_blob = zlib.compress(pickle.dumps(result), level=9)
                expires_at = time.time() + expires_days * 24 * 3600 if expires_days else None

                async with aiosqlite.connect(db_path, timeout=10.0) as db:
                    # 并发下 CREATE TABLE IF NOT EXISTS 可能导致小概率竞争，但是上面已经尝试过一次了
                    # 避免极端情况未建表，确保插入安全
                    await db.execute(
                        "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
                        (key_hash, value_blob, expires_at)
                    )
                    
                    # 7. 以1%的概率发起过期数据清理任务，防范空间无限膨胀
                    if random.random() < 0.01:
                        await db.execute(
                            "DELETE FROM cache WHERE expires_at IS NOT NULL AND expires_at < ?", 
                            (time.time(),)
                        )

                    await db.commit()
            except Exception:
                pass

            return result

        async def invalidate_cache(self_obj, *args, **kwargs):
            if not getattr(self_obj, "enable_cache", False):
                return
            db_path = getattr(self_obj, "cache_db_path", ".wenku8_cache.db")
            sig = inspect.signature(func)
            try:
                bound_args = sig.bind(self_obj, *args, **kwargs)
                bound_args.apply_defaults()
            except TypeError:
                return

            if 'self' in bound_args.arguments:
                bound_args.arguments.pop('self')

            args_list = []
            for k, v in sorted(bound_args.arguments.items()):
                args_list.append(f"{k}={repr(v)}")

            args_str = "::".join(args_list)
            cache_key_str = f"{func.__name__}::{args_str}"
            key_hash = hashlib.sha256(cache_key_str.encode('utf-8')).hexdigest()

            try:
                async with aiosqlite.connect(db_path, timeout=10.0) as db:
                    await db.execute("DELETE FROM cache WHERE key = ?", (key_hash,))
                    await db.commit()
            except Exception:
                pass

        wrapper.invalidate_cache = invalidate_cache
        return wrapper
    return decorator


class CacheDaemon:
    def __init__(self, api_instance):
        self.api = api_instance
        self.task = None
        self.last_polled_dict = None

    async def _load_last_polled_dict(self):
        if not self.api.enable_cache:
            return {}
        try:
            import aiosqlite
            import pickle
            import zlib
            async with aiosqlite.connect(self.api.cache_db_path, timeout=10.0) as db:
                await db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value BLOB, expires_at REAL)")
                async with db.execute("SELECT value FROM cache WHERE key = ?", ("__last_polled_dict__",)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return pickle.loads(zlib.decompress(row[0]))
        except Exception:
            pass
        return {}

    async def _save_last_polled_dict(self, current_dict):
        if not self.api.enable_cache:
            return
        try:
            import aiosqlite
            import pickle
            import zlib
            async with aiosqlite.connect(self.api.cache_db_path, timeout=10.0) as db:
                await db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value BLOB, expires_at REAL)")
                value_blob = zlib.compress(pickle.dumps(current_dict), level=9)
                await db.execute("INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)", ("__last_polled_dict__", value_blob, None))
                await db.commit()
        except Exception:
            pass

    async def _loop(self, interval: int):
        import asyncio
        import logging
        from wenku8.consts import NovelSortMethod, Lang
        logger = logging.getLogger(__name__)

        if self.last_polled_dict is None:
            self.last_polled_dict = await self._load_last_polled_dict()

        while True:
            try:
                page = 1
                current_poll_items = {}
                updated_aids = []

                while True:
                    result = await self.api.get_novel_list(sort=NovelSortMethod.lastUpdate, page=page)
                    if not result.results:
                        break

                    page_has_overlap_with_old = False
                    for item in result.results:
                        current_poll_items[item.aid] = item
                        if item.aid in self.last_polled_dict:
                            page_has_overlap_with_old = True
                            old_item = self.last_polled_dict[item.aid]
                            if item.word_count != old_item.word_count or item.last_updated != old_item.last_updated:
                                updated_aids.append(item.aid)
                        else:
                            updated_aids.append(item.aid)

                    if not page_has_overlap_with_old and self.last_polled_dict:
                        if page >= result.page_control.end:
                            break
                        page += 1
                        await asyncio.sleep(1)
                    else:
                        break

                if self.last_polled_dict:
                    for aid in set(updated_aids):
                        for lang in (Lang.zh_CN, Lang.zh_TW):
                            try:
                                old_index = await self.api.get_novel_index(aid, lang)
                                for vol in old_index.volumes:
                                    for ch in vol.chapters:
                                        await self.api.get_novel_content_via_full.invalidate_cache(self.api, aid=int(aid), cid=int(ch.cid), lang=lang)
                                        await self.api.get_novel_content.invalidate_cache(self.api, aid=int(aid), cid=int(ch.cid), lang=lang)

                                await self.api.get_full_novel_content.invalidate_cache(self.api, aid=int(aid), lang=lang)
                                await self.api.get_novel_info.invalidate_cache(self.api, aid=int(aid), lang=lang)
                                await self.api.get_novel_index.invalidate_cache(self.api, aid=int(aid), lang=lang)
                            except Exception as e:
                                logger.error(f"Failed to invalidate cache for aid {aid}: {e}")

                self.last_polled_dict = current_poll_items
                await self._save_last_polled_dict(self.last_polled_dict)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cache daemon encountered an error: {e}")

            await asyncio.sleep(interval)

    def start(self, interval: int = 3600):
        if not self.api.enable_cache:
            import logging
            logging.getLogger(__name__).warning("Cache is not enabled. Daemon will not run.")
            return
        if self.task is None or self.task.done():
            import asyncio
            self.task = asyncio.get_running_loop().create_task(self._loop(interval))

    def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            self.task = None

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
        return wrapper
    return decorator

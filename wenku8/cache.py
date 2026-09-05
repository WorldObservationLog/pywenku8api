"""缓存层：内存 TTL + 可选磁盘持久化。

用途：降低对来源的请求量（间接降低限速压力）。默认不启用；由 Wenku8Client
按 `cache=True/disk_cache_dir=...` 开启，业务方法用 `use_cache` 参数控制单次调用。

设计：
- 键：方法 + 参数（aid/lang/source 等）规范化串。
- 值：dataclass / str / bytes 均存；磁盘以 JSON 存（dataclass 走 asdict），
  bytes 单独存 .bin。
- 失效：内存按单调时钟 TTL 惰性失效；磁盘带写入时间戳，读时校验。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

_DEFAULT_TTL: dict[str, float] = {
    "fetch_novel_info": 30 * 60,
    "fetch_novel_intro": 30 * 60,
    "fetch_novel_bookinfo": 30 * 60,
    "fetch_novel_index": 30 * 60,
    "fetch_novel_content": 60 * 60,
    "fetch_novel_list": 10 * 60,
    "fetch_search": 5 * 60,
    "fetch_bookshelf": 2 * 60,
}


class Cache:
    def __init__(self, *, ttl_overrides: Optional[dict[str, float]] = None,
                 disk_dir: Optional[str | Path] = None):
        self._ttl = dict(_DEFAULT_TTL)
        if ttl_overrides:
            self._ttl.update(ttl_overrides)
        self._mem: dict[str, tuple[float, Any]] = {}
        self._disk_dir: Optional[Path] = Path(disk_dir) if disk_dir else None
        if self._disk_dir:
            self._disk_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    # ---- 内部 ----
    def ttl_for(self, method: str) -> float:
        return self._ttl.get(method, 0.0)

    @staticmethod
    def _key(method: str, args: tuple, kwargs: dict) -> str:
        raw = json.dumps({"m": method, "a": list(args), "k": kwargs},
                         ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _disk_path(self, key: str) -> Path:
        return self._disk_dir / f"{key}.json"

    # ---- 主接口 ----
    async def get(self, method: str, args: tuple = (), kwargs: Optional[dict] = None) -> Optional[Any]:
        kwargs = kwargs or {}
        key = self._key(method, args, kwargs)
        ttl = self.ttl_for(method)
        if ttl <= 0:
            return None
        async with self._lock:
            hit = self._mem.get(key)
            if hit:
                exp, val = hit
                if time.monotonic() < exp:
                    return val
                del self._mem[key]
        if self._disk_dir:
            val = await asyncio.to_thread(self._disk_read, key, ttl)
            if val is not None:
                async with self._lock:
                    self._mem[key] = (time.monotonic() + ttl, val)
                return val
        return None

    async def set(self, method: str, value: Any, args: tuple = (),
                  kwargs: Optional[dict] = None) -> None:
        kwargs = kwargs or {}
        ttl = self.ttl_for(method)
        if ttl <= 0:
            return
        key = self._key(method, args, kwargs)
        async with self._lock:
            self._mem[key] = (time.monotonic() + ttl, value)
        if self._disk_dir:
            await asyncio.to_thread(self._disk_write, key, value, ttl)

    def _disk_read(self, key: str, ttl: float) -> Optional[Any]:
        p = self._disk_path(key)
        if not p.exists():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if "ts" not in raw or time.time() - raw["ts"] > ttl:
            return None
        return _deserialize(raw.get("value"))

    def _disk_write(self, key: str, value: Any, ttl: float) -> None:
        try:
            payload = {"ts": time.time(), "ttl": ttl, "value": _serialize(value)}
            p = self._disk_path(key)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str),
                           encoding="utf-8")
            os.replace(tmp, p)
        except Exception:
            pass

    async def clear(self) -> None:
        async with self._lock:
            self._mem.clear()
        if self._disk_dir:
            await asyncio.to_thread(
                lambda: [p.unlink() for p in self._disk_dir.glob("*.json")])


def _serialize(value: Any) -> Any:
    """dataclass → dict（带类型标记）；bytes → base64 标记；list 递归。"""
    if is_dataclass(value):
        cls = type(value)
        return {"__type__": f"{cls.__module__}.{cls.__name__}",
                "fields": {k: _serialize(v) for k, v in asdict(value).items()}}
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if isinstance(value, tuple):
        return [_serialize(v) for v in value]
    if isinstance(value, bytes):
        import base64
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    return value


def _deserialize(value: Any) -> Any:
    if isinstance(value, dict):
        if "__bytes__" in value:
            import base64
            try:
                return base64.b64decode(value["__bytes__"])
            except Exception:
                return value
        if "__type__" in value:
            fields = value.get("fields", {})
            mod_name, cls_name = value["__type__"].rsplit(".", 1)
            try:
                import importlib
                mod = importlib.import_module(mod_name)
                cls = getattr(mod, cls_name)
                return cls(**{k: _deserialize(v) for k, v in fields.items()})
            except Exception:
                return fields
        return {k: _deserialize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deserialize(v) for v in value]
    return value

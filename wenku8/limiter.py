"""限速 / 退避 / 熔断。

设计（对应已批准方案 §四）：
- 每个 (来源, 作用域) 一个 token bucket：rps（每秒补充速率）、burst（桶容量）。
- 支持「全局限速」总闸 + 「来源级」独立配额。
- 429/403(封禁) 触发指数退避并把该来源置为「熔断」，冷却到期自动恢复（探测）。
- 一切等待在 asyncio 中进行；对并发协程安全。

默认参数来自 docs/cf_rate_limit_research.md 的研究结论：
  HTTP 指纹层对 wenku8 根/登录/书架等非质询路径可 1s 间隔连发（实测 8 连发 200）；
  深页路径当前出口全部命中 Managed Challenge（非速率所致），故不靠提高 rps 解决，
  保守起见页面来源默认 1 rps / burst 3，CDN 与 relay 适当放宽。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from wenku8.exceptions import AllSourcesBlockedException, RateLimitException, SourceUnavailableException


@dataclass
class RateLimitConfig:
    """一组限速参数。

    -1 表示“不限速”（用于 relay 等外部限速源或纯本地任务）。
    """

    rps: float = 1.0            # 每秒补充的令牌数
    burst: int = 3              # 桶容量（允许的突发）
    max_wait: float = 120.0     # 排队等待上限（秒），超过抛 RateLimitException
    backoff_base: float = 1.0   # 指数退避初始秒
    backoff_max: float = 60.0   # 退避上限秒
    circuit_breaker_threshold: int = 3   # 连续触发 429/封禁多少次后熔断
    circuit_cooldown: float = 300.0      # 熔断冷却（秒）后自动恢复探测
    enabled: bool = True

    # 常用预置
    @classmethod
    def conservative(cls) -> "RateLimitConfig":
        """最保守（对所有远程来源的默认）。"""
        return cls(rps=1.0, burst=3)

    @classmethod
    def relaxed(cls) -> "RateLimitConfig":
        """较宽松（CDN 封面 / relay 等不易触发者）。"""
        return cls(rps=2.0, burst=8)

    @classmethod
    def unlimited(cls) -> "RateLimitConfig":
        return cls(rps=-1.0, burst=1 << 30, enabled=False)


class _TokenBucket:
    """单桶：asyncio 安全的令牌桶。"""

    def __init__(self, cfg: RateLimitConfig):
        self.cfg = cfg
        self._tokens = float(cfg.burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """尝试取 1 个令牌；若需等待则等待至可用或超过 max_wait 返回 False。"""
        if not self.cfg.enabled or self.cfg.rps <= 0:
            return True
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self.cfg.burst,
                               self._tokens + (now - self._updated) * self.cfg.rps)
            self._updated = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            deficit = (1.0 - self._tokens) / self.cfg.rps
        # 锁外等待，避免持锁睡眠阻塞其他协程取令牌
        if deficit <= 0:
            return True
        if deficit > self.cfg.max_wait:
            return False
        await asyncio.sleep(deficit)
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self.cfg.burst,
                               self._tokens + (now - self._updated) * self.cfg.rps)
            self._updated = now
            self._tokens -= 1.0
            return True


@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    open_until: float = 0.0           # monotonic；0 表示关闭
    last_backoff_until: float = 0.0   # monotonic；退避结束时间


class SourceRateLimiter:
    """来源级限速器：全局桶 + 本来源桶 + 退避/熔断状态机。"""

    def __init__(self, source: str,
                 global_config: Optional[RateLimitConfig] = None,
                 source_config: Optional[RateLimitConfig] = None,
                 label: str = ""):
        self.source = source
        self.label = label or source
        self._global_bucket = _TokenBucket(global_config or RateLimitConfig.conservative())
        self._local_bucket = _TokenBucket(source_config or RateLimitConfig.conservative())
        self._circuit = _CircuitState()
        self._recovery_lock = asyncio.Lock()

    @property
    def is_circuit_open(self) -> bool:
        c = self._circuit
        if c.open_until and time.monotonic() > c.open_until:
            # 冷却到期：允许一次探测请求（由调用方触发）
            c.open_until = 0.0
            c.consecutive_failures = 0
        return bool(c.open_until)

    def _still_backing_off(self) -> bool:
        c = self._circuit
        if c.last_backoff_until and time.monotonic() < c.last_backoff_until:
            return True
        c.last_backoff_until = 0.0
        return False

    async def wait_ready(self) -> None:
        """等待令牌与退避/熔断就绪；超时抛 RateLimitException。

        熔断打开期间直接抛 SourceUnavailableException（由上层做来源切换）。
        """
        if self.is_circuit_open:
            raise SourceUnavailableException(self.source, "熔断冷却中")
        if self._still_backing_off():
            await asyncio.sleep(self._circuit.last_backoff_until - time.monotonic())
        if not await self._global_bucket.acquire():
            raise RateLimitException("全局令牌等待超时", source=self.source)
        if not await self._local_bucket.acquire():
            raise RateLimitException("来源级令牌等待超时", source=self.source)

    def report_success(self) -> None:
        self._circuit.consecutive_failures = 0
        self._circuit.last_backoff_until = 0.0

    def report_rate_limited(self) -> None:
        """记录一次 429 / 封禁：指数退避 + 熔断计数。"""
        c = self._circuit
        c.consecutive_failures += 1
        # 指数退避：base * 2^(n-1)，封顶 backoff_max
        delay = min(self._local_bucket.cfg.backoff_max,
                    self._local_bucket.cfg.backoff_base * (2 ** (c.consecutive_failures - 1)))
        c.last_backoff_until = time.monotonic() + delay
        if c.consecutive_failures >= self._local_bucket.cfg.circuit_breaker_threshold:
            c.open_until = time.monotonic() + self._local_bucket.cfg.circuit_cooldown

    def report_error(self) -> None:
        """非限流类错误不触发熔断，只轻微退避一次。"""
        c = self._circuit
        c.last_backoff_until = time.monotonic() + min(
            self._local_bucket.cfg.backoff_base, self._local_bucket.cfg.backoff_max)

    def reset(self) -> None:
        self._circuit = _CircuitState()

    def remaining_cooldown(self) -> float:
        """熔断打开后剩余冷却秒数；未熔断返回 0。"""
        c = self._circuit
        if c.open_until:
            rem = c.open_until - time.monotonic()
            return max(rem, 0.0)
        return 0.0


class ChainCircuitBreaker:
    """跨优先级链的总熔断/记录：任何来源的成功会重置其计数。"""

    def __init__(self, sources: list[str]):
        self._limiter_by_source: dict[str, SourceRateLimiter] = {}
        self._sources = sources

    def register(self, limiter: SourceRateLimiter) -> None:
        self._limiter_by_source[limiter.source] = limiter

    def limiter(self, source: str) -> Optional[SourceRateLimiter]:
        return self._limiter_by_source.get(source)

    def raise_if_all_blocked(self, operation: str) -> None:
        if self._sources and all(self.limiter(s) is None or self.limiter(s).is_circuit_open
                                 for s in self._sources):
            raise AllSourcesBlockedException(operation,
                                             {s: "circuit_open" for s in self._sources})

    def min_cooldown(self, sources: Optional[list[str]] = None) -> float:
        """给定来源列表中所有熔断者的最短剩余冷却；无熔断者返回 0。
        用于"全部熔断"时决定自动恢复前的最短等待。"""
        srcs = sources if sources is not None else self._sources
        rms = [self.limiter(s).remaining_cooldown() for s in srcs
               if self.limiter(s) is not None]
        return min(rms) if rms else 0.0

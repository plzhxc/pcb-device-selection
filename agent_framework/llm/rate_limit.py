"""请求速率管理。

限流的正确作用域是**一份配额**，而不是一个客户端实例。厂商按 API key 计算配额，
因此同一个 key 上的所有调用方必须共用同一个 :class:`RateLimiter`；每个 client 各自
限流会让总请求量按实例数成倍超发。

当前只管**提交速率**，两个维度：

- **并发**：同时在途的请求数；
- **RPM**：每分钟提交的请求数，60 秒滑动窗口。

不限制 token 吞吐（TPM）。真要加时，滑动窗口里多记一列 token 数即可，但代价是
必须先估算、再用真实 ``usage`` 回填修正——在没有确实撞到 token 配额之前，这份
复杂度不值得。

撞到 429 时仅仅退避重试是治标：源头速率没降，重试还会继续撞。因此
:meth:`RateLimiter.penalize` 会主动下调有效速率，之后随连续成功缓慢恢复。

同步与异步共用同一份账本（普通计数器 + 线程锁），差别只在等待方式：同步用
``sleep``，异步用 ``asyncio.sleep``。二者因此可以安全地共享一个限流器。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable


#: 滑动窗口长度：RPM 以分钟为单位
WINDOW_S = 60.0

#: 等待时的轮询间隔。LLM 调用耗时在百毫秒到数十秒量级，这点开销可忽略。
_POLL_S = 0.01

#: 撞限流后有效速率的下限，避免降到完全不发请求
_MIN_SCALE = 0.1


@dataclass(frozen=True)
class RateLimits:
    """一份配额的上限。

    Attributes:
        concurrency: 同时在途请求数上限。
        rpm: 每分钟提交的请求数；``None`` 表示不限。
        min_interval_s: 相邻两次提交的最小间隔，用于平滑突发。
    """

    concurrency: int = 12
    rpm: int | None = None
    min_interval_s: float = 0.0


@dataclass
class Lease:
    """一次已获准的提交占位。

    必须在请求结束后 :meth:`RateLimiter.release`，否则并发额度会泄漏。
    """

    granted_at: float
    released: bool = False


class RateLimiter:
    """一份配额的速率管理者；线程安全，可被同步与异步调用方共享。"""

    def __init__(
        self,
        limits: RateLimits | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        """
        Args:
            limits: 配额上限。
            clock: 取时函数。
            sleep: 同步等待函数。必须与 ``clock`` 成对替换——只换时钟不换等待，
                注入的假时钟永远不会前进，等待将无法结束。
        """

        self.limits = limits or RateLimits()
        self._clock = clock
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._submitted: deque[float] = deque()
        self._in_flight = 0
        self._last_start = 0.0
        # 有效速率系数：撞限流后下调，连续成功后回升
        self._scale = 1.0
        self._blocked_until = 0.0
        self.admitted = 0
        self.rate_limited = 0

    # ----------------------------- 账本 ----------------------------- #
    def _prune(self, now: float) -> None:
        threshold = now - WINDOW_S
        while self._submitted and self._submitted[0] <= threshold:
            self._submitted.popleft()

    def _effective_rpm(self) -> float:
        if self.limits.rpm is None:
            return float("inf")
        return max(1.0, self.limits.rpm * self._scale)

    def _effective_concurrency(self) -> int:
        """降速系数同样作用于并发。

        只缩 RPM 是不够的：``rpm`` 默认 ``None``（不限），此时缩放乘在无穷上，
        撞了 429 也一点没慢下来，只会继续以原速率撞墙。并发是这份配额唯一
        始终生效的闸门，降速必须落到它头上。
        """

        return max(1, int(self.limits.concurrency * self._scale))

    def _wait_time(self, now: float) -> float:
        """返回还需等待的秒数；``0`` 表示可以立即提交。调用方须持锁。"""

        self._prune(now)

        if now < self._blocked_until:
            return self._blocked_until - now

        if self._in_flight >= self._effective_concurrency():
            # 无法预估在途请求何时返还额度，短轮询即可
            return _POLL_S

        gap = self.limits.min_interval_s - (now - self._last_start)
        if gap > 0:
            return gap

        if len(self._submitted) + 1 > self._effective_rpm():
            oldest_expiry = self._submitted[0] + WINDOW_S - now
            return max(oldest_expiry, _POLL_S)

        return 0.0

    def _admit(self, now: float) -> Lease:
        """调用方须持锁。"""

        self._submitted.append(now)
        self._in_flight += 1
        self._last_start = now
        self.admitted += 1
        return Lease(granted_at=now)

    # ----------------------------- 准入 ----------------------------- #
    def acquire(self) -> Lease:
        """同步准入；额度不足时阻塞等待。"""

        while True:
            with self._lock:
                now = self._clock()
                wait = self._wait_time(now)
                if wait <= 0:
                    return self._admit(now)
            self._sleep(min(wait, 1.0))

    async def aacquire(self) -> Lease:
        """异步准入；等待期间让出事件循环。"""

        while True:
            with self._lock:
                now = self._clock()
                wait = self._wait_time(now)
                if wait <= 0:
                    return self._admit(now)
            await asyncio.sleep(min(wait, 1.0))

    # ----------------------------- 结算 ----------------------------- #
    def release(self, lease: Lease | None) -> None:
        """归还并发额度；重复调用安全。"""

        if lease is None or lease.released:
            return
        with self._lock:
            lease.released = True
            self._in_flight = max(0, self._in_flight - 1)

    def on_success(self) -> None:
        """记录一次成功，缓慢恢复被下调的速率。

        避免一次限流之后长期低速运行。
        """

        with self._lock:
            if self._scale < 1.0:
                self._scale = min(1.0, self._scale * 1.05)

    def penalize(self, retry_after_s: float | None = None) -> None:
        """撞到限流后主动降速。

        优先遵从厂商给出的 ``retry_after``；无论是否给出都下调有效速率，否则
        重试只会继续撞在同一堵墙上。
        """

        with self._lock:
            self.rate_limited += 1
            self._scale = max(_MIN_SCALE, self._scale * 0.5)
            if retry_after_s and retry_after_s > 0:
                self._blocked_until = self._clock() + retry_after_s

    # ----------------------------- 观测 ----------------------------- #
    def snapshot(self) -> dict[str, Any]:
        """当前状态快照，用于观测与排障。"""

        with self._lock:
            now = self._clock()
            self._prune(now)
            return {
                "in_flight": self._in_flight,
                "window_requests": len(self._submitted),
                "scale": round(self._scale, 3),
                "effective_concurrency": self._effective_concurrency(),
                "blocked_for_s": max(0.0, self._blocked_until - now),
                "admitted": self.admitted,
                "rate_limited": self.rate_limited,
            }


# --------------------------------------------------------------------------- #
# 进程级默认配额
# --------------------------------------------------------------------------- #
# 所有 client 默认共用这一份配额。默认共享而非默认无限制，是因为"忘了接线"的
# 后果不对称：共享最多只是稍慢，各自为政则会按实例数成倍超发、直接打爆配额。
#
# 多套凭据（不同厂商或不同 key）各有独立配额时，应显式构造并注入各自的限流器。
_DEFAULT_LIMITS = RateLimits(concurrency=12)
_default_limiter: RateLimiter | None = None
_default_lock = threading.Lock()


def get_default_limiter() -> RateLimiter:
    """返回进程级默认限流器；首次调用时按内置默认值创建。"""

    global _default_limiter
    if _default_limiter is None:
        with _default_lock:
            if _default_limiter is None:
                _default_limiter = RateLimiter(_DEFAULT_LIMITS)
    return _default_limiter


def configure_default_limiter(limits: RateLimits) -> RateLimiter:
    """按实际配额重设进程级限流器，返回新的实例。

    应用应在启动时调用一次，把厂商实际的并发与 RPM 配额告诉框架。
    """

    global _default_limiter
    with _default_lock:
        _default_limiter = RateLimiter(limits)
    return _default_limiter


__all__ = [
    "Lease",
    "RateLimiter",
    "RateLimits",
    "WINDOW_S",
    "configure_default_limiter",
    "get_default_limiter",
]

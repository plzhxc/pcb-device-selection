"""无副作用的重试判定与退避策略。

该模块不 sleep、不写日志，只判断错误是否属于限流，并计算第 N 次重试的等待
时间。真正的等待由 ``runner`` 执行，因此测试可以注入确定性的随机数。
"""

from __future__ import annotations

import random
from typing import Callable


RATE_LIMIT_TOKENS = (
    "429",
    "rate limit",
    "ratelimiterror",
    "limit_burst_rate",
    "insufficient_quota",
    "too many requests",
)


def is_rate_limit_error(error: Exception) -> bool:
    """根据常见 provider 错误文本判断是否发生限流。"""
    message = str(error).lower()
    return any(token in message for token in RATE_LIMIT_TOKENS)


class RetryRuntime:
    """带可注入抖动函数的重试策略，便于离线测试保持确定性。"""

    def __init__(self, uniform: Callable[[float, float], float] = random.uniform) -> None:
        """注入抖动随机函数。"""
        self._uniform = uniform

    def is_rate_limit_error(self, error: Exception) -> bool:
        """实例接口，转发到纯函数判定。"""
        return is_rate_limit_error(error)

    def backoff_seconds(
        self,
        retry_count: int,
        *,
        base: float,
        cap: float,
        jitter: float,
    ) -> float:
        """计算带上限和随机抖动的指数退避秒数。"""
        delay = min(cap, base * (2 ** max(0, retry_count)))
        if jitter > 0:
            delay += self._uniform(0, jitter)
        return max(0.0, delay)

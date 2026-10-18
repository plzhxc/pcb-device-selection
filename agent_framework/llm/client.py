"""统一的 LLM 调用入口。

本模块**不含任何厂商判断**：没有 ``if provider == "openai"``，也不认识任何厂商
字段。它只做与厂商无关的编排——

- 组织"构造请求 → 传输 → 解析结果"的调用序列；
- 并发上限与请求间隔限流；
- 流式整体超时守卫；
- 观测埋点；
- 把一切异常经 provider 归一成 :class:`~agent_framework.llm.errors.LLMError`。

翻译工作全部委托给注入的 :class:`~agent_framework.llm.providers.base.Provider`。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Callable, Sequence

from ..message import Message
from ..tool import ToolChoice, ToolSpec
from ..trace.write.pipeline import emit as emit_event
from ..trace.write.runtime import llm_observer as _default_llm_observer
from .errors import LLMError, LLMRateLimitError, LLMTimeoutError
from .observer import (
    NULL_OBSERVER,
    CallCancelled,
    CallEnded,
    CallFailed,
    CallStarted,
    LLMObserver,
)
from .params import CallParams
from .providers.base import Provider
from .rate_limit import RateLimiter, get_default_limiter
from .response import LLMResponse
from .streaming import ResponseAccumulator, StreamDelta

#: 流式回调：接收一个中立增量事件
OnDelta = Callable[[StreamDelta], Any]


class _Deadline:
    """流式整体耗时守卫。

    SDK 的 timeout 只覆盖连接与单次读取，不限制"整个流跑多久"。缺这道守卫时，
    一个持续缓慢吐字的响应可以远超设定超时而不被中断。
    """

    __slots__ = ("_limit", "_started")

    def __init__(self, timeout_s: float | None) -> None:
        self._limit = timeout_s
        self._started = time.monotonic()

    def check(self) -> None:
        if self._limit is None:
            return
        if time.monotonic() - self._started > self._limit:
            raise LLMTimeoutError(f"流式响应超过整体超时（{self._limit}s）")


class _CallTrace:
    """单次调用的观测埋点。

    同步与异步共用同一套计时口径，保证两条路径产出的运行时数据可比。
    观察者自身的异常一律吞掉：埋点不能影响主流程。
    """

    __slots__ = (
        "_observer",
        "_llm_id",
        "_logical_call_id",
        "_provider",
        "_params",
        "_stream",
        "_attempt",
        "_max_attempts",
        "_started",
        "_ttft_ms",
        "_chunks",
    )

    def __init__(
        self,
        observer: LLMObserver,
        *,
        provider: str,
        params: CallParams,
        stream: bool,
        attempt: int,
        max_attempts: int,
        logical_call_id: str | None,
    ) -> None:
        self._observer = observer
        self._llm_id = uuid.uuid4().hex
        self._logical_call_id = logical_call_id or self._llm_id
        self._provider = provider
        self._params = params
        self._stream = stream
        self._attempt = attempt
        self._max_attempts = max_attempts
        self._started = time.monotonic()
        self._ttft_ms: int | None = None
        self._chunks: int | None = None

    @property
    def llm_id(self) -> str:
        return self._llm_id

    def _elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    def _notify(self, hook: str, event: Any) -> None:
        emit_event(self._observer, hook, event)

    def start(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> None:
        self._started = time.monotonic()
        self._notify(
            "on_start",
            CallStarted(
                llm_id=self._llm_id,
                provider=self._provider,
                model=self._params.model,
                messages=messages,
                tools=tools,
                params=self._params.digest(),
                attempt=self._attempt,
                logical_call_id=self._logical_call_id,
            ),
        )

    def observe_delta(self, delta: StreamDelta | None) -> None:
        """记录一个流式增量；首个带内容的增量确定首字延迟。"""

        self._chunks = (self._chunks or 0) + 1
        if delta is not None and delta.has_payload and self._ttft_ms is None:
            self._ttft_ms = self._elapsed_ms()

    def end(self, response: LLMResponse) -> None:
        self._notify(
            "on_end",
            CallEnded(
                llm_id=self._llm_id,
                provider=self._provider,
                model=response.model or self._params.model,
                response=response,
                duration_ms=self._elapsed_ms(),
                ttft_ms=self._ttft_ms,
                chunk_count=self._chunks,
                params=self._params.digest(),
                attempt=self._attempt,
                logical_call_id=self._logical_call_id,
            ),
        )

    def fail(self, error: LLMError, *, will_retry: bool | None = None) -> None:
        if will_retry is None:
            will_retry = bool(
                getattr(error, "retryable", False)
                and self._attempt + 1 < self._max_attempts
            )
        self._notify(
            "on_error",
            CallFailed(
                llm_id=self._llm_id,
                provider=self._provider,
                model=self._params.model,
                error=str(error),
                error_type=type(error).__name__,
                retryable=getattr(error, "retryable", False),
                status_code=getattr(error, "status_code", None),
                duration_ms=self._elapsed_ms(),
                params=self._params.digest(),
                attempt=self._attempt,
                will_retry=will_retry,
                logical_call_id=self._logical_call_id,
            ),
        )

    def cancel(self, reason: str = "cancelled") -> None:
        self._notify(
            "on_cancel",
            CallCancelled(
                llm_id=self._llm_id,
                logical_call_id=self._logical_call_id,
                provider=self._provider,
                model=self._params.model,
                reason=reason,
                duration_ms=self._elapsed_ms(),
                params=self._params.digest(),
                attempt=self._attempt,
                phase="in_flight",
            ),
        )


def _validate_trace_identity(
    logical_call_id: str | None,
    *,
    attempt: int,
    max_attempts: int,
) -> None:
    if logical_call_id is not None and (
        not isinstance(logical_call_id, str) or not logical_call_id.strip()
    ):
        raise ValueError("logical_call_id must be a non-empty string or None")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise ValueError("attempt must be a non-negative integer")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts < 1
    ):
        raise ValueError("max_attempts must be a positive integer")


def _cancel_backoff_trace(
    observer: LLMObserver,
    *,
    logical_call_id: str,
    provider: str,
    model: str,
    attempt: int,
    reason: str,
) -> None:
    emit_event(
        observer,
        "on_cancel",
        CallCancelled(
            llm_id="",
            logical_call_id=logical_call_id,
            provider=provider,
            model=model,
            reason=reason,
            attempt=attempt,
            phase="backoff",
        ),
    )


class StreamCallbackError(Exception):
    """流式回调自己抛出的异常。

    单独包一层是为了归因：回调是**调用方的代码**，它炸了不代表模型调用失败。
    混在一起会让观测里出现一条根本不存在的"LLM 失败"，还会触发针对模型的重试，
    而重试解决不了调用方的 bug。
    """

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"流式回调 on_delta 抛出异常：{type(cause).__name__}: {cause}")
        self.cause = cause


def _run_callback(on_delta: OnDelta, delta: Any) -> Any:
    try:
        return on_delta(delta)
    except Exception as exc:
        raise StreamCallbackError(exc) from exc


async def _await_callback(outcome: Any) -> None:
    """等待协程式回调。

    回调写成 ``async def`` 时，调用它只是拿到协程，真正的异常要到 await 才抛。
    只包住调用那一半，异步回调的 bug 就仍会被算成模型调用失败——而这正是回调
    最常见的写法。
    """

    try:
        await outcome
    except Exception as exc:
        raise StreamCallbackError(exc) from exc


def _close_stream(chunks: Any) -> None:
    close = getattr(chunks, "close", None)
    if close is not None:
        try:
            close()
        except Exception:
            pass


async def _aclose_stream(chunks: Any) -> None:
    close = getattr(chunks, "aclose", None) or getattr(chunks, "close", None)
    if close is None:
        return
    try:
        outcome = close()
        if hasattr(outcome, "__await__"):
            await outcome
    except Exception:
        pass


class LLMClient:
    """同步调用入口。

    速率由 ``limiter`` 管理，**默认取进程级共享配额**——不传就自动共用同一份，
    因此不会出现"忘了接线导致每个实例各发各的"。仅当存在多套独立配额（不同厂商
    或不同 key）时才需要显式注入各自的限流器。
    """

    def __init__(
        self,
        provider: Provider,
        params: CallParams,
        *,
        observer: LLMObserver | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.provider = provider
        self.params = params
        self.observer = observer or _default_llm_observer() or NULL_OBSERVER
        self._limiter = limiter

    @property
    def limiter(self) -> RateLimiter:
        """本次调用生效的限流器。

        未显式注入时**每次访问都取当前的进程级配额**，而不是构造时快照一次——
        否则"先建 client、后配置配额"的顺序会让配置对已有实例失效，而这个顺序
        在真实应用里很常见（模块导入期建对象，启动期读配置）。

        注意：一次调用内部必须**取一次、用到底**（见 ``call`` 里的局部变量）。
        取两次的话，中途换了进程级配额就会把 lease 还给新的限流器——旧的那份
        并发额度再也回不来，攒够次数整个进程就卡死在等待上。
        """

        return self._limiter if self._limiter is not None else get_default_limiter()

    def call(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice | None = None,
        stream: bool = False,
        on_delta: OnDelta | None = None,
        attempt: int = 0,
        max_attempts: int = 1,
        logical_call_id: str | None = None,
        **overrides: Any,
    ) -> LLMResponse:
        """发起一次调用。

        ``overrides`` 按 :meth:`CallParams.merged` 语义临时覆盖参数，例如
        ``model=...`` 或 ``timeout_s=...``。
        """

        _validate_trace_identity(
            logical_call_id,
            attempt=attempt,
            max_attempts=max_attempts,
        )
        params = self.params.merged(**overrides)
        limiter = self.limiter  # 取一次用到底：acquire 与 release 必须是同一份账本
        lease = limiter.acquire()
        trace = _CallTrace(
            self.observer,
            provider=self.provider.name,
            params=params,
            stream=stream,
            attempt=attempt,
            max_attempts=max_attempts,
            logical_call_id=logical_call_id,
        )
        trace.start(messages, tools)
        try:
            request = self.provider.build_request(
                messages=messages,
                tools=tools,
                params=params,
                stream=stream,
                tool_choice=tool_choice,
            )
            if not stream:
                result = self.provider.parse_response(self.provider.complete(request))
            else:
                accumulator = ResponseAccumulator(
                    provider=self.provider.name, model=params.model
                )
                deadline = _Deadline(params.timeout_s)
                # 超时、回调抛错、调用方取消都会从循环中途退出；不显式关闭的话
                # 底层 HTTP 响应体一直挂着，连接不还池，跑久了耗尽连接数。
                chunks = self.provider.stream(request)
                try:
                    for chunk in chunks:
                        deadline.check()
                        delta = accumulator.add(self.provider.parse_delta(chunk))
                        trace.observe_delta(delta)
                        if on_delta is not None and delta is not None:
                            _run_callback(on_delta, delta)
                finally:
                    _close_stream(chunks)
                result = accumulator.result()
        except StreamCallbackError as error:
            # 回调是调用方的代码，不是模型调用失败：原样抛出，不入失败观测、
            # 不触发针对模型的重试。
            trace.fail(LLMError(str(error), provider=self.provider.name))
            raise error.cause from None
        except asyncio.CancelledError:
            # 取消不是失败：归一成 LLMError 会让 wait_for 超时、Ctrl+C、
            # gather 取消兄弟任务全部失效——调用方以为停了，实际还在跑、还在烧配额。
            trace.cancel("CancelledError")
            raise
        except Exception as error:
            failure = self._as_llm_error(error, params)
            self._penalize(limiter, failure)
            trace.fail(failure)
            raise failure from error
        except BaseException as interruption:
            # 同步调用没有统一的 CancelledError；KeyboardInterrupt/SystemExit 等
            # 外部中断同样要闭合已经开始的 Trace，再保持原异常语义向上传播。
            trace.cancel(type(interruption).__name__)
            raise
        finally:
            limiter.release(lease)
        limiter.on_success()
        trace.end(result)
        return result

    def cancel_logical_call(
        self,
        logical_call_id: str,
        *,
        attempt: int,
        reason: str = "cancelled during retry backoff",
    ) -> None:
        """闭合已经安排重试、但在下一次物理调用前被中断的 Trace。"""

        _validate_trace_identity(
            logical_call_id,
            attempt=attempt,
            max_attempts=max(1, attempt + 1),
        )
        _cancel_backoff_trace(
            self.observer,
            logical_call_id=logical_call_id,
            provider=self.provider.name,
            model=self.params.model,
            attempt=attempt,
            reason=reason,
        )

    def _penalize(self, limiter: RateLimiter, error: LLMError) -> None:
        """撞到限流时下调速率——只重试不降速，会持续撞在同一堵墙上。"""

        if isinstance(error, LLMRateLimitError):
            limiter.penalize(error.retry_after_s)

    def _as_llm_error(self, error: BaseException, params: CallParams) -> LLMError:
        """统一异常出口；已是中立异常则补全上下文后原样返回。"""

        if isinstance(error, LLMError):
            error.provider = error.provider or self.provider.name
            error.model = error.model or params.model
            return error
        return self.provider.map_error(error, model=params.model)


class AsyncLLMClient:
    """异步调用入口。

    速率由 ``limiter`` 管理，**默认取进程级共享配额**。同步与异步 client 共用同一
    份并发与 RPM 账本，因此混用两种客户端也不会突破总量。限流只管**提交速率**，
    不限 token：token 上限由各家按 key 计费侧管，我们这层拦不准也没必要拦。
    """

    def __init__(
        self,
        provider: Provider,
        params: CallParams,
        *,
        observer: LLMObserver | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.provider = provider
        self.params = params
        self.observer = observer or _default_llm_observer() or NULL_OBSERVER
        self._limiter = limiter

    @property
    def limiter(self) -> RateLimiter:
        """本次调用生效的限流器。

        未显式注入时**每次访问都取当前的进程级配额**，而不是构造时快照一次——
        否则"先建 client、后配置配额"的顺序会让配置对已有实例失效，而这个顺序
        在真实应用里很常见（模块导入期建对象，启动期读配置）。

        注意：一次调用内部必须**取一次、用到底**（见 ``call`` 里的局部变量）。
        取两次的话，中途换了进程级配额就会把 lease 还给新的限流器——旧的那份
        并发额度再也回不来，攒够次数整个进程就卡死在等待上。
        """

        return self._limiter if self._limiter is not None else get_default_limiter()

    async def close(self) -> None:
        await self.provider.aclose()

    async def call(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice | None = None,
        stream: bool = False,
        on_delta: OnDelta | None = None,
        attempt: int = 0,
        max_attempts: int = 1,
        logical_call_id: str | None = None,
        **overrides: Any,
    ) -> LLMResponse:
        """发起一次调用；语义与 :meth:`LLMClient.call` 一致。"""

        _validate_trace_identity(
            logical_call_id,
            attempt=attempt,
            max_attempts=max_attempts,
        )
        params = self.params.merged(**overrides)
        limiter = self.limiter  # 取一次用到底：acquire 与 release 必须是同一份账本
        lease = await limiter.aacquire()
        # 计时从获准发出后开始：排队等待不应算进模型响应耗时。
        trace = _CallTrace(
            self.observer,
            provider=self.provider.name,
            params=params,
            stream=stream,
            attempt=attempt,
            max_attempts=max_attempts,
            logical_call_id=logical_call_id,
        )
        trace.start(messages, tools)
        try:
            request = self.provider.build_request(
                messages=messages,
                tools=tools,
                params=params,
                stream=stream,
                tool_choice=tool_choice,
            )
            if not stream:
                raw = await self.provider.acomplete(request)
                result = self.provider.parse_response(raw)
            else:
                accumulator = ResponseAccumulator(
                    provider=self.provider.name, model=params.model
                )
                deadline = _Deadline(params.timeout_s)
                chunks = await self.provider.astream(request)
                try:
                    async for chunk in chunks:
                        deadline.check()
                        delta = accumulator.add(self.provider.parse_delta(chunk))
                        trace.observe_delta(delta)
                        if on_delta is not None and delta is not None:
                            outcome = _run_callback(on_delta, delta)
                            # 回调可写成同步函数或协程，这里统一等待。
                            if hasattr(outcome, "__await__"):
                                await _await_callback(outcome)
                finally:
                    await _aclose_stream(chunks)
                result = accumulator.result()
        except StreamCallbackError as error:
            # 回调是调用方的代码，不是模型调用失败：原样抛出，不入失败观测、
            # 不触发针对模型的重试。
            trace.fail(LLMError(str(error), provider=self.provider.name))
            raise error.cause from None
        except asyncio.CancelledError:
            # 取消不是失败：归一成 LLMError 会让 wait_for 超时、Ctrl+C、
            # gather 取消兄弟任务全部失效——调用方以为停了，实际还在跑、还在烧配额。
            trace.cancel("CancelledError")
            raise
        except Exception as error:
            failure = self._as_llm_error(error, params)
            self._penalize(limiter, failure)
            trace.fail(failure)
            raise failure from error
        except BaseException as interruption:
            trace.cancel(type(interruption).__name__)
            raise
        finally:
            limiter.release(lease)
        limiter.on_success()
        trace.end(result)
        return result

    def cancel_logical_call(
        self,
        logical_call_id: str,
        *,
        attempt: int,
        reason: str = "cancelled during retry backoff",
    ) -> None:
        """闭合已经安排重试、但在下一次物理调用前被取消的 Trace。"""

        _validate_trace_identity(
            logical_call_id,
            attempt=attempt,
            max_attempts=max(1, attempt + 1),
        )
        _cancel_backoff_trace(
            self.observer,
            logical_call_id=logical_call_id,
            provider=self.provider.name,
            model=self.params.model,
            attempt=attempt,
            reason=reason,
        )

    def _penalize(self, limiter: RateLimiter, error: LLMError) -> None:
        if isinstance(error, LLMRateLimitError):
            limiter.penalize(error.retry_after_s)

    def _as_llm_error(self, error: BaseException, params: CallParams) -> LLMError:
        if isinstance(error, LLMError):
            error.provider = error.provider or self.provider.name
            error.model = error.model or params.model
            return error
        return self.provider.map_error(error, model=params.model)


__all__ = ["AsyncLLMClient", "LLMClient", "OnDelta"]

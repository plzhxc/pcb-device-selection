"""Lazy OpenAI-compatible sync and async provider clients.

The adapter contains no PCBAgent configuration or logging imports. Credentials,
provider URL, throttling and defaults arrive through ``AgentSettings``.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import weakref
from typing import Any, Callable, Sequence

from ..contracts import AgentSettings, LoggerProtocol
from ..messages import messages_for_log


class LLMConfigurationError(RuntimeError):
    """Raised on the first provider call when credentials are absent."""


def _api_key(settings: AgentSettings) -> str:
    key = str(settings.api_key or "").strip()
    if not key:
        raise LLMConfigurationError(
            "LLM provider credentials are not configured; set QWEN_API_KEY "
            "or DASHSCOPE_API_KEY before making an LLM call."
        )
    return key


def _tool_payloads(tools: Sequence[Any]) -> list[dict[str, Any]]:
    return [tool.get_openai_format() for tool in tools]


def _request_body(
    *,
    messages: Any,
    tools: Sequence[Any],
    model: str,
    tool_choice: str,
    stream: bool,
    enable_thinking: bool,
    temperature: float | None,
    seed: int | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "extra_body": {"enable_thinking": enable_thinking},
    }
    if temperature is not None:
        body["temperature"] = temperature
    if seed is not None:
        body["seed"] = seed
    if stream:
        body["stream_options"] = {"include_usage": True}
    tool_values = _tool_payloads(tools)
    if tool_values:
        body["tools"] = tool_values
        body["tool_choice"] = tool_choice
    return body


class OpenAICompatibleClient:
    """Synchronous OpenAI-compatible client with lazy SDK construction."""

    def __init__(
        self,
        settings: AgentSettings,
        logger: LoggerProtocol,
        client: Any = None,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.client = client

    def _ensure_client(self) -> Any:
        if self.client is None:
            from openai import OpenAI

            self.client = OpenAI(
                api_key=_api_key(self.settings),
                base_url=self.settings.base_url,
            )
        return self.client

    def call_llm(
        self,
        *,
        messages: Any,
        tools: Sequence[Any],
        request_id: str,
        model: str | None = None,
        tool_choice: str = "auto",
        stream: bool = False,
        enable_thinking: bool | None = None,
        temperature: float | None = None,
        seed: int | None = None,
        on_chunk: Callable[[str, str | None], None] | None = None,
        timeout: float | None = None,
        **_: Any,
    ) -> tuple[str, str, Any]:
        selected_model = model or self.settings.default_model
        thinking = (
            self.settings.default_reasoning
            if enable_thinking is None
            else enable_thinking
        )
        body = _request_body(
            messages=messages,
            tools=tools,
            model=selected_model,
            tool_choice=tool_choice,
            stream=stream,
            enable_thinking=bool(thinking),
            temperature=temperature,
            seed=seed,
        )
        self.logger.debug(
            {
                "event": "request_llm",
                "request_id": request_id,
                "data": {
                    "model": selected_model,
                    "messages": messages_for_log(messages),
                },
            }
        )
        response = self._ensure_client().chat.completions.create(
            **body, **({"timeout": timeout} if timeout is not None else {})
        )
        if not stream:
            choice = response.choices[0]
            message = choice.message
            return (
                getattr(message, "reasoning_content", "") or "",
                getattr(message, "content", "") or "",
                getattr(message, "tool_calls", None),
            )

        reasoning = ""
        answer = ""
        calls: dict[int, dict[str, Any]] = {}
        started = time.perf_counter()
        for chunk in response:
            if timeout is not None and time.perf_counter() - started > timeout:
                raise TimeoutError(
                    f"LLM streaming exceeded wall-clock timeout ({timeout}s)"
                )
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            reason_part = getattr(delta, "reasoning_content", None)
            text_part = getattr(delta, "content", None)
            if reason_part:
                reasoning += reason_part
            if text_part:
                answer += text_part
            if on_chunk and (text_part or reason_part):
                on_chunk(text_part or "", reason_part)
            for item in getattr(delta, "tool_calls", None) or []:
                index = int(getattr(item, "index", 0))
                current = calls.setdefault(
                    index,
                    {
                        "id": getattr(item, "id", None),
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                function = getattr(item, "function", None)
                if function is not None:
                    current["function"]["name"] = (
                        getattr(function, "name", None) or current["function"]["name"]
                    )
                    current["function"]["arguments"] += getattr(
                        function, "arguments", ""
                    ) or ""
        return reasoning, answer, [calls[index] for index in sorted(calls)] or None


class AsyncOpenAICompatibleClient:
    """Asynchronous OpenAI-compatible client with per-loop throttling."""

    def __init__(
        self,
        settings: AgentSettings,
        logger: LoggerProtocol,
        client: Any = None,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.client = client
        self._locks: weakref.WeakKeyDictionary[Any, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )
        self._semaphores: weakref.WeakKeyDictionary[Any, asyncio.Semaphore] = (
            weakref.WeakKeyDictionary()
        )
        self._last_request: weakref.WeakKeyDictionary[Any, float] = (
            weakref.WeakKeyDictionary()
        )

    def _ensure_client(self) -> Any:
        if self.client is None:
            from openai import AsyncOpenAI

            self.client = AsyncOpenAI(
                api_key=_api_key(self.settings),
                base_url=self.settings.base_url,
                timeout=self.settings.llm_request_timeout_s,
                max_retries=3,
            )
        return self.client

    def _loop_controls(self) -> tuple[Any, asyncio.Lock, asyncio.Semaphore]:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[loop] = lock
        semaphore = self._semaphores.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(
                max(1, self.settings.max_concurrent_requests)
            )
            self._semaphores[loop] = semaphore
        return loop, lock, semaphore

    async def _throttle(self, loop: Any, lock: asyncio.Lock) -> None:
        async with lock:
            elapsed = time.monotonic() - self._last_request.get(loop, 0.0)
            delay = max(0.0, self.settings.min_request_interval_s - elapsed)
            if delay:
                await asyncio.sleep(delay)
            self._last_request[loop] = time.monotonic()

    async def close(self) -> None:
        if self.client is not None:
            close = getattr(self.client, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
            self.client = None

    async def call_async_llm(
        self,
        *,
        messages: Any,
        tools: Sequence[Any],
        request_id: str,
        model: str | None = None,
        tool_choice: str = "auto",
        stream: bool = False,
        enable_thinking: bool | None = None,
        temperature: float | None = None,
        seed: int | None = None,
        on_chunk: Callable[[str, str | None], Any] | None = None,
        timeout: float | None = None,
        **_: Any,
    ) -> tuple[str, str, Any]:
        selected_model = model or self.settings.default_model
        thinking = (
            self.settings.default_reasoning
            if enable_thinking is None
            else enable_thinking
        )
        body = _request_body(
            messages=messages,
            tools=tools,
            model=selected_model,
            tool_choice=tool_choice,
            stream=stream,
            enable_thinking=bool(thinking),
            temperature=temperature,
            seed=seed,
        )
        if timeout is not None:
            body["timeout"] = timeout
        loop, lock, semaphore = self._loop_controls()
        async with semaphore:
            await self._throttle(loop, lock)
            response = await self._ensure_client().chat.completions.create(**body)
            if not stream:
                choice = response.choices[0]
                message = choice.message
                return (
                    getattr(message, "reasoning_content", "") or "",
                    getattr(message, "content", "") or "",
                    getattr(message, "tool_calls", None),
                )

            reasoning = ""
            answer = ""
            calls: dict[int, dict[str, Any]] = {}
            async for chunk in response:
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                reason_part = getattr(delta, "reasoning_content", None)
                text_part = getattr(delta, "content", None)
                if reason_part:
                    reasoning += reason_part
                if text_part:
                    answer += text_part
                if on_chunk and (text_part or reason_part):
                    callback_result = on_chunk(text_part or "", reason_part)
                    if inspect.isawaitable(callback_result):
                        await callback_result
                for item in getattr(delta, "tool_calls", None) or []:
                    index = int(getattr(item, "index", 0))
                    current = calls.setdefault(
                        index,
                        {
                            "id": getattr(item, "id", None),
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    function = getattr(item, "function", None)
                    if function is not None:
                        current["function"]["name"] = (
                            getattr(function, "name", None) or current["function"]["name"]
                        )
                        current["function"]["arguments"] += getattr(
                            function, "arguments", ""
                        ) or ""
            return reasoning, answer, [calls[index] for index in sorted(calls)] or None


__all__ = [
    "AsyncOpenAICompatibleClient",
    "LLMConfigurationError",
    "OpenAICompatibleClient",
]

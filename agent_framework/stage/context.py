"""Stage 的运行上下文。

每次运行新建，承载三样东西：**数据**（``input`` / ``upstream`` 取值并登记实际消费，
``emit`` 登记产出）、**并发上限**（命名信号量）、**步骤留痕**（:meth:`StageContext.step`：
一个节点一个步骤，前驱由 DAG 声明直接给出，观测因此拿得到"哪个 agent、带什么工具、
什么提示词、产出了什么、依赖谁"）。控制流不在这里——它在 :mod:`agent_framework.dag`。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Sequence

from ..agent.base import BaseAgent
from ..trace.write.pipeline import emit as emit_event
from ..llm.response import Usage, accumulate_usage as _add_usage, usage_fields
from .observer import (
    NULL_STAGE_OBSERVER,
    StageObserver,
    StepEnded,
    StepStarted,
)




def agent_profile(agent: Any) -> dict[str, Any]:
    """给 Agent 拍一张配置快照。

    记录的是"这次跑的时候它是什么样"——类名、任务类型、挂了哪些工具、提示词
    模板的指纹。提示词改过之后，历史记录仍能对上当时的版本；只存指纹与长度而
    非全文，是因为观测库不该被几十 KB 的提示词撑爆。
    """

    if agent is None:
        return {}
    try:
        return _agent_profile(agent)
    except Exception as exc:
        # 快照只是观测数据。一个自定义 Agent 的 tools/outputs 属性写得刁钻一点
        # （property 抛错、specs() 依赖未初始化的状态），就能把整个业务步骤连坐
        # 掉——观测绝不能有这种权力。
        return {"class": type(agent).__name__, "profile_error": f"{type(exc).__name__}: {exc}"}


def _agent_profile(agent: Any) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "class": type(agent).__name__,
        "task_type": getattr(agent, "task_type", "") or "",
    }
    tools = getattr(agent, "tools", None)
    if tools is not None and not getattr(tools, "empty", True):
        profile["tools"] = [spec.name for spec in tools.specs()]
    outputs = getattr(agent, "outputs", ()) or ()
    if outputs:
        profile["outputs"] = [o.name for o in outputs]
    validators = getattr(agent, "validators", ()) or ()
    if validators:
        profile["validators"] = [
            getattr(v, "name", "") or type(v).__name__ for v in validators
        ]
    prompt = _system_prompt_of(agent)
    if prompt:
        import hashlib

        profile["system_prompt"] = {
            "chars": len(prompt),
            "sha1": hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12],
            "head": prompt[:160],
        }
    return profile


def _system_prompt_of(agent: Any) -> str:
    """按约定找出 Agent 的系统提示模板。

    应用里的提示词一律是类常量（SYSTEM / SYSTEM_PROMPT / *_TEMPLATE），这里按
    这个惯例取；取不到就算了——观测缺一个字段，好过为了取它去改所有 Agent。
    """

    for name in ("SYSTEM", "SYSTEM_PROMPT", "SYSTEM_PROMPT_TEMPLATE", "BASE_SYSTEM_PROMPT"):
        value = getattr(type(agent), name, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _step_value(result: Any) -> Any:
    """从一次运行结果里取出要登记的产出。

    只取数据，不回退成结果对象本身：往 ``outputs`` 里塞一个 AgentResult，下游
    ``ctx.output()`` 拿到的类型就随"这次成没成功"而变，用的人还得自己拆。
    """

    values = getattr(result, "values", None)
    if values:
        return values
    merged = getattr(result, "merged", None)
    if merged:
        return merged
    return getattr(result, "text", "")


def _run_ids_of(result: Any) -> list[str]:
    """取出这次结果涉及的全部 agent 运行 id，含分层 Agent 的子运行。"""

    ids = [rid for rid in (getattr(result, "run_id", ""),) if rid]
    for sub in getattr(result, "subs", ()) or ():
        sub_id = getattr(getattr(sub, "result", None), "run_id", "")
        if sub_id:
            ids.append(sub_id)
    return ids


@dataclass
class StepHandle:
    """一个步骤的句柄，用于登记它的产出。"""

    name: str
    step_id: str
    #: Dependencies declared by the Stage. They are step names or ids and are
    #: persisted into the returned DAG; Trace never reconstructs them from seq.
    depends_on: tuple[str, ...] = ()
    kind: str = "agent"
    agent_name: str = ""
    agent_task_type: str = ""
    started: float = field(default_factory=time.monotonic)
    outputs: dict[str, Any] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    agent_runs: list[str] = field(default_factory=list)
    note: str = ""
    skipped: bool = False
    skip_reason: str = ""
    #: 步骤结束时冻结的耗时。惰性计算会让每个步骤都读到"从开始到现在"，
    #: 于是所有步骤显示同一个数字——并行时尤其看不出谁慢。
    duration_ms: int | None = None
    #: 登记过的结果里有没有失败的。在 ``record()`` 当场记下，而不是事后从
    #: ``outputs`` 反推——产出被抽成 dict/str 之后就再也看不出成败了。
    failed_results: int = 0

    def record(self, value: Any, *, key: str = "result") -> Any:
        """登记步骤产出。

        传入 :class:`AgentResult` 时自动抽取值、用量与运行 id——后者让 stage 与
        agent 两层观测能串起来。
        """

        usage = getattr(value, "usage", None)
        if isinstance(usage, Usage):
            # 按"有没有 usage"识别而不是 isinstance(AgentResult)：HierarchicalResult
            # 不是 AgentResult 的子类，写死类型会让分层 Agent 的 token 在 stage 与
            # workflow 两层凭空消失——总量对不上，却没有任何报错。
            self.outputs[key] = _step_value(value)
            self.usage = _add_usage(self.usage, usage)
            # 自动串联 stage→agent 观测链。此前这里是个 pass 死分支，导致
            # "本 stage 有几个 agent 运行"永远是 0——是错的指标，不是缺失的指标。
            for run_id in _run_ids_of(value):
                self.track(run_id)
            if getattr(value, "ok", True) is False:
                self.failed_results += 1
            return value
        self.outputs[key] = value
        return value

    def status(self, error: BaseException | None = None) -> str:
        """步骤终态。

        异常只是失败的一种。``agent.run()`` 返回 ``output_invalid`` 时不抛异常，
        步骤照常"正常结束"——观测里看不出任何异样，是最容易漏掉的那类失败。
        """

        if isinstance(error, asyncio.CancelledError):
            return "cancelled"
        if error is not None:
            return "failed"
        if self.failed_results:
            return "failed"
        if self.skipped:
            return "skipped"
        return "ok"

    def track(self, run_id: str) -> None:
        """登记本步骤触发的 agent 运行 id。"""

        if run_id:
            self.agent_runs.append(run_id)

    def skip(self, reason: str) -> None:
        """标记本步骤被短路跳过（命中模板、确定性策略等）。

        短路是优化手段，但也可能是缺陷来源（模板过期、策略误命中），所以要留痕
        而不是静默略过。
        """

        self.skipped = True
        self.skip_reason = reason

    @property
    def elapsed_ms(self) -> int:
        """已结束则返回冻结值，否则返回至今耗时。"""

        if self.duration_ms is not None:
            return self.duration_ms
        return int((time.monotonic() - self.started) * 1000)

    def freeze(self) -> int:
        """在步骤结束时定格耗时。"""

        if self.duration_ms is None:
            self.duration_ms = int((time.monotonic() - self.started) * 1000)
        return self.duration_ms


class StageContext:
    """一次 stage 运行的上下文。

    每次运行新建，因此 Stage 实例本身无状态、可并发复用——与 Agent/Session 的
    分工一致。
    """

    def __init__(
        self,
        *,
        stage_id: str,
        stage_name: str,
        inputs: Mapping[str, Any] | None = None,
        upstream: Mapping[str, Any] | None = None,
        observer: StageObserver | None = None,
        semaphores: Mapping[str, int] | None = None,
        runtime: Any = None,
    ) -> None:
        self.stage_id = stage_id
        self.stage_name = stage_name
        #: 实例化 Agent 类所需的运行时（见 agent_framework.dag.runtime），由 Workflow 传入
        self.runtime = runtime
        self._inputs = dict(inputs or {})
        self._upstream = dict(upstream or {})
        self._consumed_inputs: dict[str, Any] = {}
        self._consumed_upstream: dict[str, Any] = {}
        self._observer = observer or NULL_STAGE_OBSERVER
        self._semaphore_limits = dict(semaphores or {})
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self.steps: list[StepHandle] = []
        self.usage = Usage()
        self.outputs: dict[str, Any] = {}

    # ------------------------------ 数据 ------------------------------ #
    def input(self, key: str, default: Any = None) -> Any:
        """取并登记本 Stage 实际消费的 Workflow 直接入参。"""

        if key not in self._inputs:
            return default
        value = self._inputs[key]
        self._consumed_inputs[key] = value
        return value

    def upstream(self, stage_name: str, default: Any = None) -> Any:
        """取上游某个 stage 的产出。

        显式按 stage 名取，而不是把所有上游平铺成一个大字典：依赖关系写在代码里
        看得见，改动上游时也容易查谁在用。成功读取的交接会自动进入 Stage Trace，
        因而观测记录始终跟随实际业务访问，不由 Workflow 在外部猜测。
        """

        if stage_name not in self._upstream:
            return default
        value = self._upstream[stage_name]
        self._consumed_upstream[stage_name] = value
        return value

    @property
    def consumed_inputs(self) -> dict[str, Any]:
        """本 Stage 通过 :meth:`input` 实际读取的直接入参快照。"""

        return dict(self._consumed_inputs)

    @property
    def consumed_upstream(self) -> dict[str, Any]:
        """本 Stage 通过 :meth:`upstream` 实际读取的上游交接快照。"""

        return dict(self._consumed_upstream)

    def output(self, step_name: str, key: str = "result", default: Any = None) -> Any:
        """取本 stage 内某个已完成步骤的产出。"""

        for handle in self.steps:
            if handle.name == step_name:
                return handle.outputs.get(key, default)
        return default

    def emit(self, key: str, value: Any) -> Any:
        """登记 Stage 真实产出，构成下游契约并在结束时写入 Trace。"""

        self.outputs[key] = value
        return value

    # ------------------------------ 并发 ------------------------------ #
    def semaphore(self, name: str, default_limit: int = 8) -> asyncio.Semaphore:
        """取一个命名信号量；同名共享。

        并发上限按用途命名（``device_evaluation`` / ``netlist_generation``），
        而不是全局一个——不同环节的合理并发差一个数量级，混用要么压不住要么跑不满。
        """

        existing = self._semaphores.get(name)
        if existing is not None:
            return existing
        limit = max(1, int(self._semaphore_limits.get(name, default_limit)))
        semaphore = asyncio.Semaphore(limit)
        self._semaphores[name] = semaphore
        return semaphore

    # ------------------------------ 步骤 ------------------------------ #
    @asynccontextmanager
    async def step(
        self,
        name: str,
        *,
        agent: BaseAgent | Any = None,
        kind: str = "agent",
        depends_on: Sequence[str] = (),
    ) -> AsyncIterator[StepHandle]:
        """标记一个步骤，产出可用 :meth:`StepHandle.record` 登记。

        ``agent`` 传了就顺带拍一张配置快照进观测。纯 Python 步骤把 ``kind``
        设成 ``python`` ——它们同样值得记录，因为一个 stage 里往往有一半时间
        花在这些确定性处理上，不记就成了时间黑洞::

            async with ctx.step("decompose", agent=self.agent) as step:
                step.record(await self.agent.arun(chapter=text))
        """

        profile = agent_profile(agent)
        declared_dependencies = tuple(str(item) for item in depends_on if str(item))
        handle = StepHandle(
            name=name,
            step_id=uuid.uuid4().hex,
            depends_on=declared_dependencies,
            kind=kind,
            agent_name=str(profile.get("class") or ""),
            agent_task_type=str(profile.get("task_type") or ""),
        )
        self.steps.append(handle)
        index = len(self.steps)
        self._notify(
            "on_step_started",
            StepStarted(
                stage_id=self.stage_id,
                step_id=handle.step_id,
                name=name,
                index=index,
                kind=kind,
                agent=profile,
            ),
        )
        error: BaseException | None = None
        try:
            yield handle
        except BaseException as exc:  # 记录后原样抛出，编排层决定怎么处置
            error = exc
            raise
        finally:
            duration = handle.freeze()
            self.usage = _add_usage(self.usage, handle.usage)
            self._notify(
                "on_step_ended",
                StepEnded(
                    stage_id=self.stage_id,
                    step_id=handle.step_id,
                    name=name,
                    index=index,
                    kind=kind,
                    duration_ms=duration,
                    status=handle.status(error),
                    skipped=handle.skipped,
                    skip_reason=handle.skip_reason,
                    output_keys=tuple(handle.outputs),
                    agent_runs=tuple(handle.agent_runs),
                    **usage_fields(handle.usage),
                    error=f"{type(error).__name__}: {error}" if error else None,
                ),
            )

    def _notify(self, hook: str, event: Any) -> None:
        emit_event(self._observer, hook, event)


__all__ = ["StageContext", "StepHandle", "agent_profile"]

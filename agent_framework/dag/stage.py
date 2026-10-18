"""把 DAG 引擎接到 Stage 上：节点如何在 StageContext 里跑、如何留痕。

:class:`DAGStage` 是声明式 Stage——子类给一张 :class:`DAG`，不写 ``arun``。
每个节点对应一个 ``ctx.step``，声明的前驱直接作为 ``depends_on`` 传入，因此
trace 里的实际 DAG 与声明的 DAG 完全一致。子图节点递归用同一套执行器，步骤名
以 ``父节点.`` 为前缀，子图入口节点继承父节点的前驱。
"""

from __future__ import annotations

import inspect
from contextlib import nullcontext
from typing import Any, Mapping

from ..agent.base import BaseAgent
from ..stage.base import Stage
from ..stage.context import StageContext
from .engine import (
    NodeAttemptFailed,
    NodeFailed,
    NodeOutcome,
    NodeUnavailable,
    call_fallback,
    execute_dag,
    resolve_ref,
)
from .runtime import NodeRuntime
from .spec import DAG, DAGError, Node, Ref, dig

_MISSING = object()


def failure_summary(result: Any) -> str:
    """把 Agent 结果里最有用的失败诊断压成一句话。"""

    summary = getattr(result, "failure_summary", None)
    if callable(summary):
        text = summary()
        if text:
            return str(text)
    status = getattr(getattr(result, "status", None), "value", None) or str(
        getattr(result, "status", "error")
    )
    error = getattr(result, "error", None)
    return f"{status}: {error}" if error else str(status)


def agent_output(result: Any) -> Any:
    """把一次 Agent 运行结果变成节点产出。

    取 ``values``（结构化输出）、其次 ``merged``（分层 Agent）、最后 ``text``；
    结构化输出只声明了一个名为 ``result`` 的块时直接给块内容——下游按业务键取值，
    不该知道"块"这一层包装。
    """

    if isinstance(result, Mapping):
        value: Any = result
    else:
        value = getattr(result, "values", None) or getattr(result, "merged", None)
        if not value:
            text = getattr(result, "text", "")
            return {"text": text} if text else {}
    if isinstance(value, Mapping) and set(value) == {"result"} and isinstance(value["result"], Mapping):
        return dict(value["result"])
    return dict(value) if isinstance(value, Mapping) else value


class StageNodeExecutor:
    """在一个 StageContext 里执行 DAG 节点（含子图递归）。"""

    def __init__(
        self,
        ctx: StageContext,
        runtime: NodeRuntime | None,
        *,
        scope: Mapping[str, Any] | None = None,
        prefix: str = "",
        entry_depends: tuple[str, ...] = (),
    ) -> None:
        self.ctx = ctx
        self.runtime = runtime
        self.scope = scope          # 子图的 Input() 命名空间；顶层为 None → 读 ctx.input
        self.prefix = prefix
        self.entry_depends = entry_depends

    # ------------------------------ 引用解析 ------------------------------ #
    def resolve_external(self, ref: Ref) -> Any:
        if ref.kind == "input":
            if self.scope is not None:
                base = self.scope.get(ref.source, _MISSING)
            else:
                base = self.ctx.input(ref.source, _MISSING)
        elif ref.kind == "upstream":
            base = self.ctx.upstream(ref.source, _MISSING)
        else:
            raise DAGError(f"executor cannot resolve ref kind {ref.kind!r}")
        if base is _MISSING:
            return ref.default
        return dig(base, ref.path, ref.default) if ref.path else base

    def _step(self, step_name: str) -> str:
        return self.prefix + step_name

    def _deps(self, depends_on: tuple[str, ...]) -> tuple[str, ...]:
        if depends_on:
            return tuple(self.prefix + d for d in depends_on)
        return self.entry_depends

    # ------------------------------ 节点执行 ------------------------------ #
    async def run_node(
        self,
        node: Node,
        kwargs: Mapping[str, Any],
        *,
        attempt: int,
        depends_on: tuple[str, ...],
        step_name: str,
    ) -> Any:
        target = node.run
        name, deps = self._step(step_name), self._deps(depends_on)
        if isinstance(target, DAG):
            return await self._run_subdag(target, kwargs, name, deps)
        if isinstance(target, type) and issubclass(target, BaseAgent):
            if self.runtime is None:
                raise DAGError(
                    f"node {name!r} needs a runtime to instantiate {target.__name__}; "
                    "pass runtime= to Workflow / Stage.execute"
                )
            agent = self.runtime.agent(target)
            if agent is None:
                raise NodeUnavailable(f"{target.__name__} is disabled in this runtime")
            return await self._run_agent(node, agent, kwargs, name, deps)
        if hasattr(target, "arun") and not isinstance(target, type):
            return await self._run_agent(node, target, kwargs, name, deps)
        if callable(target):
            try:
                async with self.ctx.step(name, kind=node.kind or "python", depends_on=deps) as step:
                    output = target(**kwargs)
                    if inspect.isawaitable(output):
                        output = await output
                    step.record(output)
            except Exception as exc:
                # 业务异常是一次失败尝试，交给引擎按 retries / fallback / optional 处置；
                # 步骤本身已在 ctx.step 里记为 failed
                raise NodeAttemptFailed(f"{type(exc).__name__}: {exc}", exc) from exc
            return output
        raise DAGError(f"node {name!r}: cannot run {target!r}")

    async def _run_subdag(
        self,
        dag: DAG,
        kwargs: Mapping[str, Any],
        name: str,
        deps: tuple[str, ...],
    ) -> Any:
        child = StageNodeExecutor(
            self.ctx, self.runtime, scope=dict(kwargs), prefix=name + ".", entry_depends=deps
        )
        try:
            outcomes = await execute_dag(dag, child)
        except NodeFailed as exc:
            raise NodeAttemptFailed(exc.reason, exc.cause) from exc
        if dag.outputs is None:
            return {n: o.output for n, o in outcomes.items() if o.done}
        outputs = {key: resolve_ref(ref, outcomes, child) for key, ref in dag.outputs.items()}
        # 与 Agent 的单 ``result`` 块同一约定：只有一个 result 输出时直接给内容
        if set(outputs) == {"result"}:
            return outputs["result"]
        return outputs

    async def _run_agent(
        self,
        node: Node,
        agent: Any,
        kwargs: Mapping[str, Any],
        name: str,
        deps: tuple[str, ...],
    ) -> Any:
        kind = node.kind or str(getattr(agent, "step_kind", "agent") or "agent")
        gate = self.ctx.semaphore(node.semaphore) if node.semaphore else nullcontext()
        try:
            async with gate:
                async with self.ctx.step(name, agent=agent, kind=kind, depends_on=deps) as step:
                    result = step.record(await agent.arun(**kwargs))
        except Exception as exc:
            raise NodeAttemptFailed(f"{type(exc).__name__}: {exc}", exc) from exc
        if getattr(result, "ok", True) is False:
            raise NodeAttemptFailed(failure_summary(result))
        return agent_output(result)

    async def run_fallback(
        self,
        node: Node,
        kwargs: Mapping[str, Any],
        reason: str,
        *,
        depends_on: tuple[str, ...],
        step_name: str,
    ) -> Any:
        async with self.ctx.step(
            self._step(step_name) + ".fallback", kind="python", depends_on=self._deps(depends_on)
        ) as step:
            output = await call_fallback(node, reason, kwargs)
            step.record(output)
            step.note = reason
        return output

    async def skip_node(
        self,
        node: Node,
        reason: str,
        *,
        depends_on: tuple[str, ...],
        step_name: str,
    ) -> None:
        async with self.ctx.step(
            self._step(step_name), kind=node.kind or "agent", depends_on=self._deps(depends_on)
        ) as step:
            step.skip(reason)


class DAGStage(Stage):
    """用一张 DAG 声明的 Stage：子类给 ``dag``（类属性或 ``__init__`` 里按配置生成）。"""

    dag: DAG | None = None

    async def arun(self, ctx: StageContext) -> None:
        if self.dag is None:
            raise DAGError(f"stage {self.name!r} declares no DAG")
        executor = StageNodeExecutor(ctx, getattr(ctx, "runtime", None))
        outcomes = await execute_dag(self.dag, executor)
        if self.dag.outputs is not None:
            for key, ref in self.dag.outputs.items():
                ctx.emit(key, resolve_ref(ref, outcomes, executor))
            return
        for name, outcome in outcomes.items():
            if outcome.done:
                ctx.emit(name, outcome.output)


__all__ = ["DAGStage", "StageNodeExecutor", "agent_output", "failure_summary"]

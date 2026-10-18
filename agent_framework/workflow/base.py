"""Workflow：Stage 的 DAG。

它和 Stage 用同一个执行引擎（:mod:`agent_framework.dag.engine`）：节点是 Stage，
边是顺序。不给 ``edges`` 时退化为线性链——这是目前唯一的用法，但并行 stage 与
条件 stage 不需要再改引擎。

数据流转按 stage 名寻址（``ctx.upstream("device_selection")``），不把所有上游产出
平铺成一个大字典：依赖关系写在代码里看得见，改上游时也查得出影响面。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Iterable, Mapping, Sequence

from ..dag.engine import NodeAttemptFailed, NodeFailed, call_fallback, execute_dag
from ..dag.spec import DAG, Edge, Node, Ref
from ..llm.response import Usage, accumulate_usage as _add_usage, usage_fields
from ..stage.base import Stage
from ..stage.result import StageResult, StageStatus
from ..trace.write.pipeline import digest as _digest
from ..trace.write.pipeline import emit as emit_event
from ..trace.write.runtime import workflow_observer as _default_workflow_observer
from .observer import (
    NULL_WORKFLOW_OBSERVER,
    WorkflowEnded,
    WorkflowObserver,
    WorkflowStarted,
    _current_workflow,
)
from .result import WorkflowResult, WorkflowStatus


class Workflow:
    """Stage 的有向无环图。

        workflow = Workflow("pcb", [Requirement(), Selection(), Netlist(), TopPort()],
                            runtime=AgentRuntime(client, config))
        result = await workflow.arun(user_input="...")
        result.stage("netlist").outputs
    """

    def __init__(
        self,
        name: str,
        stages: Sequence[Stage],
        *,
        edges: Iterable[Edge | tuple[str, str]] | None = None,
        observer: WorkflowObserver | None = None,
        stop_on_error: bool = True,
        runtime: Any = None,
    ) -> None:
        """
        Args:
            edges: stage 间的边；``None`` 表示按给定顺序串成链。
            stop_on_error: stage 失败是否中断。默认中断——流水线是强依赖链，前一步
                没产出，后面跑出来的只会是垃圾结果。
            runtime: 传给每个 Stage 的 Agent 运行时（DAG 声明的 Stage 靠它实例化 Agent）。
        """

        if not stages:
            raise ValueError("workflow 至少需要一个 stage")
        names = [s.name for s in stages]
        duplicated = {n for n in names if names.count(n) > 1}
        if duplicated:
            raise ValueError(f"stage 名重复：{sorted(duplicated)}")
        self.name = name
        self.stages = list(stages)
        self.observer = observer or _default_workflow_observer() or NULL_WORKFLOW_OBSERVER
        self.stop_on_error = stop_on_error
        self.runtime = runtime
        if edges is None:
            edges = [(a.name, b.name) for a, b in zip(stages, stages[1:])]
        self.dag = DAG(
            [Node(s.name, run=s, optional=not stop_on_error) for s in stages],
            edges,
            name=name,
        )

    def stage(self, name: str) -> Stage:
        for item in self.stages:
            if item.name == name:
                return item
        raise ValueError(f"未知的 stage {name!r}；可选：{[s.name for s in self.stages]}")

    async def arun(
        self,
        *,
        start_from: str | None = None,
        stop_after: str | None = None,
        upstream: Mapping[str, Any] | None = None,
        **inputs: Any,
    ) -> WorkflowResult:
        """执行（子）图。

        ``upstream`` 用于断点续跑：从中间某个 stage 起跑时，把之前 stage 的产出直接
        喂进来，不必重跑。
        """

        try:
            selected = self.dag.subgraph(start_from=start_from, stop_after=stop_after)
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        workflow_id = uuid.uuid4().hex
        started = time.monotonic()
        token = _current_workflow.set(workflow_id)
        self._notify(
            "on_workflow_started",
            WorkflowStarted(
                workflow_id=workflow_id,
                name=self.name,
                stages=selected.order(),
                inputs={k: _digest(v) for k, v in inputs.items()},
            ),
        )

        executor = _StageExecutor(self, workflow_id, inputs, dict(upstream or {}))
        failed_stage: str | None = None
        failed_stages: list[str] = []
        error: BaseException | None = None
        try:
            outcomes = await execute_dag(selected, executor)
        except NodeFailed as exc:
            failed_stage = exc.node
            failed_stages = [exc.node]
            error = exc.cause if exc.cause is not None else exc
            outcomes = {}
        except asyncio.CancelledError:
            duration = int((time.monotonic() - started) * 1000)
            self._notify(
                "on_workflow_ended",
                WorkflowEnded(
                    workflow_id=workflow_id,
                    name=self.name,
                    status="cancelled",
                    duration_ms=duration,
                    stages_total=len(selected.nodes),
                    stages_done=sum(1 for r in executor.results if r.ok),
                    failed_stage=executor.active,
                    failed_stages=(),
                    **usage_fields(executor.usage),
                    error="CancelledError",
                ),
            )
            raise
        finally:
            _current_workflow.reset(token)

        results = executor.results
        if failed_stage is None:
            # stop_on_error=False 时失败 stage 以 failed 终态留在图里，按拓扑序取第一个
            failed_stages = [n for n, o in outcomes.items() if o.state == "failed"]
            if failed_stages:
                failed_stage = failed_stages[0]
                error = next((r.error for r in results if r.name == failed_stage), None)

        degraded = [r.name for r in results if r.status is StageStatus.PARTIAL]
        partial = len(results) < len(self.stages)
        if failed_stage is not None:
            status = WorkflowStatus.FAILED
        elif partial or degraded:
            status = WorkflowStatus.PARTIAL
        else:
            status = WorkflowStatus.SUCCESS
        if failed_stage is None and degraded:
            failed_stage = degraded[0]
            failed_stages = degraded

        duration = int((time.monotonic() - started) * 1000)
        self._notify(
            "on_workflow_ended",
            WorkflowEnded(
                workflow_id=workflow_id,
                name=self.name,
                status=status.value,
                duration_ms=duration,
                stages_total=len(selected.nodes),
                stages_done=sum(1 for r in results if r.ok),
                failed_stage=failed_stage,
                failed_stages=tuple(failed_stages),
                **usage_fields(executor.usage),
                error=f"{type(error).__name__}: {error}" if error else None,
            ),
        )
        return WorkflowResult(
            workflow_id=workflow_id,
            name=self.name,
            status=status,
            stages=tuple(results),
            outputs=executor.produced,
            usage=executor.usage,
            duration_ms=duration,
            failed_stage=failed_stage,
            error=error,
        )

    def _notify(self, hook: str, event: Any) -> None:
        emit_event(self.observer, hook, event)


class _StageExecutor:
    """Workflow 层的节点执行器：一个节点 = 跑一个 Stage。"""

    def __init__(
        self,
        workflow: Workflow,
        workflow_id: str,
        inputs: Mapping[str, Any],
        produced: dict[str, Any],
    ) -> None:
        self.workflow = workflow
        self.workflow_id = workflow_id
        self.inputs = inputs
        self.produced = produced
        self.results: list[StageResult] = []
        self.usage = Usage()
        self.active: str | None = None

    def resolve_external(self, ref: Ref) -> Any:
        # Stage 通过 ctx.input()/ctx.upstream() 自取，不经引擎解析
        return ref.default

    async def run_node(
        self,
        node: Node,
        kwargs: Mapping[str, Any],
        *,
        attempt: int,
        depends_on: tuple[str, ...],
        step_name: str,
    ) -> Any:
        stage: Stage = node.run
        self.active = stage.name
        try:
            result = await stage.execute(
                inputs=self.inputs,
                upstream=dict(self.produced),
                workflow_id=self.workflow_id,
                index=len(self.results) + 1,
                runtime=self.workflow.runtime,
            )
        finally:
            self.active = None
        self.results.append(result)
        self.usage = _add_usage(self.usage, result.usage)
        if not result.ok:
            raise NodeAttemptFailed(
                f"{type(result.error).__name__}: {result.error}" if result.error else result.status.value,
                cause=result.error,
            )
        self.produced[stage.name] = result.outputs
        return result.outputs

    async def run_fallback(
        self,
        node: Node,
        kwargs: Mapping[str, Any],
        reason: str,
        *,
        depends_on: tuple[str, ...],
        step_name: str,
    ) -> Any:
        return await call_fallback(node, reason, kwargs)

    async def skip_node(
        self, node: Node, reason: str, *, depends_on: tuple[str, ...], step_name: str
    ) -> None:
        return None


__all__ = ["Workflow"]

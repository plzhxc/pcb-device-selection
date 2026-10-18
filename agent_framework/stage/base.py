"""Stage：一组 Agent 的编排单元。

一个 stage 对应流水线里的一个环节。它的职责是**声明这些 Agent 之间的图**并把过程
记录下来：具体声明方式见 :class:`agent_framework.dag.DAGStage`，本类只负责执行的
生命周期——建 :class:`StageContext`、收敛异常为 :class:`StageResult`、发观测事件、
返回本次运行的实际 DAG（由步骤的 ``depends_on`` 直接构成，不根据时间顺序猜测）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Mapping

from ..llm.response import usage_fields
from ..trace.write.pipeline import digest as _snapshot
from ..trace.write.pipeline import emit as emit_event
from ..trace.write.runtime import stage_observer as _default_stage_observer
from .context import StageContext
from .dag import StageDAG
from .observer import (
    NULL_STAGE_OBSERVER,
    StageEnded,
    StageObserver,
    StageStarted,
    _current_stage,
)
from .result import StageResult, StageStatus


class Stage(ABC):
    """流水线中的一个环节。

    实例只持有不变的东西（图、配置），运行态在 :class:`StageContext` 里，因此可被
    并发复用。子类通过 :class:`agent_framework.dag.DAGStage` 声明图::

        class ModuleDecompositionStage(DAGStage):
            name = "module_decomposition"
            dag = DAG(
                nodes=[Node("decompose", Decomposer,
                            inputs={"chapter": Upstream("requirement", "result", "chapter5")})],
                outputs={"result": From("decompose")},
            )
    """

    #: stage 名，作为下游 ``ctx.upstream(name)`` 的键
    name: str = ""
    #: 命名信号量的默认上限，如 ``{"device_evaluation": 20}``
    semaphores: Mapping[str, int] = {}
    #: Added verbatim to the Stage-produced DAG. Applications may identify
    #: their graph source without overriding DAG construction.
    dag_metadata: Mapping[str, Any] = {}

    def __init__(self, *, observer: StageObserver | None = None) -> None:
        self.observer = observer or _default_stage_observer() or NULL_STAGE_OBSERVER

    @abstractmethod
    async def arun(self, ctx: StageContext) -> Any:
        """执行本 stage 的编排。

        产出用 ``ctx.emit(key, value)`` 登记；返回值会被忽略，以免同一份数据
        出现"返回值"和"emit"两个来源而对不上。
        """

        raise NotImplementedError

    def build_dag(self, ctx: StageContext) -> StageDAG:
        """Return this run's concrete execution DAG.

        StageContext has already captured sequential, parallel and background
        branch dependencies. This method only closes and validates that graph.
        """

        return StageDAG.from_steps(
            self.name,
            ctx.steps,
            metadata=self.dag_metadata,
        )

    def _safe_dag(self, ctx: StageContext) -> StageDAG:
        """Keep a malformed observability graph from breaking the business."""

        try:
            return self.build_dag(ctx)
        except Exception as exc:
            return StageDAG.from_steps(
                self.name,
                ctx.steps,
                metadata={
                    "actual": False,
                    "dag_error": f"{type(exc).__name__}: {exc}",
                    "dependency_source": "fallback_without_inferred_edges",
                },
                include_step_dependencies=False,
            )

    async def execute(
        self,
        *,
        inputs: Mapping[str, Any] | None = None,
        upstream: Mapping[str, Any] | None = None,
        workflow_id: str = "",
        index: int = 1,
        runtime: Any = None,
    ) -> StageResult:
        """跑完一个 stage；异常不外抛，统一收敛为 :class:`StageResult`。

        收敛而非抛出，是为了让 workflow 能决定"这一步失败要不要中断整条流水线"
        ——有些 stage 失败可以降级继续，有些必须停。
        """

        stage_id = uuid.uuid4().hex
        ctx = StageContext(
            stage_id=stage_id,
            stage_name=self.name,
            inputs=inputs,
            upstream=upstream,
            observer=self.observer,
            semaphores=self.semaphores,
            runtime=runtime,
        )
        trace_inputs = _stage_trace_inputs(inputs, upstream)
        started = time.monotonic()
        # 先 set 再发事件：观察者在 started 回调里就要读 current_stage_id()
        token = _current_stage.set(stage_id)
        self._notify(
            "on_stage_started",
            StageStarted(
                stage_id=stage_id,
                name=self.name,
                workflow_id=workflow_id,
                index=index,
                inputs=trace_inputs,
            ),
        )
        status = StageStatus.SUCCESS
        error: BaseException | None = None
        cancelled = False
        try:
            await self.arun(ctx)
        except asyncio.CancelledError:
            # 取消必须穿透到 Workflow：吞掉它，被取消的流水线会把剩下的 stage
            # 全部跑完，调用方以为停了、实际还在烧 LLM 配额。
            cancelled = True
            status = StageStatus.CANCELLED
            raise
        except Exception as exc:
            status = StageStatus.ERROR
            error = exc
        finally:
            _current_stage.reset(token)
            if cancelled:
                # 取消路径上仍要闭合观测，否则会留下有开始无结束的悬挂行。
                cancelled_dag = self._safe_dag(ctx)
                self._notify(
                    "on_stage_ended",
                    StageEnded(
                        stage_id=stage_id,
                        name=self.name,
                        status=StageStatus.CANCELLED.value,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        steps=len(ctx.steps),
                        skipped_steps=sum(1 for s in ctx.steps if s.skipped),
                        **usage_fields(ctx.usage),
                        outputs=_snapshot(dict(ctx.outputs)),
                        dag=cancelled_dag.as_dict(),
                        error="CancelledError",
                    ),
                )

        failed_steps = sum(1 for s in ctx.steps if s.status() == "failed")
        if status is StageStatus.SUCCESS and failed_steps:
            # 步骤失败不抛异常，不在这里降级的话，一条有环节失败的流水线
            # 在观测里跟全绿的一模一样。
            status = StageStatus.PARTIAL

        duration = int((time.monotonic() - started) * 1000)
        dag = self._safe_dag(ctx)
        result = StageResult(
            stage_id=stage_id,
            name=self.name,
            status=status,
            outputs=dict(ctx.outputs),
            steps=tuple(ctx.steps),
            usage=ctx.usage,
            duration_ms=duration,
            error=error,
            inputs=ctx.consumed_inputs,
            upstream=ctx.consumed_upstream,
            dag=dag,
        )
        self._notify(
            "on_stage_ended",
            StageEnded(
                stage_id=stage_id,
                name=self.name,
                status=status.value,
                duration_ms=duration,
                steps=len(ctx.steps),
                skipped_steps=sum(1 for s in ctx.steps if s.skipped),
                failed_steps=failed_steps,
                agent_runs=sum(len(s.agent_runs) for s in ctx.steps),
                **usage_fields(ctx.usage),
                outputs=_snapshot(dict(ctx.outputs)),
                dag=dag.as_dict(),
                output_keys=tuple(ctx.outputs),
                error=f"{type(error).__name__}: {error}" if error else None,
            ),
        )
        return result

    def _notify(self, hook: str, event: Any) -> None:
        emit_event(self.observer, hook, event)


def _stage_trace_inputs(
    inputs: Mapping[str, Any] | None,
    upstream: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """把 Stage 入口的两个运行参数合成 Trace 中唯一的一份输入快照。

    Workflow 根参数按原键放入；已有 Stage 的产出按 Stage 名放入。极少数重名场景
    使用 ``<name>#stage_output`` 保存后者，避免观测数据静默覆盖。
    """

    merged = {str(key): value for key, value in (inputs or {}).items()}
    for raw_name, value in (upstream or {}).items():
        name = str(raw_name)
        resolved = name
        suffix = 1
        while resolved in merged:
            suffix_text = "" if suffix == 1 else f"_{suffix}"
            resolved = f"{name}#stage_output{suffix_text}"
            suffix += 1
        merged[resolved] = value
    snapshot = _snapshot(merged)
    return snapshot if isinstance(snapshot, dict) else {}


__all__ = ["Stage"]

"""HierarchicalAgent：Master 规划 → 并行子任务 → Python 合并。

**动机是可靠性，不是速度。** 让模型一次吐出 5–10K token 的大结果，会显著提高空
响应、截断和格式错误的概率，进而陷入重试地狱。拆成一次小规划 + 多个各几百 token
的子任务，每次输出都短而稳，且失败只影响单个子任务而不是全部。

流程::

    Master LLM  ──→  plan = {sub_tasks: [...]}     ← 走 BaseAgent 的完整循环
         ↓
    并行执行子 Agent（受并发上限保护，单个失败不影响其他）
         ↓
    merge(plan, subs)  ← 纯 Python，不再调模型
         ↓
    HierarchicalResult

合并刻意用纯 Python 而非再来一次 LLM：结果的拼装是确定性工作，交给模型只会
引入新的不确定性和成本。

子类需要实现三件事：:meth:`create_sub_agent`、:meth:`build_sub_kwargs`、
:meth:`merge`；以及像普通 Agent 一样实现 :meth:`~BaseAgent.prepare` 准备 Master
的提示词，并在类属性 ``outputs`` 里声明名为 :attr:`~HierarchicalAgent.plan_key`
的结构化输出。
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..llm.response import Usage
from .base import BaseAgent
from .observer import StepKind, StepRecorded
from .result import AgentResult, AgentStatus, accumulate_usage

_logger = logging.getLogger("agent_framework.hierarchical")


@dataclass(frozen=True)
class SubRun:
    """一个子任务的执行结果。

    子任务失败不会中断兄弟任务，失败信息保留在这里，由 :meth:`merge` 决定
    如何处置部分成功——是补齐、跳过，还是整体判失败。
    """

    task: Any
    result: AgentResult | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.result is not None and self.result.ok


@dataclass(frozen=True)
class HierarchicalResult:
    """分层运行的完整结果。

    Attributes:
        status: 终态。
        plan: Master 产出的计划。
        subs: 每个子任务的结果。
        merged: :meth:`merge` 的产物。
        master: Master 那一次运行的完整结果。
        usage: Master 与全部子任务累计的 token 用量。
        error: 失败原因。
    """

    status: AgentStatus
    plan: Any = None
    subs: Sequence[SubRun] = ()
    merged: Any = None
    master: AgentResult | None = None
    usage: Usage = field(default_factory=Usage)
    error: str | None = None

    @property
    def run_id(self) -> str:
        """本次分层运行的主 id，取 Master 那一次。

        有了它，stage 层的 ``step.record()`` 才能把分层运行串进观测链——否则
        整条分层任务在 stage 视角下是个没有来源的黑盒。
        """

        return self.master.run_id if self.master else ""

    @property
    def ok(self) -> bool:
        return self.status.ok

    def __bool__(self) -> bool:
        return self.ok

    @property
    def failed_subs(self) -> list[SubRun]:
        return [sub for sub in self.subs if not sub.ok]


class HierarchicalAgent(BaseAgent):
    """Master + 并行子任务 + 合并 的模板基类。

    **返回类型与 BaseAgent 不同**：:meth:`run` / :meth:`arun` 返回
    :class:`HierarchicalResult` 而非 :class:`~.result.AgentResult`——一次分层运行
    包含计划、若干子运行和合并产物，塞进单次运行的结果里只会丢信息。两者都提供
    ``ok`` / ``usage`` / ``run_id``，stage 层按这几个属性取值，因此两种结果都能
    直接 ``step.record()``。把分层 Agent 当普通 Agent 用时要注意这一点：
    ``result.values`` 在这里叫 ``result.merged``。
    """

    #: Master 结构化输出中承载计划的键名
    plan_key: str = "plan"
    #: 计划里承载子任务列表的键名
    sub_tasks_key: str = "sub_tasks"
    #: 同时运行的子 Agent 上限。注意这与 LLM 层的请求配额是两回事：
    #: 这里限制的是"同时展开多少个子任务"，请求速率仍由共享的 RateLimiter 兜底。
    sub_concurrency: int = 8

    # ------------------------------ 子类实现 ------------------------------ #
    @abstractmethod
    def create_sub_agent(self) -> BaseAgent:
        """创建一个子 Agent 实例。

        每个子任务都会单独调用一次，便于子 Agent 持有自己的工具箱或配置。
        """

        raise NotImplementedError

    @abstractmethod
    def build_sub_kwargs(self, plan: Any, task: Any) -> dict[str, Any]:
        """把计划里的一个子任务翻译成子 Agent 的 ``build`` 入参。"""

        raise NotImplementedError

    @abstractmethod
    def merge(self, plan: Any, subs: Sequence[SubRun], **kwargs: Any) -> Any:
        """用纯 Python 把计划与子任务结果合并成最终产物。"""

        raise NotImplementedError

    def sub_tasks(self, plan: Any) -> Sequence[Any]:
        """从计划中取出子任务列表；结构特殊时子类可覆盖。"""

        if isinstance(plan, dict):
            return plan.get(self.sub_tasks_key) or []
        return getattr(plan, self.sub_tasks_key, None) or []

    # ------------------------------ 编排 ------------------------------ #
    def _extract_plan(self, master: AgentResult) -> Any:
        return master.values.get(self.plan_key) if master.values else None

    def _finish(
        self,
        plan: Any,
        subs: Sequence[SubRun],
        master: AgentResult,
        **kwargs: Any,
    ) -> HierarchicalResult:
        """执行合并并组装最终结果。"""

        usage = master.usage
        for sub in subs:
            if sub.result is not None:
                usage = accumulate_usage(usage, sub.result.usage)
        failed = [sub for sub in subs if not sub.ok]
        sub_runs = []
        for index, sub in enumerate(subs, start=1):
            result = sub.result
            result_error = str(result.error) if result is not None and result.error else None
            sub_runs.append(
                {
                    "index": index,
                    "task": sub.task,
                    "agent_run_id": result.run_id if result is not None else "",
                    "status": result.status.value if result is not None else "error",
                    "ok": sub.ok,
                    "error": sub.error or result_error,
                }
            )
        self._note(
            master.run_id,
            "subs",
            len(subs),
            {
                "total": len(subs),
                "failed": len(failed),
                "errors": [sub.error for sub in failed if sub.error],
                "sub_runs": sub_runs,
            },
        )
        try:
            merged = self.merge(plan, subs, **kwargs)
        except Exception as exc:
            _logger.warning("分层合并失败", exc_info=True)
            self._note(master.run_id, "merge", 1, {"outcome": "failed", "error": f"{type(exc).__name__}: {exc}"})
            return HierarchicalResult(
                status=AgentStatus.MERGE_FAILED,
                plan=plan,
                subs=subs,
                master=master,
                usage=usage,
                error=f"{type(exc).__name__}: {exc}",
            )
        self._note(
            master.run_id,
            "merge",
            1,
            {"outcome": "success", "failed_subs": len(failed)},
        )
        return HierarchicalResult(
            status=AgentStatus.SUCCESS,
            plan=plan,
            subs=subs,
            merged=merged,
            master=master,
            usage=usage,
        )

    def _note(
        self,
        run_id: str,
        kind: StepKind,
        index: int,
        detail: dict[str, Any],
    ) -> None:
        """把分层编排的关键节点也记进观测。

        计划解析失败、某个子任务炸了、合并失败——这三处是分层任务最常出问题的
        地方，此前一条记录都不留：观测里只看得到 Master 那一次调用成功了，然后
        整件事就没有下文，排查只能靠猜。
        """

        # run_id 显式传入：这些节点都在 Master 那次运行结束之后才执行，
        # 此时 ContextVar 已重置，current_run_id() 只会读到空串。
        self._notify("on_step", StepRecorded(run_id=run_id, kind=kind, index=index, detail=detail))

    def _master_failed(self, master: AgentResult) -> HierarchicalResult:
        self._note(master.run_id, "plan", 1, {"outcome": "master_failed", "status": master.status.value})
        return HierarchicalResult(
            status=master.status,
            master=master,
            usage=master.usage,
            error=str(master.error) if master.error else "Master 运行未成功",
        )

    def _plan_invalid(self, master: AgentResult) -> HierarchicalResult:
        self._note(master.run_id, "plan", 1, {"outcome": "plan_invalid", "plan_key": self.plan_key})
        return HierarchicalResult(
            status=AgentStatus.PLAN_INVALID,
            master=master,
            usage=master.usage,
            error=f"Master 未产出可用的 {self.plan_key!r}",
        )

    # ------------------------------ 异步 ------------------------------ #
    async def arun(self, **kwargs: Any) -> HierarchicalResult:  # type: ignore[override]
        """Master → 并行子任务 → 合并。"""

        master = await super().arun(**kwargs)
        if not master.ok:
            return self._master_failed(master)
        plan = self._extract_plan(master)
        if not plan:
            return self._plan_invalid(master)

        self._note(
            master.run_id,
            "plan",
            1,
            {
                "outcome": "success",
                "plan": plan,
                "sub_tasks": list(self.sub_tasks(plan)),
            },
        )
        subs = await self._spawn_subs(plan)
        return self._finish(plan, subs, master, **kwargs)

    async def _spawn_subs(self, plan: Any) -> list[SubRun]:
        """并发执行全部子任务；单个失败归一为带 ``error`` 的结果项。"""

        tasks = list(self.sub_tasks(plan))
        if not tasks:
            return []

        semaphore = asyncio.Semaphore(max(1, self.sub_concurrency))

        async def run_one(task: Any) -> SubRun:
            # 信号量覆盖"创建 + 执行"整个过程，避免瞬间创建过多子 Agent。
            async with semaphore:
                try:
                    agent = self.create_sub_agent()
                    result = await agent.arun(**self.build_sub_kwargs(plan, task))
                    return SubRun(task=task, result=result)
                except Exception as exc:
                    # 只留 "类型: 消息" 的话，子任务里第三方库深处抛的错根本
                    # 定位不到行号；日志留全栈，返回值保持精简。
                    _logger.warning("分层子任务执行失败：%r", task, exc_info=True)
                    return SubRun(task=task, error=f"{type(exc).__name__}: {exc}")

        return list(await asyncio.gather(*[run_one(task) for task in tasks]))

    # ------------------------------ 同步 ------------------------------ #
    def run(self, **kwargs: Any) -> HierarchicalResult:  # type: ignore[override]
        """同步版本；子任务**顺序执行**。

        并行是本模板的主要价值，生产路径应优先使用 :meth:`arun`。这里保留同步
        入口只为脚本与调试方便。
        """

        master = super().run(**kwargs)
        if not master.ok:
            return self._master_failed(master)
        plan = self._extract_plan(master)
        if not plan:
            return self._plan_invalid(master)

        self._note(
            master.run_id,
            "plan",
            1,
            {
                "outcome": "success",
                "plan": plan,
                "sub_tasks": list(self.sub_tasks(plan)),
            },
        )
        subs: list[SubRun] = []
        for task in self.sub_tasks(plan):
            try:
                agent = self.create_sub_agent()
                subs.append(
                    SubRun(task=task, result=agent.run(**self.build_sub_kwargs(plan, task)))
                )
            except Exception as exc:
                _logger.warning("分层子任务执行失败：%r", task, exc_info=True)
                subs.append(SubRun(task=task, error=f"{type(exc).__name__}: {exc}"))
        return self._finish(plan, subs, master, **kwargs)


__all__ = ["HierarchicalAgent", "HierarchicalResult", "SubRun"]

"""DAG 执行引擎：拓扑调度、并行、条件边、按集合展开、有界重试、确定性兜底。

引擎不知道节点里跑的是 Agent、Stage 还是子图——怎么跑一个节点由 :class:`NodeExecutor`
决定。同一个引擎因此既跑 Stage 内的 Agent 图，也跑 Workflow 的 Stage 图。

调度规则（全部规则，没有别的）：

1. 一个节点的全部前驱到达终态后就绪；就绪节点立刻并行启动。
2. 有条件入边的节点，至少一条条件边触发（谓词对源节点终态为真）才运行，否则记一次
   显式跳过——跳过必须留痕，不能像没这个节点一样。
3. ``each`` 节点对列表逐元素并行运行，产出为对齐的列表；元素级失败按同样的兜底 /
   可选 / 失败规则处理。
4. 重试在节点内：``retries`` 次尝试，``accept`` 不通过也算一次失败；耗尽后依次看
   ``fallback``（确定性兜底，节点成功结束）→ ``optional``（记失败，下游取 default）→
   整图失败并取消兄弟节点。
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .spec import DAG, DAGError, Node, Ref, dig


class NodeAttemptFailed(Exception):
    """一次节点尝试失败；引擎据此决定重试或放弃。``cause`` 保留底层异常供归因。"""

    def __init__(self, reason: str, cause: BaseException | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.cause = cause


class NodeUnavailable(Exception):
    """节点在本运行时不可用（如 Agent 被禁用）：直接跳过或兜底，不算失败也不重试。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NodeFailed(DAGError):
    """必需节点耗尽尝试后仍失败，整张图终止。"""

    def __init__(self, node: str, reason: str, cause: BaseException | None = None) -> None:
        super().__init__(f"node {node!r} failed: {reason}")
        self.node = node
        self.reason = reason
        self.cause = cause


@dataclass
class NodeOutcome:
    """一个节点的终态。``failed`` 只会出现在可选节点上。"""

    name: str
    state: str  # done | skipped | failed
    output: Any = None
    reason: str = ""
    attempts: int = 0
    fallback: bool = False

    @property
    def done(self) -> bool:
        return self.state == "done"


class NodeExecutor(Protocol):
    """引擎与"怎么跑一个节点"之间的契约。``step_name`` 已含展开元素的标签。"""

    def resolve_external(self, ref: Ref) -> Any:
        """解析 ``input`` / ``upstream`` 两类引用。"""

    async def run_node(
        self,
        node: Node,
        kwargs: Mapping[str, Any],
        *,
        attempt: int,
        depends_on: tuple[str, ...],
        step_name: str,
    ) -> Any:
        """跑一次节点并返回产出；失败抛 :class:`NodeAttemptFailed`。"""

    async def run_fallback(
        self,
        node: Node,
        kwargs: Mapping[str, Any],
        reason: str,
        *,
        depends_on: tuple[str, ...],
        step_name: str,
    ) -> Any:
        """执行节点的确定性兜底并留痕。"""

    async def skip_node(
        self,
        node: Node,
        reason: str,
        *,
        depends_on: tuple[str, ...],
        step_name: str,
    ) -> None:
        """为被跳过的节点留痕。"""


def resolve_ref(
    ref: Ref,
    outcomes: Mapping[str, NodeOutcome],
    executor: NodeExecutor,
    *,
    item: Any = None,
    index: int | None = None,
) -> Any:
    """按引用类型取值；节点未成功即给 default。"""

    if ref.kind == "node":
        outcome = outcomes.get(ref.source)
        if outcome is None or not outcome.done:
            return ref.default
        return dig(outcome.output, ref.path, ref.default) if ref.path else outcome.output
    if ref.kind == "item":
        return dig(item, ref.path, ref.default) if ref.path else item
    if ref.kind == "index":
        return index
    return executor.resolve_external(ref)


async def call_fallback(node: Node, reason: str, kwargs: Mapping[str, Any]) -> Any:
    """兜底函数可同步可异步。"""

    assert node.fallback is not None
    output = node.fallback(reason, **kwargs)
    if inspect.isawaitable(output):
        output = await output
    return output


async def execute_dag(dag: DAG, executor: NodeExecutor) -> dict[str, NodeOutcome]:
    """跑完整张图，返回每个节点的终态（按拓扑序）。"""

    order = dag.order()
    outcomes: dict[str, NodeOutcome] = {}
    remaining = list(order)
    running: dict[asyncio.Task[NodeOutcome], str] = {}

    def is_ready(name: str) -> bool:
        return all(pred in outcomes for pred in dag.predecessors(name))

    def conditions_fired(name: str) -> bool | None:
        conditional = [edge for edge in dag.incoming(name) if edge.conditional]
        if not conditional:
            return None
        for edge in conditional:
            source = outcomes.get(edge.source)
            if source is None:
                continue
            try:
                if edge.when(source):  # type: ignore[misc]
                    return True
            except Exception as exc:  # 谓词自己崩了不能被当成"条件为假"静默放过
                raise DAGError(
                    f"condition on edge {edge.source!r} -> {edge.target!r} raised "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        return False

    def resolve_inputs(node: Node, *, item: Any = None, index: int | None = None) -> dict[str, Any]:
        return {
            key: resolve_ref(value, outcomes, executor, item=item, index=index)
            if isinstance(value, Ref)
            else value
            for key, value in node.inputs.items()
        }

    async def attempt_until_done(
        node: Node,
        kwargs: dict[str, Any],
        *,
        step_name: str,
        depends_on: tuple[str, ...],
    ) -> tuple[str, Any, str, int, bool]:
        """返回 (state, output, reason, attempts, used_fallback)。必需节点失败直接抛。"""

        last_reason = ""
        last_cause: BaseException | None = None
        for attempt in range(1, node.retries + 1):
            try:
                output = await executor.run_node(
                    node, kwargs, attempt=attempt, depends_on=depends_on, step_name=step_name
                )
            except NodeUnavailable as exc:
                if node.fallback is not None:
                    output = await executor.run_fallback(
                        node, kwargs, exc.reason, depends_on=depends_on, step_name=step_name
                    )
                    return "done", output, exc.reason, attempt, True
                await executor.skip_node(node, exc.reason, depends_on=depends_on, step_name=step_name)
                return "skipped", None, exc.reason, attempt, False
            except NodeAttemptFailed as exc:
                last_reason, last_cause = exc.reason, exc.cause or exc
                continue
            if node.accept is not None and not node.accept(output):
                last_reason = "output rejected by accept()"
                continue
            return "done", output, "", attempt, False
        reason = last_reason or "exhausted attempts"
        if node.fallback is not None:
            output = await executor.run_fallback(
                node, kwargs, reason, depends_on=depends_on, step_name=step_name
            )
            return "done", output, reason, node.retries, True
        if node.optional:
            return "failed", None, reason, node.retries, False
        raise NodeFailed(step_name, reason, last_cause)

    async def run_node(node: Node) -> NodeOutcome:
        depends_on = dag.predecessors(node.name)
        if node.each is None:
            kwargs = resolve_inputs(node)
            state, output, reason, attempts, used_fallback = await attempt_until_done(
                node, kwargs, step_name=node.name, depends_on=depends_on
            )
            return NodeOutcome(node.name, state, output, reason, attempts, used_fallback)

        items = resolve_ref(node.each, outcomes, executor)
        if items is None:
            items = []
        if isinstance(items, (str, bytes, Mapping)) or not hasattr(items, "__iter__"):
            raise DAGError(f"node {node.name!r}: each must resolve to a list, got {type(items).__name__}")
        items = list(items)

        async def one(index: int, item: Any) -> tuple[str, Any, str, int, bool]:
            suffix = node.label(item, index) if node.label else str(index + 1)
            return await attempt_until_done(
                node,
                resolve_inputs(node, item=item, index=index),
                step_name=f"{node.name}[{suffix}]",
                depends_on=depends_on,
            )

        results = await asyncio.gather(*(one(i, item) for i, item in enumerate(items)))
        outputs = [output for _, output, _, _, _ in results]
        failed_items = [reason for state, _, reason, _, _ in results if state != "done"]
        return NodeOutcome(
            node.name,
            "done",
            outputs,
            reason="; ".join(failed_items),
            attempts=sum(a for _, _, _, a, _ in results),
            fallback=any(f for _, _, _, _, f in results),
        )

    try:
        while remaining or running:
            launched = False
            for name in list(remaining):
                if not is_ready(name):
                    continue
                remaining.remove(name)
                node = dag.nodes[name]
                if conditions_fired(name) is False:
                    reason = "no conditional edge fired"
                    await executor.skip_node(
                        node, reason, depends_on=dag.predecessors(name), step_name=name
                    )
                    outcomes[name] = NodeOutcome(name, "skipped", reason=reason)
                    launched = True
                    continue
                task = asyncio.create_task(run_node(node), name=f"dag:{dag.name}:{name}")
                running[task] = name
                launched = True
            if not running:
                if remaining and not launched:
                    raise DAGError(f"nodes never became ready: {remaining}")
                continue
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                name = running.pop(task)
                outcomes[name] = task.result()
    except BaseException:
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        raise

    return {name: outcomes[name] for name in order if name in outcomes}


__all__ = [
    "NodeAttemptFailed",
    "NodeExecutor",
    "NodeFailed",
    "NodeOutcome",
    "NodeUnavailable",
    "call_fallback",
    "execute_dag",
    "resolve_ref",
]

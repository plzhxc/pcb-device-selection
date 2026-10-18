"""DAG 声明：节点、边、输入引用。

一个 DAG 只回答两个问题——**有哪些节点**、**谁依赖谁**。节点是"能 arun 的东西"
（Agent 类、Agent 实例、一段 Python，或另一张 DAG），边是执行顺序或条件。业务写的
就是这一张图，执行交给 :mod:`agent_framework.dag.engine`。

依赖不必手写两遍：节点输入里 ``From("上游节点", "键")`` 本身就声明了一条数据边，
引擎自动补齐；显式 ``edges`` 只在需要**条件边**或**无数据的纯顺序约束**时才写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence


class DAGError(ValueError):
    """DAG 声明不合法：重名、悬空引用、成环、Item 用在非展开节点上。"""


@dataclass(frozen=True)
class Ref:
    """对某个值的引用，运行时由引擎解析。

    Attributes:
        kind: ``input``（本图入参）、``upstream``（上游 stage 产出）、``node``（本图节点
            产出）、``item``（展开节点的当前元素）、``index``（当前元素序号）。
        source: 入参键 / stage 名 / 节点名；``item`` / ``index`` 为空。
        path: 在取到的值上继续按键下钻。
        default: 取不到（节点被跳过、键不存在）时的值。
    """

    kind: str
    source: str = ""
    path: tuple[str, ...] = ()
    default: Any = None

    def at(self, *keys: str) -> "Ref":
        return Ref(self.kind, self.source, self.path + tuple(keys), self.default)


def Input(key: str, *path: str, default: Any = None) -> Ref:
    """引用本图的入参：顶层图是 Stage 直接入参，子图是父节点传入的 inputs。"""

    return Ref("input", key, tuple(path), default)


def Upstream(stage: str, *path: str, default: Any = None) -> Ref:
    """引用上游 stage 的产出。"""

    return Ref("upstream", stage, tuple(path), default)


def From(node: str, *path: str, default: Any = None) -> Ref:
    """引用本图内某节点的产出；同时声明一条 ``node → 本节点`` 的数据边。"""

    return Ref("node", node, tuple(path), default)


def Item(*path: str, default: Any = None) -> Ref:
    """展开节点（``each``）里当前元素。"""

    return Ref("item", "", tuple(path), default)


def ItemIndex() -> Ref:
    """展开节点里当前元素的序号（从 0 起）。"""

    return Ref("index")


def dig(value: Any, path: Sequence[str], default: Any = None) -> Any:
    """沿 ``path`` 逐键下钻，任何一层缺失即返回 ``default``。"""

    current = value
    for key in path:
        if isinstance(current, Mapping) and key in current:
            current = current[key]
        else:
            return default
    return current


@dataclass(frozen=True)
class Node:
    """DAG 中的一个执行单元。

    Attributes:
        name: 节点名，也是 trace 里的步骤名。
        run: Agent 类（由运行时实例化）、带 ``arun`` 的实例、Python 可调用对象，或子图
            :class:`DAG`（其 ``Input`` 引用解析到本节点的 ``inputs``）。
        inputs: 传给 ``run`` 的关键字参数；值为 :class:`Ref` 时运行期解析。
        each: 给出一个列表引用即按元素展开：每个元素跑一次，产出为对齐的列表；
            ``inputs`` 里用 ``Item()`` / ``ItemIndex()`` 取当前元素。
        label: ``(item, index) -> str``，展开时每个元素的步骤名后缀；默认序号。
        optional: 失败或运行时不可用时不拖垮整图，下游按 default 取值。
        retries: 最多尝试次数；配合 ``accept`` 使用。每次尝试都是新实例。
        accept: 对产出的接受谓词，不通过视为一次失败尝试。
        fallback: ``(reason, **inputs) -> 产出``，全部尝试失败后的确定性兜底；给了它
            节点就以兜底产出成功结束，并在 trace 里记一个 ``.fallback`` 步骤。
        kind: 步骤类型（``agent`` / ``python``），留空由引擎推断。
        semaphore: 并发上限所用的命名信号量（``StageContext.semaphore``）。
    """

    name: str
    run: Any
    inputs: Mapping[str, Any] = field(default_factory=dict)
    each: Ref | None = None
    label: Callable[[Any, int], str] | None = None
    optional: bool = False
    retries: int = 1
    accept: Callable[[Any], bool] | None = None
    fallback: Callable[..., Any] | None = None
    kind: str = ""
    semaphore: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise DAGError("node name must not be empty")
        if self.retries < 1:
            raise DAGError(f"node {self.name!r}: retries must be >= 1")
        uses_item = any(
            isinstance(v, Ref) and v.kind in {"item", "index"} for v in self.inputs.values()
        )
        if uses_item and self.each is None:
            raise DAGError(f"node {self.name!r} uses Item()/ItemIndex() without each=")
        if self.each is not None and self.each.kind not in {"node", "input", "upstream"}:
            raise DAGError(f"node {self.name!r}: each must reference a node, input or upstream")

    def data_dependencies(self) -> tuple[str, ...]:
        """从输入与 ``each`` 里的 ``From`` 引用推出的上游节点名（去重、保序）。"""

        seen: dict[str, None] = {}
        if self.each is not None and self.each.kind == "node":
            seen.setdefault(self.each.source)
        for value in self.inputs.values():
            if isinstance(value, Ref) and value.kind == "node":
                seen.setdefault(value.source)
        return tuple(seen)


@dataclass(frozen=True)
class Edge:
    """一条边。``when`` 为空是数据/顺序边；给了谓词就是条件边。

    谓词收源节点的**终态**（含 ``done`` / ``state`` / ``output``），因此既能表达
    "成功且产出满足 X"，也能表达"失败时降级"。用 :func:`succeeded` / :func:`failed`
    构造最常见的两种。
    """

    source: str
    target: str
    when: Callable[[Any], bool] | None = None
    label: str = ""

    @property
    def conditional(self) -> bool:
        return self.when is not None


def succeeded(predicate: Callable[[Any], bool] | None = None) -> Callable[[Any], bool]:
    """源节点成功，且（可选）产出满足谓词。"""

    def when(outcome: Any) -> bool:
        return bool(outcome.done and (predicate is None or predicate(outcome.output)))

    return when


def failed() -> Callable[[Any], bool]:
    """源节点未成功（失败或被跳过）。"""

    return lambda outcome: not outcome.done


class DAG:
    """一张有向无环图。构造即校验。"""

    def __init__(
        self,
        nodes: Sequence[Node],
        edges: Iterable[Edge | tuple[str, str]] = (),
        *,
        outputs: Mapping[str, Ref] | None = None,
        name: str = "",
    ) -> None:
        self.name = name
        self.nodes: dict[str, Node] = {}
        for node in nodes:
            if node.name in self.nodes:
                raise DAGError(f"duplicate node name {node.name!r}")
            self.nodes[node.name] = node

        collected: list[Edge] = []
        for item in edges:
            collected.append(item if isinstance(item, Edge) else Edge(item[0], item[1]))
        explicit = {(e.source, e.target) for e in collected}
        for node in self.nodes.values():
            for upstream in node.data_dependencies():
                if (upstream, node.name) not in explicit:
                    collected.append(Edge(upstream, node.name))
                    explicit.add((upstream, node.name))
        for edge in collected:
            for endpoint in (edge.source, edge.target):
                if endpoint not in self.nodes:
                    raise DAGError(
                        f"edge {edge.source!r} -> {edge.target!r} references unknown node {endpoint!r}"
                    )
            if edge.source == edge.target:
                raise DAGError(f"self loop on {edge.source!r}")
        self.edges: tuple[Edge, ...] = tuple(collected)

        self.outputs: dict[str, Ref] | None = dict(outputs) if outputs is not None else None
        if self.outputs:
            for key, ref in self.outputs.items():
                if ref.kind == "node" and ref.source not in self.nodes:
                    raise DAGError(f"output {key!r} references unknown node {ref.source!r}")

        self._order = self._topological_order()

    # ------------------------------ 结构查询 ------------------------------ #
    def predecessors(self, name: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(e.source for e in self.edges if e.target == name))

    def successors(self, name: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(e.target for e in self.edges if e.source == name))

    def incoming(self, name: str) -> tuple[Edge, ...]:
        return tuple(e for e in self.edges if e.target == name)

    def order(self) -> tuple[str, ...]:
        """拓扑序；同层保持声明顺序。"""

        return self._order

    def _topological_order(self) -> tuple[str, ...]:
        indegree = {name: 0 for name in self.nodes}
        for edge in self.edges:
            indegree[edge.target] += 1
        ready = [name for name in self.nodes if indegree[name] == 0]
        order: list[str] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for succ in self.successors(current):
                indegree[succ] -= 1
                if indegree[succ] == 0:
                    ready.append(succ)
        if len(order) != len(self.nodes):
            cyclic = sorted(name for name, degree in indegree.items() if degree > 0)
            raise DAGError(f"cycle detected among nodes {cyclic}")
        return tuple(order)

    def layers(self) -> tuple[tuple[str, ...], ...]:
        """按依赖深度分层：同一层的节点互相无依赖，引擎会并行启动它们。

        节点的层号 = 其最长前驱链长度，因此这是"最早可开始时刻"的分层；跨层的顺序
        就是串行关系。这是读一张声明式 DAG 时判断并行 / 串行最直接的视图。
        """

        depth: dict[str, int] = {}
        for name in self._order:
            preds = self.predecessors(name)
            depth[name] = 1 + max((depth[p] for p in preds), default=-1)
        layers: dict[int, list[str]] = {}
        for name in self._order:
            layers.setdefault(depth[name], []).append(name)
        return tuple(tuple(layers[i]) for i in sorted(layers))

    def ancestors(self, name: str) -> set[str]:
        found: set[str] = set()
        frontier = list(self.predecessors(name))
        while frontier:
            current = frontier.pop()
            if current in found:
                continue
            found.add(current)
            frontier.extend(self.predecessors(current))
        return found

    def descendants(self, name: str) -> set[str]:
        found: set[str] = set()
        frontier = list(self.successors(name))
        while frontier:
            current = frontier.pop()
            if current in found:
                continue
            found.add(current)
            frontier.extend(self.successors(current))
        return found

    def subgraph(
        self,
        *,
        start_from: str | None = None,
        stop_after: str | None = None,
    ) -> "DAG":
        """断点续跑用的子图：去掉起点的全部祖先、终点的全部后代。"""

        for label, value in (("start_from", start_from), ("stop_after", stop_after)):
            if value is not None and value not in self.nodes:
                raise DAGError(f"unknown {label}={value!r}; available: {list(self.nodes)}")
        if start_from is not None and stop_after is not None:
            if start_from in self.descendants(stop_after):
                raise DAGError("stop_after cannot be earlier than start_from")
        excluded: set[str] = set()
        if start_from is not None:
            excluded |= self.ancestors(start_from)
        if stop_after is not None:
            excluded |= self.descendants(stop_after)
        keep = [name for name in self._order if name not in excluded]
        kept = set(keep)
        outputs = None
        if self.outputs is not None:
            outputs = {
                key: ref
                for key, ref in self.outputs.items()
                if ref.kind != "node" or ref.source in kept
            }
        return DAG(
            [self.nodes[name] for name in keep],
            [e for e in self.edges if e.source in kept and e.target in kept],
            outputs=outputs,
            name=self.name,
        )

    # ------------------------------ 导出 ------------------------------ #
    def as_dict(self) -> dict[str, Any]:
        """静态结构，供前端画图或文档生成；子图内嵌导出。"""

        def ref_dict(ref: Ref) -> dict[str, Any]:
            return {"ref": ref.kind, "source": ref.source, "path": list(ref.path)}

        return {
            "name": self.name,
            "nodes": [
                {
                    "name": node.name,
                    "run": describe_target(node.run),
                    "kind": node.kind or infer_kind(node.run),
                    "optional": node.optional,
                    "retries": node.retries,
                    "fallback": node.fallback is not None,
                    **({"each": ref_dict(node.each)} if node.each is not None else {}),
                    **({"dag": node.run.as_dict()} if isinstance(node.run, DAG) else {}),
                    "inputs": {
                        key: ref_dict(value) if isinstance(value, Ref) else {"literal": repr(value)}
                        for key, value in node.inputs.items()
                    },
                }
                for node in self.nodes.values()
            ],
            "edges": [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "kind": "conditional" if edge.conditional else "data",
                    **({"label": edge.label} if edge.label else {}),
                }
                for edge in self.edges
            ],
            "outputs": (
                {key: ref_dict(ref) for key, ref in self.outputs.items()}
                if self.outputs is not None
                else None
            ),
            "order": list(self._order),
            "layers": [list(layer) for layer in self.layers()],
        }


def describe_target(target: Any) -> str:
    if isinstance(target, DAG):
        return f"dag:{target.name}"
    if isinstance(target, type):
        return target.__name__
    if callable(target) and not hasattr(target, "arun"):
        return getattr(target, "__name__", type(target).__name__)
    return type(target).__name__


def infer_kind(target: Any) -> str:
    if isinstance(target, DAG):
        return "dag"
    if isinstance(target, type) or hasattr(target, "arun"):
        return str(getattr(target, "step_kind", "agent") or "agent")
    return "python"


__all__ = [
    "DAG",
    "DAGError",
    "Edge",
    "From",
    "Input",
    "Item",
    "ItemIndex",
    "Node",
    "Ref",
    "Upstream",
    "describe_target",
    "dig",
    "failed",
    "infer_kind",
    "succeeded",
]

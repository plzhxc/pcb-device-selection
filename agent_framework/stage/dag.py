"""Actual Stage execution DAG contract.

The DAG is produced by the Stage after its imperative orchestration has run.
Its public nodes are actual Agent runs. Step handles are only the Stage's
correlation mechanism; Trace never promotes them into another graph layer or
uses timestamps/``seq`` to guess control flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


class StageDAGError(ValueError):
    """The Stage returned an invalid or cyclic execution graph."""


@dataclass(frozen=True, slots=True)
class StageDAGNode:
    node_id: str
    node_type: str
    name: str
    role: str = ""
    step_id: str = ""
    agent_run_id: str = ""
    kind: str = ""
    status: str = ""
    skipped: bool = False
    duration_ms: int | None = None
    agent_run_ids: Sequence[str] = ()
    output_keys: Sequence[str] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "type": self.node_type,
            "name": self.name,
            "role": self.role,
            "step_id": self.step_id,
            "agent_run_id": self.agent_run_id,
            "kind": self.kind,
            "status": self.status,
            "skipped": self.skipped,
            "duration_ms": self.duration_ms,
            "agent_run_ids": list(self.agent_run_ids),
            "output_keys": list(self.output_keys),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class StageDAGEdge:
    source: str
    target: str
    relation: str = "depends_on"
    label: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.source,
            "to": self.target,
            "type": self.relation,
            "label": self.label,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class StageDAG:
    """One Stage's concrete graph for one actual run."""

    stage_name: str
    nodes: Sequence[StageDAGNode]
    edges: Sequence[StageDAGEdge]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    version: int = 1

    @classmethod
    def from_steps(
        cls,
        stage_name: str,
        steps: Sequence[Any],
        *,
        dependencies: Mapping[str, Sequence[str]] | None = None,
        metadata: Mapping[str, Any] | None = None,
        include_step_dependencies: bool = True,
    ) -> "StageDAG":
        """Build an Agent DAG from executed steps and Stage dependencies.

        Dependency keys and values may reference a step's ``name`` or immutable
        ``step_id``. A name resolving to several actual steps expands to all of
        them for explicit fan-in. Missing references remain node diagnostics;
        they never become guessed edges.
        """

        declared = dependencies or {}
        nodes = [
            StageDAGNode(
                node_id="stage:start",
                node_type="stage_start",
                name=stage_name,
                status="success",
            )
        ]
        by_name: dict[str, list[Any]] = {}
        by_id: dict[str, Any] = {}
        for step in steps:
            step_id = str(getattr(step, "step_id", "") or "")
            if not step_id or step_id in by_id:
                raise StageDAGError(
                    "every executed Stage step needs a unique step_id"
                )
            by_id[step_id] = step
            name = str(getattr(step, "name", "") or step_id)
            by_name.setdefault(name, []).append(step)

        unresolved: dict[str, list[str]] = {}
        edges: list[StageDAGEdge] = []
        for step in steps:
            step_id = str(step.step_id)
            name = str(getattr(step, "name", "") or step_id)
            explicit = (
                tuple(getattr(step, "depends_on", ()) or ())
                if include_step_dependencies
                else ()
            )
            references = explicit or tuple(declared.get(name, ()) or ())
            source_ids: list[str] = []
            for reference in references:
                ref = str(reference)
                if ref == "stage:start":
                    source_ids.append(ref)
                    continue
                if ref in by_id:
                    source_ids.append(f"step:{ref}")
                    continue
                matches = by_name.get(ref, [])
                if matches:
                    source_ids.extend(
                        f"step:{item.step_id}" for item in matches
                    )
                    continue
                unresolved.setdefault(step_id, []).append(ref)
            if not references:
                source_ids.append("stage:start")
            for source in dict.fromkeys(source_ids):
                target = f"step:{step_id}"
                if source == target:
                    raise StageDAGError(
                        f"step {name!r} cannot depend on itself"
                    )
                edges.append(StageDAGEdge(source=source, target=target))

            node_metadata: dict[str, Any] = {}
            if unresolved.get(step_id):
                node_metadata["unresolved_dependencies"] = unresolved[step_id]
            nodes.append(
                StageDAGNode(
                    node_id=f"step:{step_id}",
                    node_type="step",
                    name=name,
                    step_id=step_id,
                    kind=str(getattr(step, "kind", "agent") or "agent"),
                    status=str(step.status()),
                    skipped=bool(getattr(step, "skipped", False)),
                    duration_ms=getattr(step, "duration_ms", None),
                    agent_run_ids=tuple(
                        getattr(step, "agent_runs", ()) or ()
                    ),
                    output_keys=tuple(getattr(step, "outputs", {}) or ()),
                    metadata=node_metadata,
                )
            )

        step_nodes = {
            node.node_id for node in nodes if node.node_type == "step"
        }
        outgoing = {
            edge.source for edge in edges if edge.source in step_nodes
        }
        if step_nodes:
            for source in sorted(step_nodes - outgoing):
                edges.append(
                    StageDAGEdge(source=source, target="stage:end")
                )
        else:
            edges.append(
                StageDAGEdge(source="stage:start", target="stage:end")
            )
        nodes.append(
            StageDAGNode(
                node_id="stage:end",
                node_type="stage_end",
                name=stage_name,
            )
        )

        graph_metadata: dict[str, Any] = {
            "actual": not bool(unresolved),
            "dependency_source": "stage",
            **dict(metadata or {}),
        }
        if unresolved:
            graph_metadata["actual"] = False
            graph_metadata["dag_error"] = "unresolved Stage DAG dependencies"
            graph_metadata["unresolved_dependencies"] = {
                step_id: list(references)
                for step_id, references in unresolved.items()
            }
        step_graph = cls(
            stage_name=stage_name,
            nodes=tuple(nodes),
            edges=tuple(_deduplicate_edges(edges)),
            metadata=graph_metadata,
        )
        step_graph.validate()
        graph = _agent_dag_from_step_graph(step_graph, by_id)
        graph.validate()
        return graph

    def validate(self) -> None:
        ids = [node.node_id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise StageDAGError("Stage DAG node ids must be unique")
        known = set(ids)
        for edge in self.edges:
            if edge.source not in known or edge.target not in known:
                raise StageDAGError(
                    "Stage DAG edge references a missing node: "
                    f"{edge.source!r} -> {edge.target!r}"
                )
        indegree = {node_id: 0 for node_id in known}
        outgoing: dict[str, list[str]] = {
            node_id: [] for node_id in known
        }
        for edge in self.edges:
            if edge.relation != "depends_on":
                continue
            indegree[edge.target] += 1
            outgoing[edge.source].append(edge.target)
        queue = [
            node_id for node_id, degree in indegree.items() if degree == 0
        ]
        visited = 0
        while queue:
            node_id = queue.pop()
            visited += 1
            for target in outgoing[node_id]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if visited != len(known):
            raise StageDAGError("Stage DAG contains a dependency cycle")

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "stage_name": self.stage_name,
            "nodes": [node.as_dict() for node in self.nodes],
            "edges": [edge.as_dict() for edge in self.edges],
            "metadata": dict(self.metadata),
        }


def _deduplicate_edges(
    edges: Sequence[StageDAGEdge],
) -> list[StageDAGEdge]:
    result: list[StageDAGEdge] = []
    seen: set[tuple[str, str, str, str]] = set()
    for edge in edges:
        key = (edge.source, edge.target, edge.relation, edge.label)
        if key in seen:
            continue
        seen.add(key)
        result.append(edge)
    return result


def _agent_dag_from_step_graph(
    graph: StageDAG,
    steps_by_id: Mapping[str, Any],
) -> StageDAG:
    """Return the Stage's graph at Agent granularity.

    The Stage already supplied all edges. This function only replaces its
    correlation Step nodes with their primary Agent run ids and lets paths pass
    transparently through pure control Steps. It does not reorder or infer any
    dependency.
    """

    visible: dict[str, str] = {
        "stage:start": "stage:start",
        "stage:end": "stage:end",
    }
    nodes: list[StageDAGNode] = []
    for node in graph.nodes:
        if node.node_type in {"stage_start", "stage_end"}:
            nodes.append(node)
            continue
        step = steps_by_id.get(node.step_id)
        run_ids = tuple(getattr(step, "agent_runs", ()) or ())
        if not run_ids:
            continue
        # HierarchicalAgent-derived runs are internal details. The first id is
        # the Stage-invoked Agent run and therefore the DAG node identity.
        run_id = str(run_ids[0])
        projected_id = f"agent:{run_id}"
        visible[node.node_id] = projected_id
        nodes.append(
            StageDAGNode(
                node_id=projected_id,
                node_type="agent",
                name=str(getattr(step, "agent_name", "") or node.name),
                role=node.name,
                step_id=node.step_id,
                agent_run_id=run_id,
                kind=node.kind,
                status=node.status,
                skipped=node.skipped,
                duration_ms=node.duration_ms,
                agent_run_ids=(run_id,),
                output_keys=node.output_keys,
                metadata=node.metadata,
            )
        )

    outgoing: dict[str, list[str]] = {
        node.node_id: [] for node in graph.nodes
    }
    for edge in graph.edges:
        outgoing[edge.source].append(edge.target)

    edges: list[StageDAGEdge] = []
    for raw_source, source in visible.items():
        pending = list(outgoing.get(raw_source) or [])
        traversed: set[str] = set()
        while pending:
            raw_target = pending.pop()
            if raw_target in traversed:
                continue
            traversed.add(raw_target)
            target = visible.get(raw_target)
            if target:
                if source != target:
                    edges.append(StageDAGEdge(source=source, target=target))
                continue
            pending.extend(outgoing.get(raw_target) or [])

    return StageDAG(
        stage_name=graph.stage_name,
        nodes=tuple(nodes),
        edges=tuple(_deduplicate_edges(edges)),
        metadata={
            **dict(graph.metadata),
            "granularity": "agent",
            "agent_internal_details": False,
        },
        version=graph.version,
    )


__all__ = [
    "StageDAG",
    "StageDAGEdge",
    "StageDAGError",
    "StageDAGNode",
]

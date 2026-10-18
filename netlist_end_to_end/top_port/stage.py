"""Top-port stage, declared as a DAG.

Stage:      inputs ─(has ports)─▶ reconcile ─▶ result
Reconcile:  hierarchical ─(failed)─▶ sharded ─▶ strategy ─▶ boundary
            ─▶ aggregate ─▶ aggregated ─▶ lint ─▶ finalize

Three-level degradation is expressed with edges and fallbacks: the hierarchical
pair plan runs first; if it fails the sharded per-module analysis fires; if that
fails too its deterministic fallback preserves every declared port as unmatched.
With ``top_port_strategy="sharded"`` the hierarchical branch is not declared.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import partial
from typing import Any

from agent_framework.dag import DAG, DAGStage, Edge, From, Input, Item, Node, Upstream, failed, succeeded

from ..contracts import FlowContractError, FlowOptions, StageName
from ..generation_input import generation_decomposition, validate_generation_input
from .agents import TopPortAggregatorAgent, TopPortBoundaryAgent, TopPortLinterAgent, TopPortPairAgent, TopPortPlannerAgent, TopPortShardAgent
from .helpers import dependency_pairs

SEMAPHORE = "top_port"


# ------------------------------ python nodes ------------------------------ #
def read_inputs(netlist: Any, generation_input: Any, user_input: str = "") -> dict[str, Any]:
    if not isinstance(netlist, Sequence) or isinstance(netlist, (str, bytes)):
        raise FlowContractError("top_port requires a netlist result list")
    module_results = [dict(item) for item in netlist if isinstance(item, Mapping)]
    data = validate_generation_input(generation_input)
    decomposition = generation_decomposition(data)
    cross_by_id = cross_page_entries(module_results)
    return {
        "module_results": module_results,
        "decomposition": dict(decomposition),
        "requirement_docs": data.get("description") or str(user_input or ""),
        "cross_by_id": cross_by_id,
        "has_ports": bool(cross_by_id),
    }


def cross_page_entries(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        module = row.get("module_info") or row.get("module") or {}
        module = module if isinstance(module, Mapping) else {}
        info = row.get("netlist_info") or {}
        info = info if isinstance(info, Mapping) else {}
        cross = info.get("crossPageNet") or info.get("cross_page_net") or row.get("crossPageNet") or row.get("cross_page_net")
        if not isinstance(cross, Mapping) or not (cross.get("inputs") or cross.get("outputs")):
            continue
        module_id = str(module.get("id") or module.get("name") or f"module_{index + 1}")
        entries[module_id] = {"module_info": dict(module), "crossPageNet": dict(cross), "notes": info.get("notes") or row.get("notes") or ""}
    return entries


def boundary_fallback(reason: str, **kwargs: Any) -> dict[str, Any]:
    return {}


def pairs_of(planner: Mapping[str, Any], cross_by_id: Mapping[str, Any], decomposition: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = planner.get("pairs", planner.get("pair_tasks", planner.get("sub_tasks")))
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise FlowContractError("top_port planner must return pairs/pair_tasks/sub_tasks")
    pairs, covered = [], set()
    for task in raw:
        if not isinstance(task, Mapping):
            raise FlowContractError("top_port pair task must be an object")
        source, target = task.get("source_module"), task.get("target_module")
        if not isinstance(source, str) or not isinstance(target, str) or source not in cross_by_id or target not in cross_by_id or source == target:
            raise FlowContractError("top_port pair must name two different existing modules")
        identity = tuple(sorted((source, target)))
        if identity in covered:
            raise FlowContractError(f"duplicate top_port module pair: {identity}")
        for candidate in task.get("net_candidates") or []:
            if not isinstance(candidate, Mapping):
                raise FlowContractError("top_port net candidate must be an object")
            for side, module in (("source", source), ("target", target)):
                cross = cross_by_id[module]["crossPageNet"]
                if candidate.get(f"{side}_net") not in [*cross.get("inputs", []), *cross.get("outputs", [])]:
                    raise FlowContractError(f"top_port candidate endpoint does not exist: {module}:{candidate.get(f'{side}_net')}")
        covered.add(identity)
        pairs.append(dict(task))
    missing = dependency_pairs(decomposition, set(cross_by_id)) - covered
    if missing:
        raise FlowContractError(f"top_port planner omitted explicit module relationships: {sorted(missing)}")
    return pairs


def pair_results(pairs: list[dict[str, Any]], payloads: list[Any]) -> dict[str, Any]:
    return {"strategy": "hierarchical", "results": [{"pair": p, "result": r} for p, r in zip(pairs, payloads)]}


def shard_items(cross_by_id: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"module_id": module_id, "entry": entry} for module_id, entry in cross_by_id.items()]


def shard_results(items: list[dict[str, Any]], payloads: list[Any]) -> dict[str, Any]:
    return {"strategy": "sharded", "results": [{"module_id": i["module_id"], "result": r} for i, r in zip(items, payloads)]}


def unmatched_fallback(reason: str, **kwargs: Any) -> dict[str, Any]:
    """Preserve every declared port honestly when reconciliation fails."""

    results = []
    for module_id, entry in (kwargs.get("cross_by_id") or {}).items():
        cross = entry.get("crossPageNet") if isinstance(entry, Mapping) else None
        cross = cross if isinstance(cross, Mapping) else {}
        unmatched = [
            {"module_id": str(module_id), "net": str(net), "direction": direction, "reason": "Agent reconciliation unavailable; preserved from netlist."}
            for key, direction in (("inputs", "input"), ("outputs", "output"))
            for net in (cross.get(key) or [] if isinstance(cross.get(key), Sequence) and not isinstance(cross.get(key), (str, bytes)) else [])
            if str(net).strip()
        ]
        results.append({"module_id": str(module_id), "result": {"unmatched": unmatched}})
    return {"strategy": "deterministic_unmatched_fallback", "results": results}


def choose_strategy(hierarchical: Any, sharded: Any, configured: str) -> dict[str, Any]:
    if isinstance(hierarchical, Mapping):
        return dict(hierarchical)
    if isinstance(sharded, Mapping):
        chosen = dict(sharded)
        if configured == "hierarchical" and chosen.get("strategy") == "sharded":
            chosen["strategy"] = "sharded_fallback"
        return chosen
    raise FlowContractError("top_port produced no reconciliation results")


def result_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("top_port_result", "result", "reconciliation"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return dict(payload)


def aggregate_fallback(reason: str, **kwargs: Any) -> dict[str, Any]:
    aggregate: dict[str, list[Any]] = {"internal_connection": [], "system_input": [], "system_output": [], "unmatched": []}
    aliases = {
        "internal_connection": ("internal_connection", "internal_connections", "internal_connection_candidates"),
        "system_input": ("system_input", "inputs"),
        "system_output": ("system_output", "outputs"),
        "unmatched": ("unmatched", "exposed_ports"),
    }
    for item in kwargs.get("strategy_results") or []:
        raw = item.get("result") if isinstance(item, Mapping) else None
        if not isinstance(raw, Mapping):
            continue
        payload = result_payload(raw)
        for target, names in aliases.items():
            value = next((payload[n] for n in names if n in payload), None)
            if value is None:
                continue
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                aggregate[target].extend(value)
            else:
                aggregate[target].append(value)
    return aggregate


def aggregated_view(aggregate: Mapping[str, Any], strategy: str, boundary: Mapping[str, Any]) -> dict[str, Any]:
    result = result_payload(aggregate)
    result.setdefault("_boundary", dict(boundary))
    result.setdefault("strategy", strategy)
    return result


def lint_fallback(reason: str, **kwargs: Any) -> dict[str, Any]:
    return {"ok": True, "status": "skipped", "issues": []}


def lint_ok(value: Mapping[str, Any]) -> bool:
    for key in ("ok", "passed", "valid"):
        if isinstance(value.get(key), bool):
            return bool(value[key])
    return str(value.get("status") or "").strip().lower() in {"", "ok", "pass", "passed", "success", "skipped"}


def finalize(result: Mapping[str, Any], lint: Mapping[str, Any], fail_on_lint: bool) -> dict[str, Any]:
    out = dict(result)
    out["lint"] = dict(lint)
    if not lint_ok(out["lint"]):
        errors = [str(item.get("message") or item.get("code")) for item in lint.get("issues", []) if item.get("severity") == "error"]
        raise FlowContractError("top_port reconciliation incomplete or contradictory: " + "; ".join(errors))
    return out


def stage_result(reconcile: Any) -> dict[str, Any]:
    return dict(reconcile) if isinstance(reconcile, Mapping) else {}


# ------------------------------ sub-DAGs ------------------------------ #
def declare_hierarchical() -> DAG:
    cross, decomposition = Input("cross_by_id"), Input("decomposition")
    return DAG(
        name="hierarchical",
        nodes=[
            Node("plan", TopPortPlannerAgent, inputs={"cross_by_id": cross, "module_decomposition": decomposition, "requirement_docs": Input("requirement_docs")}),
            Node("pairs", pairs_of, inputs={"planner": From("plan"), "cross_by_id": cross, "decomposition": decomposition}),
            Node(
                "pair", TopPortPairAgent, each=From("pairs"), semaphore=SEMAPHORE,
                inputs={"pair": Item(), "cross_by_id": cross, "module_decomposition": decomposition},
            ),
            Node("results", pair_results, inputs={"pairs": From("pairs"), "payloads": From("pair")}),
        ],
        outputs={"result": From("results")},
    )


def declare_sharded() -> DAG:
    cross, decomposition = Input("cross_by_id"), Input("decomposition")
    return DAG(
        name="sharded",
        nodes=[
            Node("items", shard_items, inputs={"cross_by_id": cross}),
            Node(
                "shard", TopPortShardAgent, each=From("items"), semaphore=SEMAPHORE,
                label=lambda item, i: str(item.get("module_id") or i + 1),
                inputs={"module_id": Item("module_id"), "entry": Item("entry"), "cross_by_id": cross, "module_decomposition": decomposition},
            ),
            Node("results", shard_results, inputs={"items": From("items"), "payloads": From("shard")}),
        ],
        outputs={"result": From("results")},
    )


def declare_reconcile(options: FlowOptions) -> DAG:
    cross, decomposition = Input("cross_by_id"), Input("decomposition")
    strategy_inputs = {"cross_by_id": cross, "decomposition": decomposition, "requirement_docs": Input("requirement_docs")}
    nodes = []
    edges: list[Edge] = []
    if options.top_port_strategy == "hierarchical":
        nodes.append(Node("hierarchical", declare_hierarchical(), optional=True, inputs=strategy_inputs))
        nodes.append(Node("sharded", declare_sharded(), optional=True, fallback=unmatched_fallback, inputs=strategy_inputs))
        edges.append(Edge("hierarchical", "sharded", when=failed(), label="hierarchical failed"))
    else:
        nodes.append(Node("sharded", declare_sharded(), optional=True, fallback=unmatched_fallback, inputs=strategy_inputs))
    nodes += [
        Node(
            "strategy", partial(choose_strategy, configured=options.top_port_strategy),
            inputs={
                "hierarchical": From("hierarchical", default=None) if options.top_port_strategy == "hierarchical" else None,
                "sharded": From("sharded", default=None),
            },
        ),
        Node("boundary", TopPortBoundaryAgent, optional=True, fallback=boundary_fallback,
             inputs={"cross_by_id": cross, "module_decomposition": decomposition,
                     "requirement_docs": Input("requirement_docs"), "strategy_results": From("strategy", "results")}),
        Node(
            "aggregate", TopPortAggregatorAgent,
            inputs={
                "strategy": From("strategy", "strategy"), "strategy_results": From("strategy", "results"),
                "cross_by_id": cross, "boundary": From("boundary", default={}), "module_decomposition": decomposition,
            },
        ),
        Node("aggregated", aggregated_view, inputs={"aggregate": From("aggregate"), "strategy": From("strategy", "strategy"), "boundary": From("boundary", default={})}),
        Node(
            "lint", TopPortLinterAgent,
            inputs={"top_port_result": From("aggregated"), "netlist_output": Input("module_results"), "module_decomposition": decomposition},
        ),
        Node("finalize", finalize, inputs={"result": From("aggregated"), "lint": From("lint"), "fail_on_lint": options.fail_on_lint}),
    ]
    return DAG(nodes, edges, outputs={"result": From("finalize")}, name="reconcile")


class TopPortStage(DAGStage):
    name = StageName.TOP_PORT.value

    def __init__(self, options: FlowOptions, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.semaphores = {SEMAPHORE: options.top_port_concurrency}
        self.dag = self.declare(options)

    @classmethod
    def declare(cls, options: FlowOptions | None = None) -> DAG:
        options = options or FlowOptions()
        return DAG(
            name=cls.name,
            nodes=[
                Node(
                    "inputs", read_inputs,
                    inputs={
                        "netlist": Upstream(StageName.NETLIST.value, "result"),
                        "generation_input": Input("generation_input"),
                        "user_input": Input("user_input", default=""),
                    },
                ),
                Node(
                    "reconcile", declare_reconcile(options),
                    inputs={
                        "cross_by_id": From("inputs", "cross_by_id"), "decomposition": From("inputs", "decomposition"),
                        "requirement_docs": From("inputs", "requirement_docs"), "module_results": From("inputs", "module_results"),
                    },
                ),
                Node("result", stage_result, inputs={"reconcile": From("reconcile", default=None)}),
            ],
            edges=[Edge("inputs", "reconcile", when=succeeded(lambda o: bool(o.get("has_ports"))), label="has cross-page ports")],
            outputs={"result": From("result")},
        )


__all__ = ["TopPortStage"]

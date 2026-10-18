"""Netlist stage, declared as a DAG of sub-DAGs.

Stage:    inputs ─▶ module[each row] ─▶ normalize ─▶ finalize ─▶ common_devices
Module:   module_of ─▶ prefetch ─▶ prebuild ─(not matched)─▶ planned ─▶ assemble
Planned:  guidance ─▶ plan ─▶ plan_gate ─▶ worker[each sub_task] ─▶ merge ─▶ merged
          ─▶ lint_0 ─(not ok)─▶ repair_1 ─▶ repaired_1 ─▶ lint_1 … ─▶ finalize

A module whose sub-DAG fails (catalog evidence missing, plan gate violation,
workers exhausted) falls back to a visible failed artifact so sibling modules
keep their netlists.  Lint → repair rounds are unrolled by
``FlowOptions.netlist_repair_rounds``; lint is advisory unless ``fail_on_lint``.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any

from agent_framework.dag import DAG, DAGStage, Edge, From, Input, Item, ItemIndex, Node, succeeded

from ..contracts import FlowOptions, StageName
from ..tools import NetlistKnowledgePrefetcher
from .agents import NetlistLinterAgent, NetlistMergerAgent, NetlistNormalizerAgent, NetlistPlannerAgent, NetlistPrebuilderAgent, NetlistRepairerAgent, NetlistWorkerAgent, ReferenceGuidanceAgent
from .stage_results import (
    abstractize_common_devices, failed_artifact as failed_artifact, failed_module, finalize,
    knowledge_failed as knowledge_failed, lint_ok, module_failed as module_failed,
    module_label, netlist_info as netlist_info, read_inputs,
)
from .stage_module import (
    assemble_module, finalize_planned, lint_fallback, merge_fallback, merged_view,
    module_of, plan_gate, prebuild_matched, prefetch_knowledge, repaired_view,
)

MODULES = "netlist_modules"
SUBTASKS = "netlist_subtasks"


def declare_planned(rounds: int) -> DAG:
    module, selection = Input("module"), Input("selection")
    evidence = Input("knowledge", "for_agents", default={})
    guidance = From("guidance", default={})
    plan = From("plan_gate", "plan")
    nodes = [
        Node(
            "guidance", ReferenceGuidanceAgent, optional=True,
            inputs={"module": module, "selection": selection, "requirement_docs": Input("requirement_docs"), "knowledge_evidence": evidence},
        ),
        Node(
            "plan", NetlistPlannerAgent,
            inputs={
                "module": module, "selection": selection, "requirement_docs": Input("requirement_docs"),
                "module_decomposition": Input("decomposition"), "reference_guidance": guidance,
                "knowledge_evidence": evidence, "module_index": Input("index"), "module_total": Input("total"),
            },
        ),
        Node("plan_gate", plan_gate, inputs={"plan_payload": From("plan"), "knowledge": Input("knowledge")}),
        Node(
            "worker", NetlistWorkerAgent, each=From("plan_gate", "sub_tasks"), retries=2, semaphore=SUBTASKS,
            label=lambda task, i: str(task.get("id") or i + 1) if isinstance(task, Mapping) else str(i + 1),
            inputs={
                "module": module, "selection": selection, "plan": plan, "sub_task": Item(),
                "reference_guidance": guidance, "knowledge_evidence": evidence,
            },
        ),
        Node(
            "merge", NetlistMergerAgent, optional=True, fallback=merge_fallback,
            inputs={"module": module, "plan": plan, "sub_results": From("worker"), "reference_guidance": guidance, "knowledge_evidence": evidence},
        ),
        Node("merged", merged_view, inputs={"merge": From("merge"), "guidance": guidance}),
        Node("lint_0", NetlistLinterAgent, optional=True, fallback=lint_fallback, inputs={"module": module, "netlist_info": From("merged", "netlist_info")}),
    ]
    edges: list[Edge] = []
    previous_info = From("merged", "netlist_info")
    for k in range(1, rounds + 1):
        nodes += [
            Node(
                f"repair_{k}", NetlistRepairerAgent, optional=True,
                inputs={
                    "module": module, "plan": plan, "netlist_info": previous_info, "lint": From(f"lint_{k - 1}"),
                    "reference_guidance": guidance, "knowledge_evidence": evidence,
                },
            ),
            Node(f"repaired_{k}", repaired_view, inputs={"repair": From(f"repair_{k}")}),
            Node(f"lint_{k}", NetlistLinterAgent, optional=True, fallback=lint_fallback, inputs={"module": module, "netlist_info": From(f"repaired_{k}", "netlist_info")}),
        ]
        edges += [
            Edge(f"lint_{k - 1}", f"repair_{k}", when=succeeded(lambda o: not lint_ok(o)), label="lint failed"),
            Edge(f"repair_{k}", f"repaired_{k}", when=succeeded()),
            Edge(f"repaired_{k}", f"lint_{k}", when=succeeded()),
        ]
        previous_info = From(f"repaired_{k}", "netlist_info")
    final_inputs: dict[str, Any] = {"merged": From("merged"), "lint_0": From("lint_0"), "binding": From("plan_gate", "binding")}
    for k in range(1, rounds + 1):
        final_inputs[f"repaired_{k}"] = From(f"repaired_{k}", default=None)
        final_inputs[f"lint_{k}"] = From(f"lint_{k}", default=None)
    nodes.append(Node("finalize", partial(finalize_planned, rounds), inputs=final_inputs))
    return DAG(nodes, edges, outputs={"result": From("finalize")}, name="planned")


def declare_module(planned: DAG, prefetcher: NetlistKnowledgePrefetcher | None) -> DAG:
    return DAG(
        name="module",
        nodes=[
            Node("module_of", module_of, inputs={"selection": Input("selection"), "index": Input("index")}),
            Node("prefetch", partial(prefetch_knowledge, prefetcher), inputs={"module": From("module_of"), "selection": Input("selection")}),
            Node(
                "prebuild", NetlistPrebuilderAgent, optional=True,
                inputs={
                    "module": From("module_of"), "selection": Input("selection"),
                    "module_decomposition": Input("decomposition"), "knowledge_evidence": From("prefetch", "for_agents", default={}),
                },
            ),
            Node(
                "planned", planned,
                inputs={
                    "module": From("module_of"), "selection": Input("selection"), "requirement_docs": Input("requirement_docs"),
                    "decomposition": Input("decomposition"), "knowledge": From("prefetch"), "index": Input("index"), "total": Input("total"),
                },
            ),
            Node(
                "assemble", assemble_module,
                inputs={"module": From("module_of"), "prebuilt": From("prebuild", default=None), "planned": From("planned", default=None), "knowledge": From("prefetch")},
            ),
        ],
        edges=[Edge("prebuild", "planned", when=lambda o: not (o.done and prebuild_matched(o.output)), label="no template")],
        outputs={"result": From("assemble")},
    )


class NetlistStage(DAGStage):
    name = StageName.NETLIST.value

    def __init__(
        self,
        options: FlowOptions,
        *,
        knowledge_prefetcher: NetlistKnowledgePrefetcher | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.semaphores = {MODULES: options.netlist_concurrency, SUBTASKS: options.netlist_concurrency}
        self.dag = self.declare(options, knowledge_prefetcher)

    @classmethod
    def declare(cls, options: FlowOptions | None = None, prefetcher: NetlistKnowledgePrefetcher | None = None) -> DAG:
        options = options or FlowOptions()
        module = declare_module(declare_planned(options.netlist_repair_rounds), prefetcher)
        return DAG(
            name=cls.name,
            nodes=[
                Node(
                    "inputs", read_inputs,
                    inputs={
                        "generation_input": Input("generation_input"),
                        "user_input": Input("user_input", default=""),
                    },
                ),
                Node(
                    "module", module, each=From("inputs", "module_rows"), label=module_label, semaphore=MODULES,
                    fallback=failed_module,
                    inputs={
                        "selection": Item(), "index": ItemIndex(), "total": From("inputs", "total"),
                        "requirement_docs": From("inputs", "requirement_docs"), "decomposition": From("inputs", "decomposition"),
                    },
                ),
                Node(
                    "normalize", NetlistNormalizerAgent, optional=True,
                    inputs={"module_results": From("module"), "module_decomposition": From("inputs", "decomposition")},
                ),
                Node(
                    "finalize", finalize,
                    inputs={"results": From("module"), "normalized": From("normalize", default=None), "fail_on_lint": options.fail_on_lint},
                ),
                Node("common_devices", abstractize_common_devices, inputs={"results": From("finalize")}),
            ],
            outputs={"result": From("common_devices")},
        )


__all__ = ["NetlistStage"]

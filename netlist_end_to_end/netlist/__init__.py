"""The netlist stage: the DAG in ``stage.py``, its agents under ``agents/``."""

from ..roles import AgentRole
from .agents import NetlistLinterAgent, NetlistMergerAgent, NetlistNormalizerAgent, NetlistPlannerAgent, NetlistPrebuilderAgent, NetlistRepairerAgent, NetlistWorkerAgent, ReferenceGuidanceAgent
from .stage import NetlistStage
from .merge import MergedNetlist, merge_cleaned_netlists
from .scl import SCLResult, SCLSource, compile_scl

NETLIST_AGENT_TYPES = {
    AgentRole.NETLIST_PREBUILDER: NetlistPrebuilderAgent,
    AgentRole.REFERENCE_GUIDANCE: ReferenceGuidanceAgent,
    AgentRole.NETLIST_PLANNER: NetlistPlannerAgent,
    AgentRole.NETLIST_WORKER: NetlistWorkerAgent,
    AgentRole.NETLIST_MERGER: NetlistMergerAgent,
    AgentRole.NETLIST_LINTER: NetlistLinterAgent,
    AgentRole.NETLIST_REPAIRER: NetlistRepairerAgent,
    AgentRole.NETLIST_NORMALIZER: NetlistNormalizerAgent,
}

__all__ = [
    "SCLResult",
    "SCLSource",
    "compile_scl",
    "MergedNetlist",
    "merge_cleaned_netlists",
    "NetlistStage",
    "NETLIST_AGENT_TYPES",
    "NetlistLinterAgent",
    "NetlistMergerAgent",
    "NetlistNormalizerAgent",
    "NetlistPlannerAgent",
    "NetlistPrebuilderAgent",
    "NetlistRepairerAgent",
    "NetlistWorkerAgent",
    "ReferenceGuidanceAgent",
]

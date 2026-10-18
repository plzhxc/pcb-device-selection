"""The top-port stage: the DAG in ``stage.py``, its agents under ``agents/``."""

from ..roles import AgentRole
from .agents import TopPortAggregatorAgent, TopPortBoundaryAgent, TopPortLinterAgent, TopPortPairAgent, TopPortPlannerAgent, TopPortShardAgent
from .stage import TopPortStage

TOP_PORT_AGENT_TYPES = {
    AgentRole.TOP_PORT_BOUNDARY: TopPortBoundaryAgent,
    AgentRole.TOP_PORT_PLANNER: TopPortPlannerAgent,
    AgentRole.TOP_PORT_PAIR: TopPortPairAgent,
    AgentRole.TOP_PORT_SHARD: TopPortShardAgent,
    AgentRole.TOP_PORT_AGGREGATOR: TopPortAggregatorAgent,
    AgentRole.TOP_PORT_LINTER: TopPortLinterAgent,
}

__all__ = [
    "TopPortStage",
    "TOP_PORT_AGENT_TYPES",
    "TopPortAggregatorAgent",
    "TopPortBoundaryAgent",
    "TopPortLinterAgent",
    "TopPortPairAgent",
    "TopPortPlannerAgent",
    "TopPortShardAgent",
]

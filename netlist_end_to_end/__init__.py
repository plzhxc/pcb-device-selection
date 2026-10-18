"""PCBAgent Workflow, Stage, Agent, prompt, and business declarations.

Execution primitives and Trace lifecycle belong to :mod:`agent_framework`;
process configuration and operational commands belong to ``pcbagent_runtime``.

The active pipeline consumes explicit module/device input, generates local
netlists, and reconciles their top-level ports. Shared primitive support lives
in ``common_devices``.
"""

from .contracts import (
    FlowContractError,
    FlowOptions,
    PCBFlowResult,
    STAGE_ORDER,
    StageName,
)
from .factory import DEFAULT_AGENT_TYPES, AgentDescriptor, agent_manifest, build_agent_runtime
from .roles import AgentRole
from .tools import (
    BatchDeviceEvidenceTool,
    CatalogMilvusNetlistKnowledgePrefetcher,
    DatasheetMilvusTool,
    DeviceCatalogSQLTool,
    KnowledgeConfig,
    MilvusDatasheetProvider,
    NetlistKnowledgePrefetcher,
    create_batch_device_evidence_toolbox,
    create_device_resolver_toolbox,
    create_netlist_knowledge_prefetcher,
    knowledge_health,
)
from .generation_input import build_generation_input, generation_decomposition, validate_generation_input
from .workflow import PCBWorkflow, build_workflow

__all__ = [
    "AgentDescriptor",
    "AgentRole",
    "BatchDeviceEvidenceTool",
    "CatalogMilvusNetlistKnowledgePrefetcher",
    "DEFAULT_AGENT_TYPES",
    "DatasheetMilvusTool",
    "DeviceCatalogSQLTool",
    "FlowContractError",
    "FlowOptions",
    "KnowledgeConfig",
    "MilvusDatasheetProvider",
    "NetlistKnowledgePrefetcher",
    "PCBFlowResult",
    "PCBWorkflow",
    "STAGE_ORDER",
    "StageName",
    "agent_manifest",
    "build_agent_runtime",
    "build_workflow",
    "build_generation_input",
    "generation_decomposition",
    "validate_generation_input",
    "create_batch_device_evidence_toolbox",
    "create_device_resolver_toolbox",
    "create_netlist_knowledge_prefetcher",
    "knowledge_health",
]

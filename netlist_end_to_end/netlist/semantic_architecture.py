"""Compatibility entry points for bounded architecture and peripheral planning."""

from .semantic_architecture_agents import (
    CANDIDATE_PROMPT as CANDIDATE_PROMPT,
    OBLIGATION_PROMPT as OBLIGATION_PROMPT,
    SemanticArchitectureAgent as SemanticArchitectureAgent,
    SemanticArchitectureBoundaryAgent as SemanticArchitectureBoundaryAgent,
    SemanticArchitectureInventoryRepairAgent as SemanticArchitectureInventoryRepairAgent,
    _architecture_repair_focus as _architecture_repair_focus,
)
from .semantic_architecture_context import (
    _knowledge_summary as _knowledge_summary,
    _source_subset as _source_subset,
)
from .semantic_architecture_planning import (
    plan_with_architecture as plan_with_architecture,
    _plan_architecture_attempt as _plan_architecture_attempt,
)
from .semantic_architecture_reconciliation import (
    _RECONCILIATION_CONSTRAINTS as _RECONCILIATION_CONSTRAINTS,
    _PROJECTED_RECONCILIATION_CONSTRAINTS as _PROJECTED_RECONCILIATION_CONSTRAINTS,
    final_port_reconciliation as final_port_reconciliation,
    _boundary_reconciliation as _boundary_reconciliation,
    _reconciliation_invariants as _reconciliation_invariants,
)
from .semantic_architecture_validation import (
    ARCH_MODULE as ARCH_MODULE,
    ARCHITECTURE_SCHEMA as ARCHITECTURE_SCHEMA,
    validate_architecture as validate_architecture,
    architecture_inventory_repair_targets as architecture_inventory_repair_targets,
    merge_architecture_inventory_repair as merge_architecture_inventory_repair,
    _error as _error,
    _source_reference_details as _source_reference_details,
    _domain_reference_details as _domain_reference_details,
    _boundary_errors as _boundary_errors,
)

__all__ = [
    "SemanticArchitectureAgent", "ARCHITECTURE_SCHEMA",
    "validate_architecture", "plan_with_architecture",
]

"""Public semantic generation entry points and compatibility imports.

Implementation is grouped into agents, inventory, planning, compilation and
bounded generation modules. Model instructions live in the prompts package.
"""

from .semantic_agents import (
    SemanticModuleAgent as SemanticModuleAgent,
    SemanticPeripheralAgent as SemanticPeripheralAgent,
    SemanticPlannerAgent as SemanticPlannerAgent,
    _peripheral_repair_focus as _peripheral_repair_focus,
)
from .semantic_compilation import (
    _compiled_pin_precheck as _compiled_pin_precheck,
    _declared_rail_requirements as _declared_rail_requirements,
    _pin_failure_feedback as _pin_failure_feedback,
    _rail_failure_feedback as _rail_failure_feedback,
    _top_port_diagnostics as _top_port_diagnostics,
)
from .semantic_generation import (
    _config as _config,
    _emit as _emit,
    generate_semantic_board as generate_semantic_board,
)
from .semantic_inventory import (
    _citation_refs as _citation_refs,
    _compact_contract as _compact_contract,
    _contracts as _contracts,
    _input_conditions as _input_conditions,
    _normal_quote as _normal_quote,
    _open_allowed as _open_allowed,
    _pin_nc_policy as _pin_nc_policy,
    _prepare as _prepare,
    _template_facts as _template_facts,
)
from .semantic_plan import (
    _freeze as _freeze,
    _normalize_connector_numbering as _normalize_connector_numbering,
    _normalize_contact_identity_maps as _normalize_contact_identity_maps,
    _normalize_passive_numbering as _normalize_passive_numbering,
)
from .semantic_shared import (
    MAX_COMMON_DEVICES as MAX_COMMON_DEVICES,
    MAX_CORE_INSTANCES as MAX_CORE_INSTANCES,
    MAX_MODULES as MAX_MODULES,
    MAX_REPAIRS as MAX_REPAIRS,
    _IDENTITY as _IDENTITY,
    _diagnostic as _diagnostic,
    _digest as _digest,
)

__all__ = ["generate_semantic_board", "SemanticPlannerAgent", "SemanticModuleAgent"]

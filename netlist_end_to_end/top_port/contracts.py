"""Schemas shared by top-port agents."""

from ..schemas import STRING_LIST, TOP_CONNECTION, object_schema

BOUNDARY_PORT = {
    "type": "object",
    "properties": {
        "module_id": {"type": "string", "minLength": 1},
        "net": {"type": "string", "minLength": 1},
        "direction": {
            "type": "string",
            "enum": ["input", "output", "inout"],
        },
        "reason": {"type": "string"},
    },
    "required": ["module_id", "net", "direction", "reason"],
    "additionalProperties": True,
}

BOUNDARY_SCHEMA = object_schema(
    {
        "pair_excluded_module_ids": {**STRING_LIST, "maxItems": 0},
        "system_inputs": {"type": "array", "items": {
            **BOUNDARY_PORT, "properties": {**BOUNDARY_PORT["properties"],
                "direction": {"type": "string", "enum": ["input", "inout"]}}}},
        "system_outputs": {"type": "array", "items": {
            **BOUNDARY_PORT, "properties": {**BOUNDARY_PORT["properties"],
                "direction": {"type": "string", "enum": ["output", "inout"]}}}},
        "reasons": STRING_LIST,
        "unmatched": {"type": "array", "items": {
            "type": "object", "properties": {
                "module_id": {"type": "string", "minLength": 1},
                "net": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1}},
            "required": ["module_id", "net", "reason"], "additionalProperties": True}},
    },
    required=(
        "pair_excluded_module_ids",
        "system_inputs",
        "system_outputs",
        "reasons",
        "unmatched",
    ),
)

PAIR_TASK = {
    "type": "object",
    "properties": {
        "source_module": {"type": "string", "minLength": 1},
        "target_module": {"type": "string", "minLength": 1},
        "dependency_type": {
            "type": "string",
            "enum": ["power", "clock", "signal", "protection", "other"],
        },
        "net_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_net": {"type": "string"},
                    "target_net": {"type": "string"},
                },
                "required": ["source_net", "target_net"],
                "additionalProperties": True,
            },
        },
        "hint": {"type": "string"},
    },
    "required": [
        "source_module",
        "target_module",
        "dependency_type",
        "net_candidates",
        "hint",
    ],
    "additionalProperties": True,
}

TOP_PLAN_SCHEMA = object_schema(
    {
        "pairs": {
            "type": "array",
            "items": PAIR_TASK,
            "minItems": 0,
        }
    },
    required=("pairs",),
)

PAIR_SCHEMA = object_schema(
    {
        "internal_connection": {
            "type": "array",
            "items": TOP_CONNECTION,
        },
        "unmatched": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
    required=("internal_connection", "unmatched"),
)

SHARD_CANDIDATE = {
    "type": "object",
    "properties": {
        "local": {"type": "string", "minLength": 1},
        "peer_module": {"type": "string", "minLength": 1},
        "peer_net": {"type": "string", "minLength": 1},
        "type": {
            "type": "string",
            "enum": ["power", "clock", "signal", "protection", "other"],
        },
        "role": {"type": "string", "enum": ["start", "end"]},
        "reason": {"type": "string"},
    },
    "required": ["local", "peer_module", "peer_net", "type", "role"],
    "additionalProperties": True,
}

EXPOSED_PORT = {
    "type": "object",
    "properties": {
        "local": {"type": "string", "minLength": 1},
        "direction": {
            "type": "string",
            "enum": ["input", "output", "inout"],
        },
        "guess": {
            "type": "string",
            "enum": ["system_input", "system_output", "unmatched"],
        },
        "reason": {"type": "string"},
    },
    "required": ["local", "direction", "guess", "reason"],
    "additionalProperties": True,
}

SHARD_SCHEMA = object_schema(
    {
        "internal_connection_candidates": {
            "type": "array",
            "items": SHARD_CANDIDATE,
        },
        "exposed_ports": {
            "type": "array",
            "items": EXPOSED_PORT,
        },
    },
    required=("internal_connection_candidates", "exposed_ports"),
)

__all__ = [
    "BOUNDARY_SCHEMA",
    "PAIR_SCHEMA",
    "SHARD_SCHEMA",
    "TOP_PLAN_SCHEMA",
]

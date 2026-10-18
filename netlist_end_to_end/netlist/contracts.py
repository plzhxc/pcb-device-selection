"""Schemas shared by netlist-stage agents."""

from ..schemas import (
    CROSS_PAGE_NET,
    NETLIST,
    NETLIST_INFO,
    STRING_LIST,
    object_schema,
)

GUIDANCE_SCHEMA = object_schema(
    {
        "guidance": {"type": "string", "minLength": 1},
        "topology_rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rule": {"type": "string"},
                    "source": {
                        "type": "string",
                        "enum": [
                            "provided_reference",
                            "provided_device_data",
                            "engineering_convention",
                        ],
                    },
                    "reason": {"type": "string"},
                },
                "required": ["rule", "source", "reason"],
                "additionalProperties": True,
            },
        },
        "cite_info": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
    required=("guidance", "topology_rules", "cite_info"),
)

MAIN_CHIP = {
    "type": ["object", "null"],
    "properties": {
        "designator": {"type": "string"},
        "device_name": {"type": "string"},
        "supplier_part": {"type": "string"},
        "device_type": {"type": "string"},
        "value": {"type": "string"},
        "note": {"type": "string"},
        "pins": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
    "additionalProperties": True,
}

SUB_TASK = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "role": {"type": "string", "minLength": 1},
        "context": {"type": "string"},
        "needed_nets": STRING_LIST,
        "expected_devices": {"type": "string"},
        "acceptance": STRING_LIST,
    },
    "required": [
        "id",
        "role",
        "context",
        "needed_nets",
        "expected_devices",
        "acceptance",
    ],
    "additionalProperties": True,
}

PLAN = {
    "type": "object",
    "properties": {
        "complexity": {
            "type": "string",
            "enum": ["simple", "complex"],
        },
        "main_chip": MAIN_CHIP,
        "sub_tasks": {
            "type": "array",
            "items": SUB_TASK,
            "minItems": 1,
        },
        "crossPageNet": CROSS_PAGE_NET,
        "design_rules": STRING_LIST,
        "notes": {"type": "string"},
    },
    "required": [
        "complexity",
        "main_chip",
        "sub_tasks",
        "crossPageNet",
        "design_rules",
        "notes",
    ],
    "additionalProperties": True,
}

PLANNER_SCHEMA = object_schema({"plan": PLAN}, required=("plan",))

WORKER_SCHEMA = object_schema(
    {
        # A connection-only task can legitimately add no physical part. The
        # final module still uses NETLIST_INFO and its nonempty constraint.
        "netlist": {**NETLIST, "minProperties": 0},
        "crossPageNet": CROSS_PAGE_NET,
        "notes": {"type": "string"},
    },
    required=("netlist", "crossPageNet", "notes"),
)

REPAIR_SCHEMA = object_schema(
    {"netlist_info": NETLIST_INFO},
    required=("netlist_info",),
)

__all__ = [
    "GUIDANCE_SCHEMA",
    "MAIN_CHIP",
    "PLAN",
    "PLANNER_SCHEMA",
    "REPAIR_SCHEMA",
    "SUB_TASK",
    "WORKER_SCHEMA",
]

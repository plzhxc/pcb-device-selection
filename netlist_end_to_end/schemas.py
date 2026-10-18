"""Independent JSON Schemas for PCBAgent Stage hand-offs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from agent_framework.structured_output import StructuredOutput


RESULT_PATTERN = (
    r"<{2,4}RESULT_START>{2,4}\s*(.*?)\s*"
    r"<{2,4}RESULT_END>{2,4}"
)


def json_text(value: Any) -> str:
    """Render Agent input and schema data as stable readable JSON."""

    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=str,
    )


def result_output(schema: Mapping[str, Any]) -> StructuredOutput:
    """Declare the standard Stage-facing ``result`` block."""

    return StructuredOutput(
        name="result",
        extract_pattern=RESULT_PATTERN,
        schema_def=dict(schema),
        required=True,
    )


STRING_LIST: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
}

DEVICE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "model": {"type": "string"},
        "manufacturer": {"type": "string"},
        "supplier_part": {"type": "string"},
        "role": {"type": "string"},
        "description": {"type": "string"},
        "verification_status": {
            "type": "string",
            "enum": ["confirmed", "unverified", "unknown"],
        },
        "evidence": {"type": "string"},
    },
    "required": ["name", "verification_status"],
    "additionalProperties": True,
}

CROSS_PAGE_NET: dict[str, Any] = {
    "type": "object",
    "properties": {
        "inputs": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "outputs": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
    },
    "required": ["inputs", "outputs"],
    "additionalProperties": True,
}

NETLIST_DEVICE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "props": {
            "type": "object",
            "properties": {
                "designator": {"type": "string", "minLength": 1},
                "deviceName": {"type": "string", "minLength": 1},
                "supplierPart": {"type": "string"},
                "value": {"type": "string"},
                "deviceType": {
                    "type": "string",
                    "enum": [
                        "resistance",
                        "capacitance",
                        "inductance",
                        "diode",
                        "tvs_diode",
                        "esd_protection",
                        "triode",
                        "mos",
                        "switch",
                        "oscillator",
                        "fuse",
                        "connector",
                        "chip",
                        "module",
                        "other",
                    ],
                },
                "deviceNotes": {"type": "string"},
            },
            "required": [
                "designator",
                "deviceName",
                "value",
                "deviceType",
                "deviceNotes",
            ],
            "additionalProperties": True,
        },
        "pins": {
            "type": "object",
            "additionalProperties": {"type": "string", "minLength": 1},
        },
    },
    "required": ["props", "pins"],
    "additionalProperties": True,
}

NETLIST: dict[str, Any] = {
    "type": "object",
    "minProperties": 1,
    "patternProperties": {
        r"^gge[0-9A-Za-z_-]+$": NETLIST_DEVICE,
    },
    "additionalProperties": False,
}

NETLIST_INFO: dict[str, Any] = {
    "type": "object",
    "properties": {
        "netlist": NETLIST,
        "crossPageNet": CROSS_PAGE_NET,
        "notes": {"type": "string"},
    },
    "required": ["netlist", "crossPageNet", "notes"],
    "additionalProperties": True,
}

LINT_ISSUE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "severity": {
            "type": "string",
            "enum": ["error", "warning", "info"],
        },
        "path": {"type": "string"},
        "message": {"type": "string"},
    },
    "required": ["code", "severity", "message"],
    "additionalProperties": True,
}

TOP_CONNECTION: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_module": {"type": "string", "minLength": 1},
        "source_net": {"type": "string", "minLength": 1},
        "target_module": {"type": "string", "minLength": 1},
        "target_net": {"type": "string", "minLength": 1},
        "type": {
            "type": "string",
            "enum": ["power", "clock", "signal", "protection", "other"],
        },
        "reason": {"type": "string"},
    },
    "required": [
        "source_module",
        "source_net",
        "target_module",
        "target_net",
        "type",
    ],
    "additionalProperties": True,
}


def object_schema(
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a permissive object schema with explicit required fields."""

    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": True,
    }


__all__ = [
    "CROSS_PAGE_NET",
    "DEVICE",
    "LINT_ISSUE",
    "NETLIST",
    "NETLIST_DEVICE",
    "NETLIST_INFO",
    "STRING_LIST",
    "TOP_CONNECTION",
    "RESULT_PATTERN",
    "json_text",
    "object_schema",
    "result_output",
]

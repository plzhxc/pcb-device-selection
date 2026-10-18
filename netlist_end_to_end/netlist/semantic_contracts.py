"""Generation contracts for a fixed BOM and connection-only SCL."""

from __future__ import annotations

from typing import Any

from .prompts import load_prompt

from validation_platform.pin_contracts import PAPER_ROLES, PROJECT_ROLES


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


STRING = {"type": "string", "minLength": 1}
STRINGS = {"type": "array", "items": STRING}
NULLABLE = {"type": ["string", "null"]}
PIN = obj({"id": STRING, "name": NULLABLE})
COMMON_TYPE = {"type": "string", "pattern": r"^[a-z][a-z0-9_]*(?![\s\S])",
               "not": {"enum": ["ic", "common_device"]},
               "description": "Concrete device category, e.g. capacitor or resistor; not a dotted template_id."}
COMMON = obj({"name": NULLABLE, "designator": STRING, "type": COMMON_TYPE,
              "is_abstract": {"const": True}, "value": NULLABLE,
              "description": NULLABLE, "id": {"type": "null"},
              "pins": {"type": "array", "items": PIN}})
from validation_platform.regulator_transfer import CAPACITOR_SELECTION_SCHEMA

COMMON_SPEC = obj({"device": COMMON, "source_refs": STRINGS, "reason": STRING,
                   "terminal_mapping": {"type": "object", "additionalProperties": STRING},
                   "parameters": {"type": "object", "additionalProperties": True,
                                  "properties": {"regulator_application": CAPACITOR_SELECTION_SCHEMA}}},
                  ["device", "source_refs", "reason"])
PORT = obj({"width": {"type": "integer", "minimum": 1}, "external": {"type": "boolean"},
            "purpose": STRING, "source_refs": STRINGS, "domain": NULLABLE,
            "domain_binding": {"enum": ["rail", "logic", None]},
            "external_source": obj({"source_id": STRING, "terminal": {"enum": ["positive", "return"]}})},
           ["width", "external", "purpose", "source_refs", "domain", "domain_binding"])
CONDITION_FACTS = {"type": "object", "additionalProperties": obj({
    "value": {"type": ["string", "number", "boolean", "null"]}, "source_refs": STRINGS})}
USAGE = obj({"device": STRING, "pin": STRING,
             "selected_role": {"enum": sorted(PAPER_ROLES | PROJECT_ROLES | {"unknown"})},
             "unconnected": {"type": "boolean"}, "source_refs": STRINGS, "reason": STRING,
             "selected_mode": NULLABLE, "capability": NULLABLE, "usage": NULLABLE,
             "signal_function": NULLABLE,
             "domain": NULLABLE, "resources": STRINGS,
             "configuration": CONDITION_FACTS, "requirements": CONDITION_FACTS},
            ["device", "pin", "selected_role", "unconnected", "source_refs", "reason", "capability", "selected_mode", "domain"])
SUPPLY_BINDING = obj({"device": STRING, "pin": STRING, "symbol": STRING,
                      "domain": STRING, "source_refs": STRINGS})
OPERATING_SELECTION = obj({"device": STRING, "option_id": STRING, "field": STRING,
                          "value": {"type": ["string", "boolean"]}, "source_refs": STRINGS, "reason": STRING})
MODULE = obj({"id": STRING, "label": STRING, "intent": STRING,
              "core_instances": STRINGS,
              "common_devices": {"type": "object", "additionalProperties": COMMON_SPEC},
              "ports": {"type": "object", "additionalProperties": PORT},
              "pin_usage": {"type": "array", "items": USAGE},
              "supply_bindings": {"type": "array", "items": SUPPLY_BINDING},
              "operating_selections": {"type": "array", "items": OPERATING_SELECTION}},
             ["id", "label", "intent", "core_instances", "common_devices", "ports", "pin_usage"])
DOMAIN = obj({"id": STRING, "voltage_v": {"type": ["number", "null"]}, "reference": NULLABLE,
              "source_refs": STRINGS, "reason": STRING, "profile_ref": STRING},
             ["id", "voltage_v", "reference", "source_refs", "reason"])
PLAN_SCHEMA = obj({"modules": {"type": "array", "items": MODULE, "minItems": 1},
                   "voltage_domains": {"type": "array", "items": DOMAIN},
                   "assumptions": STRINGS, "unresolved": STRINGS})

SCL_LANGUAGE = load_prompt('scl_language.md')

__all__ = ["PLAN_SCHEMA", "SCL_LANGUAGE"]

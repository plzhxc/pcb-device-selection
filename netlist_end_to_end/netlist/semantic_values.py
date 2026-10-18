"""Normalize redundant R/C/L and crystal nominal values from explicit SI parameters only."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
import math
from typing import Any

from .prompts import load_prompt

from jsonschema import Draft202012Validator
from validation_platform.electrical_values import normalized_value


def normalize_common_values(plan: dict, templates: list[dict], source_index: Mapping) -> tuple[list[dict], list[dict]]:
    """Update the working plan; raw agent output is owned by the caller.

    Only a unique built-in R/C/L or crystal primitive with an explicit finite numeric SI
    parameter supplies a missing scalar. Existing conflicting representations
    fail. Descriptions, designators, and reference circuits are never inputs.
    """
    records, diagnostics = [], []
    for module in plan["modules"]:
        for alias, specification in module["common_devices"].items():
            device = specification["device"]
            if device["type"] not in {"resistor", "capacitor", "inductor", "crystal"}:
                continue
            candidates = [template for template in templates if template["device_type"] == device["type"]
                          and template.get("value_spec")]
            if len(candidates) != 1:
                continue
            template = candidates[0]; value_spec = template["value_spec"]
            parameter = value_spec["parameter"]
            parameters = specification.get("parameters") or {}
            if parameter not in parameters:
                continue
            number = parameters[parameter]
            details: dict[str, Any] = {"severity": "error", "module": module["id"], "device": alias,
                                      "field": f"parameters.{parameter}"}
            if type(number) not in {int, float} or type(number) is float and not math.isfinite(number):
                diagnostics.append({**details, "code": "COMMON_TYPED_VALUE_INVALID",
                    "message": "explicit SI parameter must be a finite numeric value; booleans, strings and null are not values"})
                continue
            parameter_schema = template["parameters_schema"]
            extras = set(parameters) - set(parameter_schema.get("properties", {}))
            if parameter_schema.get("additionalProperties") is False and extras:
                diagnostics.append({**details, "code": "COMMON_TYPED_VALUE_SCHEMA",
                    "message": "unknown parameters cannot provide an alternative nominal value", "unknown_parameters": sorted(extras)})
                continue
            # Polarization, ratings and other required facts remain the full
            # common-contract checker's responsibility. Their absence must not
            # erase an independently explicit, valid nominal SI value.
            errors = list(Draft202012Validator(parameter_schema["properties"][parameter]).iter_errors(number))
            if errors:
                diagnostics.extend({**details, "code": "COMMON_TYPED_VALUE_SCHEMA", "message": error.message,
                                    "parameter_path": list(error.absolute_path)} for error in errors)
                continue
            scalar = str(number) + value_spec["unit"]
            dimension = normalized_value("1" + value_spec["unit"])[0]
            expected = (dimension, Decimal(str(number)))
            if device["value"] is not None:
                parsed = normalized_value(device["value"])
                if parsed != expected:
                    # Keep the shared parser strict. Unparsed text is not a
                    # numerical disagreement, and must not be silently replaced.
                    format_hint = ({"representation_issue": "unparsed_scalar_quantity",
                        "repair_hint": load_prompt("common_value_repair.md"),
                        "repair_field": "device.value", "required_unit": value_spec["unit"],
                        "same_parameter_scalar_example": scalar}
                        if parsed[0] == "text" else {})
                    diagnostics.append({**details, "code": "COMMON_VALUE_PARAMETER_CONFLICT",
                        "message": "device.value conflicts with the explicit typed SI parameter; resolve the source-backed value before SCL generation",
                        "scalar_value": device["value"], "parameter_value": number, "unit": value_spec["unit"],
                        **format_hint})
                continue
            if not specification["source_refs"] or any(ref not in source_index for ref in specification["source_refs"]):
                continue
            device["value"] = scalar
            records.append({"method": "typed_si_parameter_to_value", "module": module["id"], "device": alias,
                "field": "device.value", "before": None, "after": scalar,
                "parameter": parameter, "parameter_value": number, "unit": value_spec["unit"],
                "source_refs": deepcopy(specification["source_refs"]),
                "template": {"id": template["template_id"], "version": template["version"],
                             "sha256": template["definition_sha256"]}})
    return records, diagnostics


__all__ = ["normalize_common_values"]

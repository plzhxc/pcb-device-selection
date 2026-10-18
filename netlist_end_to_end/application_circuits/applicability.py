"""Deterministic candidate applicability against caller-frozen target fields.

Accepted digests identify a caller-selected candidate library, not human review
signatures. Input evidence below binds explicit Target fields to that target's
canonical bytes; it authenticates neither manufacturer truth nor user intent.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from validation_platform.conditional_pin_rules import evaluate_condition

from .models import Fragment, Target, canonical_digest


def _fresh(value: Fragment | Target) -> Fragment | Target:
    """Revalidate nested mutable lists/maps, even after valid model creation."""
    return type(value).model_validate(value.model_dump(mode="python"))


def _binding(target: Target) -> dict[str, Any]:
    target_sha = canonical_digest(target)
    result: dict[str, Any] = {}
    for namespace in ("configuration", "requirements"):
        result[namespace] = {}
        for field, value in getattr(target, namespace).items():
            result[namespace][field] = {
                "value": value,
                "evidence": [{
                    "source_kind": "input", "source_ref": target.id,
                    "document_sha256": target_sha, "locator": f"/{namespace}/{field}",
                    "quote": json.dumps({f"{namespace}.{field}": value}, ensure_ascii=False,
                                        sort_keys=True, allow_nan=False),
                }],
            }
    return result


def assess(fragment: Fragment, target: Target, *, accepted_digests: Mapping[str, str]) -> dict[str, Any]:
    """Assess identity, physical pins and sourced explicit condition selections.

    Only ``applicable`` authorizes fragment instantiation. ``false`` conditions
    mean this candidate is inapplicable; absent/unknown conditions or retained
    candidate uncertainties never authorize connections.
    """
    if not isinstance(fragment, Fragment) or not isinstance(target, Target):
        raise TypeError("assess requires validated Fragment and Target objects")
    fragment, target = _fresh(fragment), _fresh(target)
    fragment_sha = canonical_digest(fragment)
    result = {
        "status": "rejected", "reasons": [], "condition_result": None,
        "fragment_sha256": fragment_sha, "target_sha256": canonical_digest(target),
    }
    if not isinstance(accepted_digests, Mapping) or accepted_digests.get(fragment.id) != fragment_sha:
        result["reasons"] = ["Fragment ID/digest is absent from or differs from the caller-frozen candidate library"]
        return result
    different = [key for key in ("manufacturer", "model", "package")
                 if getattr(fragment.device, key) != getattr(target.device, key)]
    if different:
        result.update(status="inapplicable", reasons=[f"Exact device identity differs: {', '.join(different)}"])
        return result
    pin_errors = []
    for pin in fragment.pins:
        if pin.physical not in target.pin_catalog:
            pin_errors.append(f"Physical pin {pin.physical!r} is missing from the target catalog")
        elif target.pin_catalog[pin.physical] != pin.label:
            pin_errors.append(f"Physical pin {pin.physical!r} label differs from the target catalog")
    if pin_errors:
        result["reasons"] = pin_errors
        return result
    condition = evaluate_condition(fragment.when.model_dump(mode="json"), _binding(target))
    result["condition_result"] = condition
    if condition["status"] == "false":
        result.update(status="inapplicable", reasons=["At least one explicit target condition is false"])
    elif fragment.unresolved or condition["status"] != "true":
        result.update(status="unresolved", reasons=list(fragment.unresolved))
        if condition["status"] != "true":
            result["reasons"].append("At least one required target condition is absent or unresolved")
    else:
        result.update(status="applicable", reasons=[])
    return result


__all__ = ["assess"]

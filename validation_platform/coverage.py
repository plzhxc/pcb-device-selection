"""Formal three-metric comparison for generated vs reference netlists."""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from typing import Any, Dict, Optional

from validation_platform.connect_compare import (
    component_type_deviation,
    formal_connection_counts,
    formal_value_counts,
)
from validation_platform.graph import normalize_type, parse_value
from validation_platform.reference.models import ReferenceCapability


CapabilityLike = ReferenceCapability | Mapping[str, Any]


def formal_scale_thresholds(ref_device_count: int) -> Dict[str, Any]:
    if ref_device_count >= 600:
        return {
            "scale": "800",
            "component_quantity_deviation_max_ratio": 0.25,
            "value_accuracy_min": 0.75,
            "net_connection_accuracy_min": 0.75,
        }
    return {
        "scale": "400",
        "component_quantity_deviation_max_ratio": 0.20,
        "value_accuracy_min": 0.80,
        "net_connection_accuracy_min": 0.80,
    }


def compute_coverage(
    gen_netlist: Dict[str, Any],
    ref_netlist: Dict[str, Any],
    expected: Optional[Dict[str, float]] = None,
    *,
    capability: CapabilityLike | None = None,
) -> Dict[str, Any]:
    """Return legacy formal metrics, or a strict cleaned-format graph report.

    The public name is kept for existing callers, but the returned payload no
    longer includes legacy topology/connect-F1/value-distribution fields.
    Cleaned v1.2/v1.3 inputs return the explicitly versioned graph-comparison
    schema with topology, core-pin connections, peripheral circuits and values.
    They must not enter the legacy IC-path abstraction (which drops devices).
    """
    from validation_platform.netlist_graph import is_cleaned_netlist

    if is_cleaned_netlist(gen_netlist) or is_cleaned_netlist(ref_netlist):
        if expected:
            raise ValueError("cleaned graph comparison uses strict equality, not legacy acceptance thresholds")
        if capability is not None and not all(_has_capability(capability, key) for key in ("components", "values", "connectivity")):
            raise ValueError("cleaned graph comparison requires complete component, value and connectivity evidence")
        from validation_platform.netlist_compare import compare_cleaned_netlists

        return compare_cleaned_netlists(gen_netlist, ref_netlist)
    formal = compute_formal_metrics(
        gen_netlist,
        ref_netlist,
        expected=expected,
        capability=capability,
    )
    count = formal["component_count"]
    value = formal["component_value_accuracy"]
    net = formal["net_connection_accuracy"]
    value_multiset = formal.get("component_value_multiset")
    return {
        "formal_metrics": formal,
        "summary": {
            "formal_pass": formal["pass"],
            "formal_status": formal["formal_status"],
            "formal_component_quantity_deviation": count.get("type_abs_delta_sum"),
            "formal_component_quantity_deviation_pass": count.get("pass"),
            "formal_component_status": count["status"],
            "formal_value_accuracy_pct": value.get("accuracy_pct"),
            "formal_value_pass": value.get("pass"),
            "formal_value_status": value["status"],
            "formal_net_connection_accuracy_pct": net.get("accuracy_pct"),
            "formal_net_connection_pass": net.get("pass"),
            "formal_net_connection_status": net["status"],
            "formal_scale": formal["scale"],
            "component_value_multiset_accuracy_pct": (
                value_multiset.get("accuracy_pct") if value_multiset else None
            ),
            "component_value_multiset_pass": (
                value_multiset.get("pass") if value_multiset else None
            ),
            "component_value_multiset_status": (
                value_multiset.get("status") if value_multiset else None
            ),
        },
    }


def compute_formal_metrics(
    gen_netlist: Dict[str, Any],
    ref_netlist: Dict[str, Any],
    expected: Optional[Dict[str, float]] = None,
    *,
    capability: CapabilityLike | None = None,
) -> Dict[str, Any]:
    ref_count = len(ref_netlist or {})
    thresholds = _thresholds(ref_count, expected)

    has_components = _has_capability(capability, "components")
    has_values = _has_capability(capability, "values")
    has_connectivity = _has_capability(capability, "connectivity")

    count = (
        _as_evaluated(_component_count_metric(gen_netlist, ref_netlist, thresholds))
        if has_components
        else _skipped_metric(
            "sum(abs(generated IC/R/C/L type count - reference IC/R/C/L type count)); core IC identities must be complete",
            "SKIPPED_MISSING_COMPONENTS",
            ("components",),
        )
    )
    missing_value_capabilities = tuple(
        name
        for name, available in (
            ("components", has_components),
            ("values", has_values),
            ("connectivity", has_connectivity),
        )
        if not available
    )
    if missing_value_capabilities:
        value = _skipped_metric(
            "correct R/C/L values on stable core-IC/PWR/GND/NC endpoint relations; anonymous local and path-device endpoints remain diagnostic",
            _skip_reason(missing_value_capabilities),
            missing_value_capabilities,
        )
    else:
        value = _as_evaluated(_component_value_metric(gen_netlist, ref_netlist, thresholds))

    missing_net_capabilities = tuple(
        name
        for name, available in (
            ("components", has_components),
            ("connectivity", has_connectivity),
        )
        if not available
    )
    if missing_net_capabilities:
        net = _skipped_metric(
            "correct core-IC endpoint relations over direct or R/C/L paths; interchangeable D/Q/SW path topology remains diagnostic",
            _skip_reason(missing_net_capabilities),
            missing_net_capabilities,
        )
    else:
        net = _as_evaluated(_net_connection_metric(gen_netlist, ref_netlist, thresholds))

    required = (count, value, net)
    evaluable = all(metric["available"] for metric in required)
    payload: Dict[str, Any] = {
        "scale": thresholds["scale"],
        "thresholds": thresholds,
        "component_count": count,
        "component_value_accuracy": value,
        "net_connection_accuracy": net,
        "formal_status": "evaluated" if evaluable else "not_evaluable",
        "pass": (
            bool(count["pass"] and value["pass"] and net["pass"])
            if evaluable
            else None
        ),
    }
    # A BOM can still compare the multiset of R/C/L values, but that weaker
    # evidence must never be presented as the relational formal value metric.
    if has_components and has_values and not has_connectivity:
        payload["component_value_multiset"] = _component_value_multiset_metric(
            gen_netlist,
            ref_netlist,
            thresholds,
        )
    return payload


def _has_capability(capability: CapabilityLike | None, name: str) -> bool:
    """No capability argument is the legacy all-capabilities path."""

    if capability is None:
        return True
    if isinstance(capability, Mapping):
        return bool(capability.get(name))
    return bool(getattr(capability, name, False))


def _skip_reason(missing: tuple[str, ...]) -> str:
    for name in ("components", "values", "connectivity"):
        if name in missing:
            return f"SKIPPED_MISSING_{name.upper()}"
    return "SKIPPED_MISSING_CAPABILITY"


def _skipped_metric(
    definition: str,
    reason: str,
    missing_capabilities: tuple[str, ...],
) -> Dict[str, Any]:
    return {
        "definition": definition,
        "status": "skipped",
        "available": False,
        "pass": None,
        "reason": reason,
        "failure_reason": reason,
        "missing_capabilities": list(missing_capabilities),
    }


def _as_evaluated(metric: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **metric,
        "status": "evaluated",
        "available": True,
        "reason": "",
        "missing_capabilities": [],
    }


def _thresholds(ref_count: int, expected: Optional[Dict[str, float]]) -> Dict[str, Any]:
    thresholds = formal_scale_thresholds(ref_count)
    expected = expected or {}
    if expected.get("component_quantity_deviation_max_ratio") is not None:
        thresholds["component_quantity_deviation_max_ratio"] = float(expected["component_quantity_deviation_max_ratio"])
    elif expected.get("component_count_min_ratio") is not None and expected.get("component_count_max_ratio") is not None:
        min_ratio = float(expected["component_count_min_ratio"])
        max_ratio = float(expected["component_count_max_ratio"])
        thresholds["component_quantity_deviation_max_ratio"] = max(1.0 - min_ratio, max_ratio - 1.0)
    if expected.get("component_value_accuracy_min") is not None:
        thresholds["value_accuracy_min"] = float(expected["component_value_accuracy_min"])
    if expected.get("net_connection_accuracy_min") is not None:
        thresholds["net_connection_accuracy_min"] = float(expected["net_connection_accuracy_min"])
    if expected.get("scale"):
        thresholds["scale"] = str(expected["scale"])
    return thresholds


def _component_value_multiset_metric(
    gen_netlist: Dict[str, Any],
    ref_netlist: Dict[str, Any],
    thresholds: Dict[str, Any],
) -> Dict[str, Any]:
    """Weaker value-only evidence for references that have no connectivity.

    This deliberately lives beside, rather than inside,
    ``component_value_accuracy``.  Matching a type/value multiset cannot prove
    that a part with the right value is bound to the right pins or network.
    """

    generated = _passive_value_multiset(gen_netlist)
    reference = _passive_value_multiset(ref_netlist)
    ref_total = sum(reference.values())
    gen_total = sum(generated.values())
    if not ref_total:
        return _skipped_metric(
            "R/C/L type and normalized-value multiset match without endpoint binding",
            "SKIPPED_NO_REFERENCE_VALUES",
            (),
        ) | {
            "evaluation_mode": "component_value_multiset",
            "evidence_limit": (
                "No parseable reference R/C/L values were available; this metric cannot "
                "prove value accuracy or pin/network binding."
            ),
        }

    matched = sum(
        min(reference_count, generated.get(key, 0))
        for key, reference_count in reference.items()
    )
    min_accuracy = float(thresholds["value_accuracy_min"])
    accuracy = matched / ref_total
    missing = _counter_delta(reference, generated, label="missing")
    extra = _counter_delta(generated, reference, label="extra")
    return {
        "definition": "R/C/L type and normalized-value multiset match without endpoint binding",
        "evaluation_mode": "component_value_multiset",
        "status": "evaluated_weak_evidence",
        "available": True,
        "pass": accuracy >= min_accuracy,
        "reason": "",
        "failure_reason": (
            "component/value multiset is below threshold" if accuracy < min_accuracy else ""
        ),
        "accuracy": accuracy,
        "accuracy_pct": accuracy * 100,
        "min_accuracy": min_accuracy,
        "min_accuracy_pct": min_accuracy * 100,
        "matched": matched,
        "ref_total": ref_total,
        "gen_total": gen_total,
        "numerator": matched,
        "denominator": ref_total,
        "missing": missing,
        "extra": extra,
        "missing_capabilities": ["connectivity"],
        "evidence_limit": (
            "This weak metric compares only component type/value multiplicity and cannot "
            "prove that values are attached to the correct pins or networks."
        ),
    }


def _passive_value_multiset(netlist: Dict[str, Any]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for entry in (netlist or {}).values():
        props = entry.get("props") or {}
        kind = normalize_type(props)
        if kind not in {"R", "C", "L"}:
            continue
        value = _parse_passive_value(props, kind)
        if value is None or value <= 0:
            continue
        bucket = round(math.log10(value) * 2) / 2
        counter[f"{kind}@{bucket}"] += 1
    return counter


def _parse_passive_value(props: Dict[str, Any], kind: str) -> float | None:
    for raw in (
        str(props.get("value") or ""),
        str(props.get("deviceName") or props.get("DeviceName") or ""),
        str(props.get("deviceNotes") or ""),
    ):
        value = parse_value(raw, kind)
        if value is not None:
            return value
    return None


def _counter_delta(
    expected: Counter[str],
    actual: Counter[str],
    *,
    label: str,
) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    for key, expected_count in expected.items():
        actual_count = int(actual.get(key, 0))
        if actual_count >= expected_count:
            continue
        rows.append(
            {
                "key": key,
                "expected_count": int(expected_count),
                "actual_count": actual_count,
                label: int(expected_count - actual_count),
            }
        )
    return sorted(rows, key=lambda item: (-int(item[label]), str(item["key"])))[:40]


def _component_count_metric(
    gen_netlist: Dict[str, Any],
    ref_netlist: Dict[str, Any],
    thresholds: Dict[str, Any],
) -> Dict[str, Any]:
    deviation = component_type_deviation(gen_netlist, ref_netlist)
    ref_total = int(deviation.get("ref_total") or 0)
    max_ratio = float(thresholds["component_quantity_deviation_max_ratio"])
    max_delta = math.floor(ref_total * max_ratio) if ref_total else 0
    delta = int(deviation.get("type_abs_delta_sum") or 0)
    core_ok = bool(deviation.get("core_ic_complete"))
    gap = max(0, delta - max_delta)
    reason = ""
    if not core_ok:
        reason = "core IC identity multiset is incomplete"
    elif gap:
        reason = f"type absolute deviation is {gap} above allowed bound"
    passed = core_ok and delta <= max_delta
    return {
        "definition": "sum(abs(generated IC/R/C/L type count - reference IC/R/C/L type count)); core IC identities must be complete; path-device deltas remain diagnostic",
        "gen": int(deviation.get("gen_total") or 0),
        "ref": ref_total,
        "numerator": delta,
        "denominator": ref_total,
        "type_abs_delta_sum": delta,
        "deviation_ratio": delta / ref_total if ref_total else 0.0,
        "deviation_pct": (delta / ref_total * 100) if ref_total else 0.0,
        "max_deviation": max_delta,
        "max_deviation_ratio": max_ratio,
        "max_deviation_pct": max_ratio * 100,
        "gap": gap,
        "failure_reason": reason,
        "by_type": deviation.get("by_type") or {},
        "evaluated_types": deviation.get("evaluated_types") or [],
        "diagnostic_type_abs_delta_sum": int(
            deviation.get("diagnostic_type_abs_delta_sum") or 0
        ),
        "excluded_path_type_abs_delta_sum": int(
            deviation.get("excluded_path_type_abs_delta_sum") or 0
        ),
        "core_ic_complete": core_ok,
        "core_ic_missing": deviation.get("core_ic_missing") or [],
        "core_ic_extra": deviation.get("core_ic_extra") or [],
        "pass": passed,
    }


def _component_value_metric(
    gen_netlist: Dict[str, Any],
    ref_netlist: Dict[str, Any],
    thresholds: Dict[str, Any],
) -> Dict[str, Any]:
    counts = formal_value_counts(gen_netlist, ref_netlist)
    matched = int(counts.get("matched") or 0)
    ref_total = int(counts.get("ref_total") or 0)
    gen_total = int(counts.get("gen_total") or 0)
    accuracy = matched / ref_total if ref_total else 1.0
    min_accuracy = float(thresholds["value_accuracy_min"])
    gap = max(0, math.ceil(ref_total * min_accuracy) - matched) if ref_total else 0
    return {
        "definition": "correct R/C/L values on stable core-IC/PWR/GND/NC endpoint relations; anonymous local and path-device endpoints remain diagnostic",
        "accuracy": accuracy,
        "accuracy_pct": accuracy * 100,
        "min_accuracy": min_accuracy,
        "min_accuracy_pct": min_accuracy * 100,
        "matched": matched,
        "ref_total": ref_total,
        "gen_total": gen_total,
        "numerator": matched,
        "denominator": ref_total,
        "gap": gap,
        "failure_reason": f"need at least {gap} more value-matched components" if gap else "",
        "missing": counts.get("missing") or [],
        "extra": counts.get("extra") or [],
        "diagnostic_ref_total": int(counts.get("diagnostic_ref_total") or 0),
        "diagnostic_gen_total": int(counts.get("diagnostic_gen_total") or 0),
        "excluded_ref_total": int(counts.get("excluded_ref_total") or 0),
        "excluded_gen_total": int(counts.get("excluded_gen_total") or 0),
        "excluded_ref_by_reason": counts.get("excluded_ref_by_reason") or {},
        "excluded_gen_by_reason": counts.get("excluded_gen_by_reason") or {},
        "pass": accuracy >= min_accuracy,
    }


def _net_connection_metric(
    gen_netlist: Dict[str, Any],
    ref_netlist: Dict[str, Any],
    thresholds: Dict[str, Any],
) -> Dict[str, Any]:
    counts = formal_connection_counts(gen_netlist, ref_netlist)
    matched = int(counts.get("matched") or 0)
    ref_total = int(counts.get("ref_total") or 0)
    gen_total = int(counts.get("gen_total") or 0)
    accuracy = matched / ref_total if ref_total else 1.0
    min_accuracy = float(thresholds["net_connection_accuracy_min"])
    gap = max(0, math.ceil(ref_total * min_accuracy) - matched) if ref_total else 0
    return {
        "definition": "correct core-IC endpoint relations over direct or R/C/L paths; interchangeable D/Q/SW path topology remains diagnostic",
        "accuracy": accuracy,
        "accuracy_pct": accuracy * 100,
        "min_accuracy": min_accuracy,
        "min_accuracy_pct": min_accuracy * 100,
        "matched": matched,
        "ref_total": ref_total,
        "gen_total": gen_total,
        "numerator": matched,
        "denominator": ref_total,
        "gap": gap,
        "failure_reason": f"need at least {gap} more strict pin-connect matches" if gap else "",
        "missing": counts.get("missing") or [],
        "extra": counts.get("extra") or [],
        "diagnostic_ref_total": int(counts.get("diagnostic_ref_total") or 0),
        "diagnostic_gen_total": int(counts.get("diagnostic_gen_total") or 0),
        "excluded_ref_total": int(counts.get("excluded_ref_total") or 0),
        "excluded_gen_total": int(counts.get("excluded_gen_total") or 0),
        "excluded_ref_by_path_type": (
            counts.get("excluded_ref_by_path_type") or {}
        ),
        "excluded_gen_by_path_type": (
            counts.get("excluded_gen_by_path_type") or {}
        ),
        "pass": accuracy >= min_accuracy,
    }

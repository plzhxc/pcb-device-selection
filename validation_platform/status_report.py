"""Render a compact capability-aware comparison status report."""

from __future__ import annotations

from typing import Any


def render_evaluation_status(
    formal_metrics: dict[str, Any],
    reference_metadata: dict[str, Any] | None,
) -> str:
    reference_metadata = reference_metadata or {}
    provenance = reference_metadata.get("provenance") or {}
    capability = reference_metadata.get("capability") or {}
    lines = [
        "# Reference evaluation status",
        "",
        f"- Formal status: `{formal_metrics.get('formal_status', 'unknown')}`",
        f"- Formal pass: `{formal_metrics.get('pass')}`",
        f"- Reference format: `{provenance.get('format', 'flat_netlist_json')}`",
        f"- Reference capability: `{capability}`",
        "",
        "| Metric | Status | Available | Pass | Accuracy | Reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for key, label in (
        ("component_count", "component_count"),
        ("component_value_accuracy", "component_value_accuracy (endpoint-bound)"),
        ("net_connection_accuracy", "net_connection_accuracy"),
    ):
        metric = formal_metrics.get(key) or {}
        accuracy = metric.get("accuracy_pct")
        accuracy_text = f"{float(accuracy):.2f}%" if accuracy is not None else "—"
        lines.append(
            f"| {label} | {metric.get('status', 'unknown')} | "
            f"{metric.get('available')} | {metric.get('pass')} | {accuracy_text} | "
            f"{metric.get('reason') or '—'} |"
        )

    weak = formal_metrics.get("component_value_multiset")
    if weak:
        accuracy = weak.get("accuracy_pct")
        accuracy_text = f"{float(accuracy):.2f}%" if accuracy is not None else "—"
        lines.extend(
            [
                "",
                "## Weak evidence: component_value_multiset",
                "",
                f"- Status: `{weak.get('status')}`",
                f"- Pass against its value threshold: `{weak.get('pass')}`",
                f"- Accuracy: `{accuracy_text}`",
                f"- Evidence limit: {weak.get('evidence_limit')}",
                "- This result is not the endpoint-bound formal value metric and does not "
                "make the overall result evaluable.",
            ]
        )

    warnings = reference_metadata.get("warnings") or []
    if warnings:
        lines.extend(["", "## Reference warnings", ""])
        lines.extend(f"- `{warning}`" for warning in warnings)
    return "\n".join(lines) + "\n"


__all__ = ["render_evaluation_status"]

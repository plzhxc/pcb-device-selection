"""Shared validator helpers and the cross-stage DeviceEvidenceValidator.

These validators deliberately use only the new application contracts.  They
catch invariants JSON Schema cannot express and feed actionable errors back to
``BaseAgent`` for correction.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from agent_framework.agent import (
    ValidationContext,
    ValidationIssue,
    Validator,
)


def mapping_of(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def result_of(ctx: ValidationContext) -> dict[str, Any]:
    values = mapping_of(ctx.values)
    nested = values.get("result")
    return mapping_of(nested) if isinstance(nested, Mapping) else values


class DeviceEvidenceValidator(Validator):
    """Prevent unverified suggestions from carrying invented supplier facts."""

    name = "device_evidence"

    def validate(
        self,
        ctx: ValidationContext,
    ) -> Sequence[ValidationIssue]:
        payload = result_of(ctx)
        devices: list[tuple[str, Mapping[str, Any]]] = []
        for index, value in enumerate(payload.get("resolved_devices") or []):
            if isinstance(value, Mapping):
                devices.append((f"resolved_devices[{index}]", value))
        for index, value in enumerate(payload.get("selected_devices") or []):
            if not isinstance(value, Mapping):
                continue
            device = value.get("device")
            if isinstance(device, Mapping):
                devices.append((f"selected_devices[{index}].device", device))

        issues: list[ValidationIssue] = []
        for path, device in devices:
            status = str(device.get("verification_status") or "")
            supplier_part = str(device.get("supplier_part") or "").strip()
            if status == "unverified" and supplier_part:
                issues.append(
                    ValidationIssue(
                        code="unverified_supplier_part",
                        path=f"{path}.supplier_part",
                        message=(
                            "未验证器件不能携带供应商料号；清空 supplier_part，"
                            "或仅在输入有明确证据时改为 confirmed。"
                        ),
                    )
                )
            if status == "confirmed" and not str(
                device.get("evidence") or ""
            ).strip():
                issues.append(
                    ValidationIssue(
                        code="confirmed_without_evidence",
                        path=f"{path}.evidence",
                        severity="warning",
                        message="confirmed 器件应说明确认依据。",
                    )
                )
        return issues


__all__ = ["DeviceEvidenceValidator", "mapping_of", "result_of"]

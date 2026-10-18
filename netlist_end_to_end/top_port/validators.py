"""Validators for the top-port stage agents."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_framework.agent import ValidationContext, ValidationIssue, Validator

from ..validators import result_of


def _ports(cross_by_id: Mapping[str, Any]) -> set[tuple[str, str]]:
    return {(module, net) for module, entry in cross_by_id.items()
            for group in ("inputs", "outputs")
            for net in (entry.get("crossPageNet") or {}).get(group, [])}


class BoundaryClassificationValidator(Validator):
    """Repair contradictory boundary claims before deterministic aggregation."""

    name = "boundary_classification"

    def __init__(self, cross_by_id: Mapping[str, Any], existing: Mapping[str, Any] | None = None) -> None:
        self.ports = _ports(cross_by_id)
        self.existing = existing or {}

    def validate(self, ctx: ValidationContext) -> Sequence[ValidationIssue]:
        payload, seen, issues = result_of(ctx), {}, []
        covered = set()
        for row in self.existing.get("internal_connection") or []:
            if isinstance(row, Mapping):
                for side in ("source", "target"):
                    port = (row.get(side + "_module"), row.get(side + "_net"))
                    if port in self.ports:
                        covered.add(port)
        for key, expected in (("system_inputs", "input"), ("system_outputs", "output")):
            rows = [*(self.existing.get(key[:-1]) or []), *(payload.get(key) or [])]
            for index, row in enumerate(rows):
                if not isinstance(row, Mapping):
                    continue
                port = (row.get("module_id"), row.get("net"))
                direction = row.get("direction")
                path = f"{key}[{index}]"
                if port not in self.ports:
                    issues.append(ValidationIssue(code="unknown_boundary_endpoint", path=path,
                        message="边界端点必须逐字存在于原始 cross_by_id。"))
                if direction not in (expected, "inout"):
                    issues.append(ValidationIssue(code="boundary_direction_conflict", path=path,
                        message=f"{key} 中 direction 必须是 {expected} 或 inout。"))
                classification = (key, direction)
                if port in seen and seen[port] != classification:
                    issues.append(ValidationIssue(code="conflicting_boundary_classification", path=path,
                        message="同一边界端点只能采用一种输入/输出分类；双向用 inout 并只放在一个列表。"))
                seen[port] = classification
                if port in self.ports:
                    covered.add(port)
        for row in payload.get("unmatched") or []:
            if isinstance(row, Mapping):
                port = (row.get("module_id"), row.get("net"))
                if port not in self.ports:
                    issues.append(ValidationIssue(code="unknown_unmatched_endpoint", path="unmatched",
                        message="未解决端点也必须逐字存在于 cross_by_id。"))
                else:
                    covered.add(port)
        for module, net in sorted(self.ports - covered):
            issues.append(ValidationIssue(code="unclassified_boundary_endpoint", path="boundary",
                message=f"{module}:{net} 尚无内部连接或边界分类。若既定设计说明明确它是当前子系统输入/输出，可分类且无需新增连接器；缺乏证据则放入 unmatched，禁止省略或仅凭网名猜测。"))
        return issues


class TopConnectionValidator(Validator):
    """Reject self-connections and duplicate pair results."""

    name = "top_connection"

    def __init__(self, cross_by_id: Mapping[str, Any] | None = None, pair: Mapping[str, Any] | None = None) -> None:
        self.ports = _ports(cross_by_id) if cross_by_id is not None else None
        self.pair = pair

    def validate(
        self,
        ctx: ValidationContext,
    ) -> Sequence[ValidationIssue]:
        payload = result_of(ctx)
        rows = payload.get("internal_connection") or []
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            return ()
        seen: set[tuple[str, str, str, str]] = set()
        issues: list[ValidationIssue] = []
        for index, raw in enumerate(rows):
            if not isinstance(raw, Mapping):
                continue
            key = (
                str(raw.get("source_module") or ""),
                str(raw.get("source_net") or ""),
                str(raw.get("target_module") or ""),
                str(raw.get("target_net") or ""),
            )
            if key[0] == key[2]:
                issues.append(
                    ValidationIssue(
                        code="same_module_connection",
                        path=f"internal_connection[{index}]",
                        message="顶层端口只能连接不同模块。",
                    )
                )
            identity = min(key, (key[2], key[3], key[0], key[1]))
            if identity in seen:
                issues.append(
                    ValidationIssue(
                        code="duplicate_top_connection",
                        path=f"internal_connection[{index}]",
                        message="该连接已经输出过一次。",
                    )
                )
            seen.add(identity)
            if self.ports is not None and ((key[0], key[1]) not in self.ports or (key[2], key[3]) not in self.ports):
                issues.append(ValidationIssue(code="unknown_endpoint", path=f"internal_connection[{index}]",
                    message="两端必须逐字存在于完整 cross_by_id；连接器端点也可参与内部连接。"))
            if self.pair and {key[0], key[2]} != {self.pair.get("source_module"), self.pair.get("target_module")}:
                issues.append(ValidationIssue(code="outside_pair", path=f"internal_connection[{index}]",
                    message="本次只允许连接指定 pair 的两个模块。"))
        if self.pair:
            unresolved = payload.get("unmatched") or []
            for index, candidate in enumerate(self.pair.get("net_candidates") or []):
                key = (self.pair["source_module"], candidate["source_net"], self.pair["target_module"], candidate["target_net"])
                identity = min(key, (key[2], key[3], key[0], key[1]))
                accounted = any(isinstance(row, Mapping) and
                    row.get("source_net") == key[1] and row.get("target_net") == key[3] for row in unresolved)
                if identity not in seen and not accounted:
                    issues.append(ValidationIssue(code="omitted_candidate", path=f"pair.net_candidates[{index}]",
                        message=f"候选 {key[0]}:{key[1]} → {key[2]}:{key[3]} 不得丢弃；核实后显式连接，证据不足则用 source_net/target_net 记录 unmatched。"))
        return issues


__all__ = ["BoundaryClassificationValidator", "TopConnectionValidator"]

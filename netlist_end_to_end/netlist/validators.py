"""Validators for the netlist stage agents."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from agent_framework.agent import ValidationContext, ValidationIssue, Validator

from ..validators import mapping_of, result_of
from ..tools.netlist_evidence.common import core_device_type
from .helpers import nonphysical_connection_device


class NetlistFragmentValidator(Validator):
    """Check worker-local connectivity before fragment merge."""

    name = "netlist_fragment"

    def validate(
        self,
        ctx: ValidationContext,
    ) -> Sequence[ValidationIssue]:
        payload = result_of(ctx)
        netlist = mapping_of(payload.get("netlist"))
        cross = mapping_of(payload.get("crossPageNet"))
        nets: Counter[str] = Counter()
        designators: list[str] = []
        issues: list[ValidationIssue] = []

        for key, raw_device in netlist.items():
            device = mapping_of(raw_device)
            props = mapping_of(device.get("props"))
            pins = mapping_of(device.get("pins"))
            designators.append(str(props.get("designator") or ""))
            device_type = str(props.get("deviceType") or "")
            if nonphysical_connection_device(props):
                issues.append(ValidationIssue(
                    code="nonphysical_connection_device",
                    path=f"netlist.{key}",
                    message="不能用虚构器件表示导线；无器件子任务返回空网表，直连由计划中的同一网络表示。",
                ))
            for pin, raw_net in pins.items():
                net = str(raw_net or "")
                if net.upper() == "NC" and not core_device_type(device_type):
                    issues.append(
                        ValidationIssue(
                            code="non_ic_nc",
                            path=f"netlist.{key}.pins.{pin}",
                            message="非 IC 器件不能连接 NC。",
                        )
                    )
                elif net:
                    nets[net] += 1

        duplicates = [
            item
            for item, count in Counter(designators).items()
            if item and count > 1
        ]
        if duplicates:
            issues.append(
                ValidationIssue(
                    code="duplicate_designator",
                    path="netlist",
                    message=f"位号重复：{', '.join(sorted(duplicates))}。",
                )
            )
        for direction in ("inputs", "outputs"):
            rows = cross.get(direction) or []
            if not isinstance(rows, Sequence) or isinstance(
                rows, (str, bytes)
            ):
                continue
            for index, raw_net in enumerate(rows):
                net = str(raw_net)
                if net not in nets:
                    issues.append(
                        ValidationIssue(
                            code="unused_cross_page_net",
                            path=f"crossPageNet.{direction}[{index}]",
                            severity="warning",
                            message=(
                                f"当前 worker 片段未使用跨页网络 {net!r}；"
                                "该网络将在片段合并后的模块级 lint 中统一核验。"
                            ),
                        )
                    )
        return issues


__all__ = ["NetlistFragmentValidator"]

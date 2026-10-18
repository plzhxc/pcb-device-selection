"""Agent roles: the closed vocabulary shared by agent classes, trace and the console."""

from __future__ import annotations

from enum import Enum


class AgentRole(str, Enum):
    # Netlist generation
    NETLIST_PREBUILDER = "netlist_prebuilder"
    REFERENCE_GUIDANCE = "reference_guidance"
    NETLIST_PLANNER = "netlist_planner"
    NETLIST_WORKER = "netlist_worker"
    NETLIST_MERGER = "netlist_merger"
    NETLIST_LINTER = "netlist_linter"
    NETLIST_REPAIRER = "netlist_repairer"
    NETLIST_NORMALIZER = "netlist_normalizer"

    # Top-port reconciliation
    TOP_PORT_BOUNDARY = "top_port_boundary"
    TOP_PORT_PLANNER = "top_port_planner"
    TOP_PORT_PAIR = "top_port_pair"
    TOP_PORT_SHARD = "top_port_shard"
    TOP_PORT_AGGREGATOR = "top_port_aggregator"
    TOP_PORT_LINTER = "top_port_linter"

    @classmethod
    def parse(cls, value: "AgentRole | str") -> "AgentRole":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError as exc:
            raise ValueError(f"unknown Agent role: {value!r}") from exc


__all__ = ["AgentRole"]

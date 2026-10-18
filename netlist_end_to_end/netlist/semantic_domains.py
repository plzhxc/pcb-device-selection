"""Materialize frozen voltage metadata as auditable, connection-free SCL.

The planner's immutable domain table is already a semantic declaration. This
adapter serializes that declaration once; agent source text and every actual
wire remain unchanged. The ordinary SCL compiler validates all declarations,
including repeated agent definitions, reference cycles and conflicting values.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import math

from .scl import SCLSource


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def port_domain_selection_valid(port: Mapping, domains: Mapping) -> bool:
    """Require a complete declared port context; do not infer its binding or wire."""
    domain, binding = port.get("domain"), port.get("domain_binding")
    return ((domain is None and binding is None)
            or isinstance(domain, str) and domain in domains and binding in {"rail", "logic"})


def external_source_port_valid(port: Mapping, environment: Mapping | None) -> bool:
    """Check an optional source annotation against the explicit frozen input.

    An absent annotation is not a source guarantee. This predicate selects no
    source, return, domain or connection and does not infer profiles from prose.
    """
    if "external_source" not in port:
        return True
    binding = port["external_source"]
    sources = environment.get("sources", {}) if isinstance(environment, Mapping) else {}
    return (isinstance(binding, Mapping) and port.get("external") is True
            and port.get("domain_binding") == "rail" and port.get("width") == 1
            and binding.get("terminal") in {"positive", "return"}
            and binding.get("source_id") in sources)


def materialize_voltage_domains(plan: Mapping, board_id: str) -> tuple[SCLSource | None, dict]:
    """Serialize only frozen domain literals; never introduce a wire or alias.

This function does not authenticate manufacturer facts or decide whether a
voltage is appropriate. Planner validation and downstream source/electrical
checks keep those responsibilities. Malformed metadata is not repaired here.
"""
    if not isinstance(plan, Mapping) or not isinstance(board_id, str) or not board_id:
        raise ValueError("A frozen plan object and nonempty board ID are required")
    domains = plan.get("voltage_domains")
    if not isinstance(domains, list):
        raise ValueError("Frozen voltage_domains must be an array")
    definitions, provenance, seen = [], [], set()
    for domain in domains:
        if not isinstance(domain, Mapping) or not all(k in domain for k in ("id", "voltage_v", "reference")):
            raise ValueError("Each frozen domain requires id, voltage_v and reference")
        did, value, reference = (domain[k] for k in ("id", "voltage_v", "reference"))
        if not isinstance(did, str) or not did.strip() or "\x00" in did or did in seen:
            raise ValueError("Frozen domain IDs must be nonempty, unique strings without NUL")
        if value is not None and (type(value) not in (int, float) or type(value) is float and not math.isfinite(value)):
            raise ValueError("Frozen domain voltage must be a finite JSON number or null")
        if reference is not None and (not isinstance(reference, str) or not reference.strip() or "\x00" in reference):
            raise ValueError("Frozen reference must be a nonempty domain ID or null")
        seen.add(did)
        from validation_platform.static_voltages import domain_definition
        definitions.append(domain_definition(domain))
        provenance.append({**definitions[-1], "source_refs": deepcopy(domain.get("source_refs", [])),
                           "reason": deepcopy(domain.get("reason"))})
    definitions.sort(key=lambda row: row["id"])
    provenance.sort(key=lambda row: row["id"])
    lines = ["voltage_domain(" + repr(row["id"]) + ", voltage_v=" + repr(row["voltage_v"])
             + ", reference=" + repr(row["reference"])
             + (", profile_ref=" + repr(row["profile_ref"]) if "profile_ref" in row else "") + ")" for row in definitions]
    text = "\n".join(lines) + ("\n" if lines else "")
    source = SCLSource(f"{board_id}:frozen_voltage_domains", None, text) if lines else None
    record = {"schema_version": "pcbagent.scl.frozen_voltage_domains.v1",
              "authority": "frozen_semantic_plan", "frozen_plan_sha256": _digest(dict(plan)),
              "definitions_sha256": _digest(definitions), "definitions": definitions,
              "provenance": provenance, "source_name": source.name if source else None,
              "source_sha256": hashlib.sha256(text.encode()).hexdigest() if source else None,
              "operation_kinds": ["voltage_domain"] if source else [], "statement_count": len(lines),
              "agent_sources_modified": False, "wire_connections_added": False,
              "device_or_value_changes": False, "electrical_correctness_claimed": False}
    return source, record


__all__ = ["materialize_voltage_domains", "port_domain_selection_valid", "external_source_port_valid"]

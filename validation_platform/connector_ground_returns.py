"""Bounded evaluation projections for explicitly permitted spare ground returns.

Only an already selected private abstract connector is eligible. Ground is
proved through actual source-authenticated core terminals and zero-reference
supply bindings. The original graph and all metric denominators remain intact.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

from .certificate_facts import certificate_source_errors, digest, effective_gpio_fact
from .pin_contracts import component_contract, pin_binding
from .netlist_graph import NetNode
from .reference_binding import graph_binding_digest


def _map(value):
    return value if isinstance(value, Mapping) else {}


def _zero_domain(domain, net):
    value = domain.get("voltage_v")
    if isinstance(value, bool) or value is None or domain.get("reference") is not None or domain.get("net") != net[1]:
        return False
    try:
        number = Decimal(str(value))
        return number.is_finite() and number == 0
    except (InvalidOperation, ValueError):
        return False


def interface_ground(graph, contracts, connector):
    """Identify exactly one source-defined ground actually reaching the interface."""
    witnesses = {}; domain_ids = {}
    nets = {p.net for p in graph.pin_map(connector).values() if p.net and graph.connected_pins(p.key)}
    for net in sorted(nets):
        for peer in sorted(graph.nets[net].pins):
            pin = graph.pins[peer]; component = graph.components[pin.component]
            if not component.is_core:
                continue
            fact, errors = effective_gpio_fact(graph, contracts, pin.component[1], pin.id)
            if errors or fact.get("semantic_role") != "supply_gnd" or fact.get("electrical_type") != "ground":
                continue
            binding = pin_binding(contracts, pin.component[1], pin.id); did = binding.get("domain")
            domain = _map(_map(contracts.get("domains")).get(did))
            if not isinstance(did, str) or not _zero_domain(domain, net):
                continue
            cc = component_contract(contracts, pin.component[1]); supplies = []
            for symbol, raw in _map(cc.get("supplies")).items():
                supply = _map(raw)
                numbers = supply.get("pin_numbers") or [supply.get("pin")]
                # The binder separates manufacturer identity from operating
                # conditions. Legacy mixed lists stay strictly authenticated.
                identity_refs = supply.get("identity_evidence", supply.get("evidence"))
                if (isinstance(numbers, list) and pin.id in numbers and supply.get("domain") == did
                        and not certificate_source_errors(contracts, component.anchor_id,
                            identity_refs, ["pin_number", "pin_name"])):
                    supplies.append({"symbol": symbol, "pin_numbers": numbers, "domain": did,
                                     "identity_evidence": identity_refs,
                                     "binding_evidence": supply.get("binding_evidence"),
                                     "evidence": supply.get("evidence")})
            if not supplies:
                continue
            witnesses.setdefault(net, []).append({"component": pin.component[1], "supplier_part": component.anchor_id,
                "physical_pin": pin.id, "domain": did, "source_role": "supply_gnd",
                "role_evidence": _map(fact.get("field_evidence")).get("semantic_role", fact.get("evidence")),
                "supply_bindings": supplies})
            domain_ids.setdefault(net, set()).add(did)
    if len(witnesses) != 1 or any(len(ids) != 1 for ids in domain_ids.values()):
        return None, [], ["interface must expose one unambiguous source-authenticated zero-reference ground with an actual supply binding"]
    net = next(iter(witnesses))
    return net, witnesses[net], []


def apply_ground_projection(graph, operations):
    """Reconstruct a deterministic proof-only graph from verified incidences."""
    pins, nets = dict(graph.pins), dict(graph.nets)
    for operation in operations:
        key = (graph.source, operation["generated_component"], operation["generated_contact"])
        pin = pins[key]
        if list(pin.net or ()) != operation["before_net"]:
            raise ValueError("projected contact no longer has its verified actual net")
        old = nets[pin.net]
        remaining = old.pins - {key}
        if not remaining:
            raise ValueError("an additional ground return cannot be the entire source ground network")
        nets[pin.net] = replace(old, pins=remaining)
        new = None
        if operation["reference_unused_state"] == "singleton":
            new = (graph.source, "__spare_ground_projection__:" + key[1] + ":" + key[2])
            if new in nets:
                raise ValueError("projection singleton identifier conflicts with an existing net")
            nets[new] = NetNode(new, frozenset({key}))
        elif operation["reference_unused_state"] != "nc":
            raise ValueError("unsupported unused-contact state")
        pins[key] = replace(pin, net=new)
    return replace(graph, pins=MappingProxyType(pins), nets=MappingProxyType(nets))


def prove_ground_returns(reference, generated, rc, gc, rk, gk, pinmap, permission, claimed):
    """Recompute every allowed extra; candidate claims provide no source facts."""
    pairs = []
    for rp, gp in pinmap.items():
        rpin, gpin = reference.pin_map(rk)[rp], generated.pin_map(gk)[gp]
        if not reference.connected_pins(rpin.key) and generated.connected_pins(gpin.key):
            pairs.append({"reference_contact": rp, "generated_contact": gp})
    pairs.sort(key=lambda row: int(row["reference_contact"]))
    if not pairs:
        return generated, ([] if claimed in (None, []) else ["certificate claims ground returns absent from the actual graphs"]), {}
    policy = _map(permission.get("spare_contact_policy")); errors = []
    if (policy.get("mode") != "additional_ground_returns"
            or policy.get("ground_scope") != "unique_source_authenticated_interface_ground"
            or type(policy.get("max_contacts")) is not int or len(pairs) > policy["max_contacts"]):
        return generated, ["additional terminations lack an explicit bounded spare-ground policy"], {}
    if claimed != pairs:
        errors.append("certificate must list exactly every added contact under its complete bijection")
    rn, rw, re = interface_ground(reference, rc, rk); gn, gw, ge = interface_ground(generated, gc, gk)
    errors.extend(re + ge)
    if rn is None or gn is None:
        return generated, errors, {}
    ranchors = {(row["supplier_part"], row["physical_pin"]) for row in rw}
    ganchors = {(row["supplier_part"], row["physical_pin"]) for row in gw}
    if ranchors != ganchors:
        errors.append("additional returns do not use the same source-defined physical interface ground")
    rcc = component_contract(rc, rk[1]); operations = []
    rparams = _map(_map(_map(rcc.get("logical_terminal_contract")).get("voltage_obligation")).get("parameters"))
    for pair in pairs:
        rp, gp = pair["reference_contact"], pair["generated_contact"]
        rpin, gpin = reference.pin_map(rk)[rp], generated.pin_map(gk)[gp]
        fact = _map(_map(rcc.get("pins")).get(rp)); use = pin_binding(rc, rk[1], rp)
        if (fact.get("required_connection") is not False or fact.get("allow_nc") is not True
                or use.get("signal_function") not in (None, "")
                or _map(rparams.get("contact_usage")).get("contact_" + rp) not in (None, "spare")
                or "contact_" + rp in _map(rparams.get("contact_port_bindings"))):
            errors.append("a reference contact has an independent required interface function and cannot become an extra return")
        if gpin.net != gn:
            errors.append("additional contact must connect directly to the proved interface ground, never through a device or a different rail")
        operations.append({"reference_component": rk[1], **pair, "generated_component": gk[1],
            "before_net": list(gpin.net or ()), "reference_unused_state": "nc" if rpin.net is None else "singleton"})
    if errors:
        return generated, list(dict.fromkeys(errors)), {}
    try:
        projected = apply_ground_projection(generated, operations)
    except (KeyError, ValueError, TypeError) as exc:
        return generated, [str(exc)], {}
    proof = {"additional_ground_contacts": pairs, "projection_operations": operations,
        "reference_ground_network": list(rn), "generated_ground_network": list(gn),
        "source_ground_witnesses": {"reference": rw, "generated": gw},
        "projected_graph_sha256": graph_binding_digest(projected),
        "credit_exclusions": [{"metric": "connection", "generated_unit": [gk[1], row["generated_contact"]],
                               "reason": "additional_contact_has_no_reference_connected_unit"} for row in pairs],
        "original_graph_unchanged": True, "original_metric_denominators_preserved": True}
    proof["projection_sha256"] = digest({"operations": operations, "projected_graph_sha256": proof["projected_graph_sha256"]})
    return projected, [], proof


__all__ = ["interface_ground", "apply_ground_projection", "prove_ground_returns"]

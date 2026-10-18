"""Proof-only equivalence of null and isolated-net private spare contacts.

No physical edge is removed: only a net containing exactly its one authorized
abstract spare contact can be omitted from a proof copy. Original graphs,
source contracts, connected-unit inventories and metric denominators persist.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType

from .certificate_facts import digest
from .pin_contracts import component_contract, pin_binding
from .reference_binding import graph_binding_digest


def _map(value):
    return value if isinstance(value, Mapping) else {}


def apply_open_projection(graph, operations):
    """Replay independently verified singleton removals on one graph copy."""
    pins, nets = dict(graph.pins), dict(graph.nets)
    for row in operations:
        key = (graph.source, row["component"], row["contact"])
        pin = pins[key]; component = graph.components[pin.component]
        if (component.type != "connector" or component.is_abstract is not True or component.anchor_id is not None
                or pin.net is None or list(pin.net) != row["before_net"] or nets[pin.net].pins != frozenset({key})):
            raise ValueError("open representation projection requires exactly one private abstract contact in its verified net")
        del nets[pin.net]
        pins[key] = replace(pin, net=None)
    return replace(graph, pins=MappingProxyType(pins), nets=MappingProxyType(nets))


def prove_open_contacts(reference, generated, rc, gc, rk, gk, pinmap):
    """Normalize only matching electrically open, independently permitted spares.

The caller must first resolve the permitted private interface and authenticate
its complete uniform template. A reference singleton is observed connectivity,
not a historical intent claim; independent required functions still block it.
Generated spare status must come from its explicit frozen contact selection.
"""
    operations = {"reference": [], "generated": []}; pairs = []; errors = []
    for rp, gp in sorted(pinmap.items()):
        rpin, gpin = reference.pin_map(rk)[rp], generated.pin_map(gk)[gp]
        if reference.connected_pins(rpin.key) or generated.connected_pins(gpin.key):
            continue
        if rpin.net is None and gpin.net is None:
            continue
        for side, graph, bound, key, pid, pin in (
                ("reference", reference, rc, rk, rp, rpin), ("generated", generated, gc, gk, gp, gpin)):
            cc = component_contract(bound, key[1]); fact = _map(_map(cc.get("pins")).get(pid))
            use = pin_binding(bound, key[1], pid)
            params = _map(_map(_map(cc.get("logical_terminal_contract")).get("voltage_obligation")).get("parameters"))
            terminal = "contact_" + pid
            if (fact.get("required_connection") is not False or fact.get("allow_nc") is not True
                    or terminal in _map(params.get("contact_port_bindings"))
                    or _map(params.get("contact_usage")).get(terminal) not in (None, "spare")
                    or side == "reference" and use.get("signal_function") not in (None, "")
                    or side == "generated" and (_map(params.get("contact_usage")).get(terminal) != "spare"
                                                 or use.get("unconnected") is not True)):
                errors.append(side + " contact " + pid + " is not an independently permitted unassigned spare")
            if pin.net is not None:
                operations[side].append({"component": key[1], "contact": pid, "before_net": list(pin.net),
                                         "before_state": "singleton", "after_state": "nc"})
        pairs.append({"reference_contact": rp, "generated_contact": gp,
                      "reference_state": "nc" if rpin.net is None else "singleton",
                      "generated_state": "nc" if gpin.net is None else "singleton"})
    if errors or not pairs:
        return reference, generated, list(dict.fromkeys(errors)), {}
    try:
        rgraph = apply_open_projection(reference, operations["reference"])
        ggraph = apply_open_projection(generated, operations["generated"])
    except (ValueError, KeyError, TypeError) as exc:
        return reference, generated, [str(exc)], {}
    proof = {"open_contact_pairs": pairs, "open_projection_operations": operations,
             "open_projected_graph_sha256": {"reference": graph_binding_digest(rgraph), "generated": graph_binding_digest(ggraph)},
             "open_representation_credit": False, "original_graph_unchanged": True,
             "original_metric_denominators_preserved": True}
    proof["open_projection_sha256"] = digest(proof)
    return rgraph, ggraph, [], proof


__all__ = ["apply_open_projection", "prove_open_contacts"]

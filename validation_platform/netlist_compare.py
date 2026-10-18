"""Value-independent topology matching and core-pin peripheral subgraph checks.

Color refinement restricts candidate domains; it is NOT an isomorphism proof.
A bounded bijective search verifies every pin/net incidence before accepting
topology. Symmetric domains remain explicitly unresolved for per-instance
values unless all candidates give the same value result. Core components are
anchored by LCSC C IDs or project-defined S IDs, then matched outward by depth. Numeric values,
designators, gge numbers and peripheral supplier IDs never choose a mapping.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict, deque
from time import monotonic
from typing import Any, Mapping

from .electrical_values import compare_values, normalized_value
from .netlist_graph import (
    ComponentKey, NetKey, NetlistGraph, PinKey,
    extract_pin_subgraph, parse_netlist_graph,
)


def _core_id(graph: NetlistGraph, key: ComponentKey) -> str | None:
    component = graph.components[key]
    value = component.anchor_id
    return value if component.is_core and value and re.fullmatch(r"[CS][0-9]+", value) else None


def _label(graph: NetlistGraph, key: ComponentKey) -> tuple[Any, ...]:
    component = graph.components[key]
    if component.is_core:
        return "core_c_id", component.type, _core_id(graph, key), tuple(sorted(graph.pin_map(key)))
    return component.type, None if component.is_abstract is True else component.name, tuple(sorted(graph.pin_map(key)))


def _core_anchors(ref: NetlistGraph, gen: NetlistGraph) -> tuple[dict, dict]:
    """C/S IDs identify core part kinds; repeated instances remain separate."""
    groups = [defaultdict(list), defaultdict(list)]
    missing = [[], []]
    for side, graph in enumerate((ref, gen)):
        for key, component in sorted(graph.components.items()):
            if not component.is_core:
                continue
            cid = _core_id(graph, key)
            if cid is None:
                missing[side].append(_component_ref(graph, key))
            else:
                groups[side][cid].append(key)
    fixed = {}
    records = []
    mismatch = False
    for cid in sorted(groups[0].keys() | groups[1].keys()):
        a, b = groups[0][cid], groups[1][cid]
        mismatch |= len(a) != len(b)
        if len(a) == len(b) == 1:
            fixed[a[0]] = b[0]
        records.append({"c_id": cid, "reference": [_component_ref(ref, k) for k in a],
                        "generated": [_component_ref(gen, k) for k in b],
                        "status": "count_mismatch" if len(a) != len(b) else "unique_anchor" if len(a) == 1 else "repeated_instances"})
    complete = not any(missing)
    return fixed, {
        "status": "incomplete" if not complete else "different" if mismatch else "consistent" if records else "not_applicable",
        "equivalent": None if not complete else not mismatch,
        "groups": records, "unique_anchor_count": len(fixed),
        "missing_reference_c_ids": missing[0], "missing_generated_c_ids": missing[1],
    }


def _anchor_depths(graph: NetlistGraph) -> dict[ComponentKey, int]:
    """Multi-source expansion: C/S-ID cores at depth 0, adjacent devices at 1.

    Each component/net crossing adds one device layer. Nets are visited once;
    ownership traversal is structural, not an electrical-conduction claim.
    Components disconnected from every core anchor are left unlayered.
    """
    depths = {key: 0 for key in graph.components if _core_id(graph, key) is not None}
    todo = deque(sorted(depths))
    visited_nets = set()
    while todo:
        key = todo.popleft()
        for pin in graph.pin_map(key).values():
            if pin.net is None or pin.net in visited_nets:
                continue
            visited_nets.add(pin.net)
            for endpoint in sorted(graph.nets[pin.net].pins):
                neighbor = graph.pins[endpoint].component
                if neighbor not in depths:
                    depths[neighbor] = depths[key] + 1
                    todo.append(neighbor)
    return depths


def _layer_report(graph: NetlistGraph, depths: Mapping[ComponentKey, int]) -> dict[str, Any]:
    layers = defaultdict(list)
    for key, depth in sorted(depths.items()):
        layers[depth].append(_component_ref(graph, key))
    return {"layers": [{"depth": depth, "components": layers[depth]} for depth in sorted(layers)],
            "unanchored_components": [_component_ref(graph, key) for key in sorted(graph.components.keys() - depths.keys())]}


def _refine(ref: NetlistGraph, gen: NetlistGraph, fixed: Mapping[ComponentKey, ComponentKey],
            strict_net_names: bool, depths: tuple[dict, dict]) -> tuple[dict[ComponentKey, list[ComponentKey]], bool]:
    """Joint edge-labelled refinement of the compressed incidence graph."""
    labels = {}; adjacent = {}
    fixed_labels = [{key: index for index, key in enumerate(fixed)},
                    {key: index for index, key in enumerate(fixed.values())}]
    for side, graph in enumerate((ref, gen)):
        for key in graph.components:
            node = (side, "component", key)
            labels[node] = ("component", _label(graph, key), fixed_labels[side].get(key), depths[side].get(key))
            adjacent[node] = [(pin.id, (side, "net", pin.net)) for pin in graph.pin_map(key).values()
                              if pin.net is not None]
        for key, net in graph.nets.items():
            node = (side, "net", key)
            labels[node] = ("net", key[1] if strict_net_names else None)
            adjacent[node] = [(graph.pins[p].id, (side, "component", graph.pins[p].component))
                              for p in net.pins]
    def color(signatures):
        palette = {}
        return {node: palette.setdefault(signature, len(palette)) for node, signature in signatures.items()}
    colors = color(labels)
    for _ in range(32):
        refined = color({node: (colors[node], tuple(sorted((edge, colors[other]) for edge, other in edges)))
                         for node, edges in adjacent.items()})
        stable = len(set(refined.values())) == len(set(colors.values()))
        colors = refined
        if stable:
            break
    counts = [Counter(value for node, value in colors.items() if node[0] == side) for side in (0, 1)]
    groups = defaultdict(list)
    for key in gen.components:
        groups[colors[(1, "component", key)]].append(key)
    return {key: sorted(groups[colors[(0, "component", key)]]) for key in ref.components}, counts[0] == counts[1]


def _isomorphism(ref: NetlistGraph, gen: NetlistGraph, domains: dict[ComponentKey, list[ComponentKey]],
                 *, max_states: int, timeout_seconds: float, strict_net_names: bool,
                 depths: Mapping[ComponentKey, int]) -> dict[str, Any]:
    """Iterative backtracking over devices, enforcing a net bijection at each pin."""
    order = sorted(ref.components, key=lambda k: (depths.get(k, float("inf")), len(domains[k]), -len(ref.components[k].pins), k))
    reference_pins = {key: ref.pin_map(key) for key in ref.components}
    generated_pins = {key: gen.pin_map(key) for key in gen.components}
    mapping = {}; reverse = {}; net_map = {}; net_reverse = {}
    offsets = [0] * len(order)
    undo: list[list[tuple[NetKey, NetKey]]] = [[] for _ in order]
    depth = states = 0
    deadline = monotonic() + timeout_seconds
    while depth >= 0:
        if depth == len(order):
            return {"equivalent": True, "mapping": mapping, "net_mapping": net_map, "states": states,
                    "reason": "verified_pin_and_net_bijection"}
        if states >= max_states or monotonic() >= deadline:
            return {"equivalent": None, "mapping": {}, "net_mapping": {}, "states": states,
                    "reason": "search_limit"}
        key = order[depth]
        previous = mapping.pop(key, None)
        if previous is not None:
            reverse.pop(previous)
            for a, b in undo[depth]:
                net_map.pop(a); net_reverse.pop(b)
        advanced = False
        while offsets[depth] < len(domains[key]):
            candidate = domains[key][offsets[depth]]
            offsets[depth] += 1
            states += 1
            if states > max_states or monotonic() >= deadline:
                return {"equivalent": None, "mapping": {}, "net_mapping": {}, "states": states,
                        "reason": "search_limit"}
            if candidate in reverse:
                continue
            additions = {}; back = {}; valid = True
            for pin_id, pin in reference_pins[key].items():
                other = generated_pins[candidate][pin_id]
                a, b = pin.net, other.net
                if a is None or b is None:
                    if a != b:
                        valid = False; break
                    continue
                if (len(ref.nets[a].pins) != len(gen.nets[b].pins)
                        or (strict_net_names and a[1] != b[1])
                        or net_map.get(a, additions.get(a, b)) != b
                        or net_reverse.get(b, back.get(b, a)) != a):
                    valid = False; break
                if a not in net_map:
                    additions[a] = b; back[b] = a
            if not valid:
                continue
            mapping[key] = candidate; reverse[candidate] = key
            net_map.update(additions); net_reverse.update(back)
            undo[depth] = list(additions.items())
            depth += 1
            advanced = True
            break
        if not advanced:
            offsets[depth] = 0
            depth -= 1
    return {"equivalent": False, "mapping": {}, "net_mapping": {}, "states": states,
            "reason": "no_pin_and_net_bijection"}


def _diagnostic_mapping(ref: NetlistGraph, gen: NetlistGraph,
                        fixed: Mapping[ComponentKey, ComponentKey]) -> dict[ComponentKey, ComponentKey]:
    """Conservative local correspondences for explanations of non-equivalent graphs.

    These are diagnostic hypotheses, never substitutes for the exact proof.
    Start at fixed C-ID anchors, then expand through unique neighborhoods of
    already paired pins. Missing-ID cores are never inferred from their names.
    """
    mapping = dict(fixed)
    while True:
        reverse = {b: a for a, b in mapping.items()}
        buckets = [defaultdict(list), defaultdict(list)]
        for side, graph in enumerate((ref, gen)):
            for key in graph.components:
                if key in (mapping if side == 0 else reverse):
                    continue
                if graph.components[key].is_core and _core_id(graph, key) is None:
                    continue
                neighbors = []
                has_anchor = False
                for pin_id, pin in sorted(graph.pin_map(key).items()):
                    anchors = []
                    if pin.net is not None:
                        for endpoint in graph.nets[pin.net].pins:
                            owner = graph.pins[endpoint].component
                            anchor = owner if side == 0 and owner in mapping else reverse.get(owner) if side else None
                            if anchor is not None:
                                anchors.append((anchor, graph.pins[endpoint].id))
                                has_anchor = True
                    neighbors.append((pin_id, tuple(sorted(anchors))))
                if has_anchor:
                    buckets[side][(_label(graph, key), tuple(neighbors))].append(key)
        additions = {keys[0]: buckets[1][label][0] for label, keys in buckets[0].items()
                     if len(keys) == len(buckets[1].get(label, [])) == 1}
        if not additions:
            return mapping
        mapping.update(additions)


def _component_ref(graph: NetlistGraph, key: ComponentKey) -> dict[str, Any]:
    return {"source": key[0], "component": key[1], "designator": graph.components[key].designator,
            "id": graph.components[key].supplier_id,
            "identity_anchor": graph.components[key].identity_anchor}


def _pin_ref(graph: NetlistGraph, key: PinKey) -> dict[str, Any]:
    pin = graph.pins[key]
    return {**_component_ref(graph, pin.component), "pin": pin.id, "pin_name": pin.name,
            "net": pin.net[1] if pin.net is not None else None}


def _network_records(graph: NetlistGraph, identities: Mapping[ComponentKey, Any],
                     *, core_only: bool = False) -> dict[frozenset, NetKey]:
    records = {}
    for key, net in graph.nets.items():
        signature = frozenset((identities[graph.pins[p].component], graph.pins[p].id) for p in net.pins
                              if not core_only or graph.components[graph.pins[p].component].is_core)
        if not core_only or len(signature) >= 2:
            records[signature] = key
    return records


def _connection_diff(ref: NetlistGraph, gen: NetlistGraph, mapping: Mapping[ComponentKey, ComponentKey],
                     *, core_only: bool = False) -> dict[str, Any]:
    reverse = {b: a for a, b in mapping.items()}
    ref_ids = {k: ("reference", k) for k in ref.components}
    gen_ids = {k: ("reference", reverse[k]) if k in reverse else ("generated", k) for k in gen.components}
    rn = _network_records(ref, ref_ids, core_only=core_only)
    gn = _network_records(gen, gen_ids, core_only=core_only)
    def describe(graph, net):
        return {"net": net[1], "pins": [_pin_ref(graph, p) for p in sorted(graph.nets[net].pins)
                                        if not core_only or graph.components[graph.pins[p].component].is_core]}
    missing = [describe(ref, rn[s]) for s in rn.keys() - gn.keys()]
    extra = [describe(gen, gn[s]) for s in gn.keys() - rn.keys()]
    null_changes = []
    for a, b in sorted(mapping.items()):
        if core_only and not ref.components[a].is_core:
            continue
        rp, gp = ref.pin_map(a), gen.pin_map(b)
        for pin_id in sorted(rp.keys() & gp.keys()):
            x, y = rp[pin_id], gp[pin_id]
            if (x.net is None) != (y.net is None):
                null_changes.append({"reference": _pin_ref(ref, x.key), "generated": _pin_ref(gen, y.key)})
    def overlaps(source, target):
        index = defaultdict(set)
        for signature, net in target.items():
            for endpoint in signature:
                index[endpoint].add(net)
        changes = []
        for signature, net in source.items():
            hits = set().union(*(index[p] for p in signature))
            if len(hits) > 1:
                changes.append({"net": net[1], "other_nets": sorted(n[1] for n in hits)})
        return sorted(changes, key=lambda x: x["net"])
    return {
        "matched_nets": len(rn.keys() & gn.keys()), "reference_nets": len(rn), "generated_nets": len(gn),
        "renamed_nets": sorted([{"reference": rn[s][1], "generated": gn[s][1]}
                                for s in rn.keys() & gn.keys() if rn[s][1] != gn[s][1]],
                               key=lambda item: item["reference"]),
        "missing_nets": sorted(missing, key=lambda x: x["net"]),
        "extra_nets": sorted(extra, key=lambda x: x["net"]),
        "split_nets": overlaps(rn, gn), "merged_nets": overlaps(gn, rn),
        "unconnected_changes": null_changes,
    }


def _value_results(ref: NetlistGraph, gen: NetlistGraph,
                   domains: Mapping[ComponentKey, list[ComponentKey]]) -> dict[str, Any]:
    rows = []
    for key in sorted(ref.components):
        candidates = domains.get(key, [])
        reference = ref.components[key].value
        values = {normalized_value(gen.components[k].value) for k in candidates}
        row: dict[str, Any] = {"reference_component": _component_ref(ref, key), "candidate_count": len(candidates)}
        if not candidates:
            row.update(status="unresolved_component", equal=None, reference=reference)
        elif len(values) > 1:
            # Do not search for a value-favorable permutation of symmetric parts.
            outcomes = [compare_values(reference, gen.components[k].value) for k in candidates]
            if all(result["equal"] is False for result in outcomes):
                row.update(status="different_for_all_candidates", equal=False, reference=reference)
            else:
                row.update(status="ambiguous", equal=None, reference=reference)
        else:
            row.update(compare_values(reference, gen.components[candidates[0]].value))
        if len(candidates) == 1:
            row["generated_component"] = _component_ref(gen, candidates[0])
        elif candidates:
            row["candidate_components"] = [_component_ref(gen, k) for k in candidates]
        rows.append(row)
    unresolved = sum(row["equal"] is None and row["status"] != "not_applicable" for row in rows)
    wrong = sum(row["equal"] is False for row in rows)
    correct = sum(row["equal"] is True for row in rows)
    skipped = sum(row["status"] == "not_applicable" for row in rows)
    groups = defaultdict(list)
    for key, candidates in domains.items():
        if len(candidates) > 1:
            groups[tuple(candidates)].append(key)
    impossible_groups = []
    for candidates, keys in groups.items():
        # An inventory mismatch can DISPROVE consistency without selecting a
        # value-based assignment. Inventory equality cannot verify placement.
        if len(keys) == len(candidates) and Counter(normalized_value(ref.components[k].value) for k in keys) != Counter(
                normalized_value(gen.components[k].value) for k in candidates):
            impossible_groups.append({"reference": [_component_ref(ref, k) for k in sorted(keys)],
                                       "generated": [_component_ref(gen, k) for k in candidates]})
    return {"equivalent": False if wrong or impossible_groups else None if unresolved else True,
            "matched": correct, "different": wrong, "unresolved": unresolved, "not_applicable": skipped,
            "inconsistent_candidate_groups": impossible_groups,
            "reference_total": len(rows) - skipped,
            "accuracy_pct": 100 * correct / (len(rows) - skipped) if len(rows) > skipped else None,
            "components": rows}


def _circuit_reports(ref: NetlistGraph, gen: NetlistGraph, mapping: Mapping[ComponentKey, ComponentKey],
                     values: dict[str, Any], topology: bool | None, strict_net_names: bool) -> dict[str, Any]:
    """Compare whole peripheral regions, retaining every boundary and branch."""
    reverse = {b: a for a, b in mapping.items()}
    rids = {k: ("reference", k) for k in ref.components}
    gids = {k: ("reference", reverse[k]) if k in reverse else ("generated", k) for k in gen.components}
    def signature(graph, region, ids):
        networks = frozenset((n[1] if strict_net_names else None,
                               frozenset((ids[graph.pins[p].component], graph.pins[p].id)
                                         for p in graph.nets[n].pins)) for n in region.nets)
        devices = frozenset((ids[k], _label(graph, k), tuple(sorted(p.id for p in graph.pin_map(k).values()
                                                                  if p.net is None))) for k in region.components)
        return networks, devices
    gr = gen.peripheral_circuits()
    indexed = {signature(gen, circuit, gids): i for i, circuit in enumerate(gr)}
    rows = []; used = set()
    value_by_key = {(row["reference_component"]["source"], row["reference_component"]["component"]): row
                    for row in values["components"]}
    for circuit in ref.peripheral_circuits():
        match = indexed.get(signature(ref, circuit, rids))
        if match is not None:
            used.add(match)
        outcomes = [value_by_key[k] for k in circuit.components]
        inconsistent_group = any(
            {(item["source"], item["component"]) for item in group["reference"]} <= circuit.components
            for group in values["inconsistent_candidate_groups"]
        )
        certain = all(k in mapping for k in circuit.components) and all(ref.pins[p].component in mapping for p in circuit.boundary)
        rows.append({
            "boundary_pins": [_pin_ref(ref, p) for p in sorted(circuit.boundary)],
            "reference_components": [_component_ref(ref, k) for k in sorted(circuit.components)],
            "generated_components": [_component_ref(gen, k) for k in sorted(gr[match].components)] if match is not None else [],
            "topology": "equivalent" if match is not None else "different" if certain else "unresolved",
            "values": "different" if inconsistent_group or any(row["equal"] is False for row in outcomes) else
                      "unresolved" if any(row["equal"] is None and row["status"] != "not_applicable" for row in outcomes) else "consistent",
            "pairing_evidence": "verified_global_isomorphism" if topology is True else "diagnostic_partial_mapping",
        })
    return {"reference_regions": len(rows), "generated_regions": len(gr), "regions": rows,
            "unmatched_generated_regions": [{"components": [_component_ref(gen, k) for k in sorted(c.components)],
                                               "boundary_pins": [_pin_ref(gen, p) for p in sorted(c.boundary)]}
                                              for i, c in enumerate(gr) if i not in used]}


def compare_netlist_graphs(generated: NetlistGraph, reference: NetlistGraph, *,
                           component_map: Mapping[ComponentKey, ComponentKey] | None = None,
                           strict_net_names: bool = False, max_states: int = 100000,
                           timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Anchor cores by C/S ID, compare topology outward, then position-bound values.

    component_map, if supplied, maps reference keys to generated keys as hard
    anchors, but cannot override a conflicting core C/S ID. Missing core IDs
    leave identity confirmation unresolved. Search limits also mean unknown.
    """
    if max_states < 1 or timeout_seconds <= 0:
        raise ValueError("search limits must be positive")
    requested = dict(component_map or {})
    if (any(a not in reference.components or b not in generated.components for a, b in requested.items())
            or len(set(requested.values())) != len(requested)):
        raise ValueError("component_map must be a one-to-one reference-to-generated mapping of existing components")
    fixed, anchors = _core_anchors(reference, generated)
    for a, b in requested.items():
        if reference.components[a].is_core or generated.components[b].is_core:
            if _core_id(reference, a) is None or _core_id(reference, a) != _core_id(generated, b):
                raise ValueError("explicit core anchors must have the same valid C or S ID on both sides")
        if a in fixed and fixed[a] != b:
            raise ValueError("component_map conflicts with a unique C-ID anchor")
        fixed[a] = b
    if len(set(fixed.values())) != len(fixed):
        raise ValueError("component_map conflicts with C-ID anchor bijection")
    depths = (_anchor_depths(reference), _anchor_depths(generated))
    domains, compatible = ({}, False)
    if anchors["equivalent"] is True:
        domains, compatible = _refine(reference, generated, fixed, strict_net_names, depths)
    if anchors["equivalent"] is not True:
        search = {"equivalent": anchors["equivalent"], "mapping": {}, "net_mapping": {}, "states": 0,
                  "reason": "missing_core_c_id" if anchors["equivalent"] is None else "core_c_id_inventory_mismatch"}
    elif compatible:
        search = _isomorphism(reference, generated, domains, max_states=max_states,
                              timeout_seconds=timeout_seconds, strict_net_names=strict_net_names, depths=depths[0])
    else:
        search = {"equivalent": False, "mapping": {}, "net_mapping": {}, "states": 0,
                  "reason": "different_labelled_incidence_partitions"}
    equivalent = search["equivalent"]
    mapping = search["mapping"] if equivalent is True else _diagnostic_mapping(reference, generated, fixed)
    value_domains = domains if compatible and equivalent is not False else {a: [b] for a, b in mapping.items()}
    values = _value_results(reference, generated, value_domains)
    diff = _connection_diff(reference, generated, mapping)
    groups = defaultdict(list)
    if equivalent is True:
        for key, candidates in domains.items():
            if len(candidates) > 1:
                groups[tuple(candidates)].append(key)
    ambiguous = [{"reference": [_component_ref(reference, k) for k in sorted(keys)],
                  "generated_candidates": [_component_ref(generated, k) for k in candidates]}
                 for candidates, keys in groups.items()]
    unresolved_ref = [_component_ref(reference, k) for k in sorted(reference.components.keys() - mapping.keys())]
    unresolved_gen = [_component_ref(generated, k) for k in sorted(generated.components.keys() - set(mapping.values()))]
    overall = False if equivalent is False or (compatible and values["equivalent"] is False) else (
        True if equivalent is True and values["equivalent"] is True else None)
    annotation_differences = []
    for a, b in sorted(mapping.items()):
        x, y = reference.components[a], generated.components[b]
        if _core_id(reference, a) and _core_id(reference, a) == _core_id(generated, b) and (x.name, x.type) != (y.name, y.type):
            annotation_differences.append({"c_id": x.anchor_id,
                "reference": {**_component_ref(reference, a), "name": x.name, "type": x.type},
                "generated": {**_component_ref(generated, b), "name": y.name, "type": y.type}})
    return {
        "schema": "pcbagent.netlist_graph_comparison.v1", "pass": overall,
        "status": "equivalent" if overall is True else "different" if overall is False else "unresolved",
        "reference": reference.summary(), "generated": generated.summary(),
        "policy": {"matching": "core_c_id_anchors_then_depth_layers", "values_used_for_matching": False,
                   "core_identity": "exact_c_id", "peripheral_supplier_ids_used_for_matching": False,
                   "missing_core_c_ids": "unresolved_no_model_fallback",
                   "net_names": "strict" if strict_net_names else "ignored_for_matching",
                   "pin_permutations": "strict_pin_id", "passive_reduction": "none",
                   "ambiguity": "candidate_domains_are_conservative_not_enumerated_automorphisms",
                   "circuit_scope": "all_non_core_branches_stop_at_core_pins"},
        "topology": {"equivalent": equivalent, "reason": search["reason"], "search_states": search["states"],
                     "differences": diff, "difference_evidence": "exact" if equivalent is True else "diagnostic_partial_mapping"},
        "core_anchors": {**anchors, "annotation_differences": annotation_differences},
        "expansion": {"reference": _layer_report(reference, depths[0]), "generated": _layer_report(generated, depths[1])},
        "matching": {"unique_pairs": [{"reference": _component_ref(reference, a), "generated": _component_ref(generated, b)}
                                      for a, b in sorted(mapping.items()) if equivalent is not True or len(domains[a]) == 1],
                     "witness_pairs": [{"reference": _component_ref(reference, a), "generated": _component_ref(generated, b)}
                                       for a, b in sorted(mapping.items())] if equivalent is True else [],
                     "ambiguous_groups": ambiguous, "unresolved_reference": unresolved_ref, "unresolved_generated": unresolved_gen,
                     "evidence": "verified_isomorphism" if equivalent is True else "diagnostic_hypothesis"},
        "core_pin_connections": _connection_diff(reference, generated, mapping, core_only=True),
        "peripheral_circuits": _circuit_reports(reference, generated, mapping, values, equivalent, strict_net_names),
        "values": {**values, "evidence": "topology_candidate_domains" if compatible and equivalent is not False else "diagnostic_hypothesis"},
    }


def compare_cleaned_netlists(generated: dict[str, Any], reference: dict[str, Any], **options: Any) -> dict[str, Any]:
    return compare_netlist_graphs(parse_netlist_graph(generated, source="generated"),
                                  parse_netlist_graph(reference, source="reference"), **options)


def compare_pin_subgraphs(generated: NetlistGraph, reference: NetlistGraph, *,
                          generated_pins: tuple[PinKey, PinKey], reference_pins: tuple[PinKey, PinKey],
                          **options: Any) -> dict[str, Any]:
    """Compare two rooted pin-to-pin peripheral subgraphs, including shunts.

    Terminal order establishes the two external anchors. The interior device
    mapping still uses topology only. Other encountered core pins remain ports.
    """
    if len(generated_pins) != 2 or len(reference_pins) != 2:
        raise ValueError("exactly two ordered terminals are required on each side")
    fixed = {}
    for a, b in zip(reference_pins, generated_pins):
        if a not in reference.pins or b not in generated.pins:
            raise ValueError("terminal does not exist")
        if a[2] != b[2]:
            raise ValueError("corresponding terminal pin IDs must agree")
        if a[:2] in fixed and fixed[a[:2]] != b[:2]:
            raise ValueError("terminal component correspondence is inconsistent")
        fixed[a[:2]] = b[:2]
    result = compare_netlist_graphs(extract_pin_subgraph(generated, generated_pins),
                                    extract_pin_subgraph(reference, reference_pins), component_map=fixed, **options)
    result["terminals"] = {"reference": [_pin_ref(reference, p) for p in reference_pins],
                           "generated": [_pin_ref(generated, p) for p in generated_pins]}
    return result

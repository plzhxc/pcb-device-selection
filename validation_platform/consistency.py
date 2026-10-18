"""Raw and certificate-adjusted consistency with explicit, unhidden coverage.

Connection score is F1 over connected physical pins whose *complete* direct-wire
neighbour sets agree. NC and singleton nets never increase this score. Value
score is F1 over all non-null component values; unpaired parts remain in both
denominators. Structural candidate domains are fixed without values. If a full
topology witness exists, a second bounded search may choose a witness satisfying
all value equalities within those domains; every wire incidence is reverified.
This makes isomorphic graph-and-value self-comparisons exact while unresolved
partial comparisons retain their unpaired denominators.

The caller freezes input permissions independently of generated certificates:
permissions={schema:'pcbagent.equivalence_permissions.v1', input_sha256:..., rules:
 [{id, kind, reference_component, applies:True, evidence:[...], ...}], context:{}}.
GPIO rules add pin_ids and allowed_usages. Value-interval rules add minimum and
maximum quantities and conditions (matched exactly against context). Certificates
contain id, kind, permission_id, reference_component, generated_component, and,
for GPIO, pin_map mapping reference physical pin IDs to generated physical IDs.
GPIO maps must be closed bijections, prove identical selected capability, mode,
resource assignment and domain voltage, and never remap reserved/special pins.
No LLM explanation or an unrecognized certificate can award credit.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any

from .electrical_values import compare_values, normalized_value
from .connection_correspondence import extend_connection_correspondence
from .value_correspondence import extend_value_correspondence
from .netlist_compare import compare_netlist_graphs, _anchor_depths, _isomorphism
from .netlist_graph import ComponentKey, NetNode, NetlistGraph, PinKey
from .pin_contracts import (check_pin_contracts, component_contract, decimal_number,
                            evidence_errors, pin_binding, pin_fact, role_mode_compatible)


def _part(graph: NetlistGraph, key: ComponentKey) -> str | None:
    c = graph.components[key]
    return c.anchor_id if c.is_core and re.fullmatch(r"[CS][0-9]+", c.anchor_id or "") else None


def _label(graph: NetlistGraph, key: ComponentKey) -> tuple[Any, ...]:
    c = graph.components[key]
    return ("core", c.type, _part(graph, key), tuple(sorted(graph.pin_map(key)))) if c.is_core else (
        c.type, None if c.is_abstract is True else c.name, tuple(sorted(graph.pin_map(key))))


def _pair(reference: NetlistGraph, generated: NetlistGraph, *, max_states: int,
          timeout_seconds: float) -> tuple[dict, list, dict]:
    """Verified global witnesses, or unique anchored local structures."""
    report = compare_netlist_graphs(generated, reference, max_states=max_states, timeout_seconds=timeout_seconds)
    mapping: dict[ComponentKey, ComponentKey] = {}
    evidence = []
    by_part = [defaultdict(list), defaultdict(list)]
    for side, graph in enumerate((reference, generated)):
        for key in graph.components:
            if part := _part(graph, key):
                by_part[side][part].append(key)
    for part in sorted(by_part[0].keys() & by_part[1].keys()):
        left, right = by_part[0][part], by_part[1][part]
        if len(left) == len(right) == 1 and _label(reference, left[0]) == _label(generated, right[0]):
            mapping[left[0]] = right[0]
            evidence.append({"reference": left[0][1], "generated": right[0][1], "kind": "unique_core_identity", "part_id": part, "depth": 0})
    if report["topology"]["equivalent"] is True:
        domains = {}
        for pair in report["matching"]["unique_pairs"]:
            domains[(reference.source, pair["reference"]["component"])] = [(generated.source, pair["generated"]["component"])]
        for group in report["matching"]["ambiguous_groups"]:
            candidates = [(generated.source, item["component"]) for item in group["generated_candidates"]]
            for item in group["reference"]:
                domains[(reference.source, item["component"])] = candidates
        value_domains = {key: [candidate for candidate in choices if normalized_value(reference.components[key].value) ==
                              normalized_value(generated.components[candidate].value)] for key, choices in domains.items()}
        if len(value_domains) == len(reference.components) and all(value_domains.values()):
            value_search = _isomorphism(reference, generated, value_domains, max_states=max_states,
                                        timeout_seconds=timeout_seconds, strict_net_names=False,
                                        depths=_anchor_depths(reference))
        else:
            value_search = {"equivalent": False, "mapping": {}, "states": 0, "reason": "value_domain_inventory_mismatch"}
        report["value_witness_search"] = {k: value_search[k] for k in ("equivalent", "states", "reason")}
        witnesses = report["matching"]["witness_pairs"]
        if value_search["equivalent"] is True:
            witnesses = [{"reference": {"component": a[1]}, "generated": {"component": b[1]}}
                         for a, b in sorted(value_search["mapping"].items())]
        for pair in witnesses:
            left = (reference.source, pair["reference"]["component"])
            right = (generated.source, pair["generated"]["component"])
            if left not in mapping:
                mapping[left] = right
                evidence.append({"reference": left[1], "generated": right[1], "kind": "verified_global_bijection_witness",
                                 "all_values_equal_witness": value_search["equivalent"] is True})
    depth = 1
    while True:
        reverse = {b: a for a, b in mapping.items()}
        buckets = [defaultdict(list), defaultdict(list)]
        witnesses: dict[tuple[int, ComponentKey], list] = {}
        for side, graph in enumerate((reference, generated)):
            paired = mapping if side == 0 else reverse
            for key in graph.components:
                if key in paired or (graph.components[key].is_core and not _part(graph, key)):
                    continue
                signature, anchors = [], []
                for pid, pin in sorted(graph.pin_map(key).items()):
                    endpoints = []
                    if pin.net is not None:
                        for other in sorted(graph.nets[pin.net].pins):
                            owner = graph.pins[other].component
                            if owner == key:
                                token = ("self", graph.pins[other].id)
                            elif owner in paired:
                                anchor = owner if side == 0 else reverse[owner]
                                token = ("anchor", anchor[1], graph.pins[other].id)
                                anchors.append(list(token[1:]))
                            else:
                                token = ("unpaired", _label(graph, owner), graph.pins[other].id)
                            endpoints.append(json.dumps(token, ensure_ascii=False, sort_keys=True))
                    signature.append((pid, pin.net is None, tuple(sorted(endpoints))))
                if anchors:
                    fingerprint = (_label(graph, key), tuple(signature))
                    buckets[side][fingerprint].append(key)
                    witnesses[side, key] = sorted(anchors)
        additions = []
        for signature, left in buckets[0].items():
            right = buckets[1].get(signature, [])
            if len(left) == len(right) == 1:
                additions.append((left[0], right[0], signature))
        if not additions:
            break
        for left, right, signature in additions:
            mapping[left] = right
            evidence.append({"reference": left[1], "generated": right[1], "kind": "unique_anchored_local_structure",
                             "depth": depth, "anchors": witnesses[0, left],
                             "fingerprint_sha256": hashlib.sha256(repr(signature).encode()).hexdigest()})
        depth += 1
    mapping, position_evidence = extend_value_correspondence(reference, generated, mapping)
    evidence.extend(position_evidence)
    return mapping, evidence, report


def _metric(matched: int, reference_total: int, generated_total: int, **extra: Any) -> dict[str, Any]:
    precision = matched / generated_total if generated_total else None
    recall = matched / reference_total if reference_total else None
    score = 2 * matched / (reference_total + generated_total) if reference_total + generated_total else None
    return {"matched": matched, "reference_total": reference_total, "generated_total": generated_total,
            "precision": precision, "recall": recall, "f1": score, "score": score,
            "percentage": 100 * score if score is not None else None, **extra}


def _connected(graph: NetlistGraph, pin: PinKey) -> bool:
    return bool(graph.connected_pins(pin))


def _connection_rows(reference: NetlistGraph, generated: NetlistGraph, mapping: Mapping) -> tuple[dict, dict, list]:
    reverse = {b: a for a, b in mapping.items()}
    rows = {}
    nc = []
    for key, pin in sorted(reference.pins.items()):
        target = mapping.get(pin.component)
        other_key = (*target, pin.id) if target else None
        other = generated.pins.get(other_key)
        if not _connected(reference, key):
            nc.append({"reference": list(key[1:]), "generated": list(other_key[1:]) if other else None,
                       "kind": "nc" if pin.net is None else "singleton",
                       "status": "unresolved" if other is None else "equal" if (pin.net is None) == (other.net is None) and not _connected(generated, other_key) else "different"})
            continue
        row = {"reference": list(key[1:]), "generated": list(other_key[1:]) if other else None}
        if other is None:
            row.update(status="unresolved", reason="unpaired_component_or_missing_pin")
        elif not _connected(generated, other_key):
            row.update(status="different", reason="missing_connection")
        else:
            rp, gp = reference.connected_pins(key), generated.connected_pins(other_key)
            unknown = any(reference.pins[p].component not in mapping for p in rp) or any(generated.pins[p].component not in reverse for p in gp)
            if unknown:
                row.update(status="unresolved", reason="unpaired_neighbor")
            else:
                ref_neighbors = {(reference.pins[p].component, reference.pins[p].id) for p in rp}
                gen_neighbors = {(reverse[generated.pins[p].component], generated.pins[p].id) for p in gp}
                row.update(status="equal" if ref_neighbors == gen_neighbors else "different", reason="complete_direct_wire_neighbor_set")
        rows[key] = row
    matched = sum(row["status"] == "equal" for row in rows.values())
    rt = sum(_connected(reference, p) for p in reference.pins)
    gt = sum(_connected(generated, p) for p in generated.pins)
    metric = _metric(matched, rt, gt, unit="physical_pin_with_at_least_one_wire_peer",
                     unknown_reference=sum(r["status"] == "unresolved" for r in rows.values()),
                     different_reference=sum(r["status"] == "different" for r in rows.values()),
                     denominator="all connected pins on each side, including unmatched; NC and singleton nets excluded")
    return metric, rows, nc


def _value_rows(reference: NetlistGraph, generated: NetlistGraph, mapping: Mapping) -> tuple[dict, dict]:
    rows = {}
    for key, component in sorted(reference.components.items()):
        target = mapping.get(key)
        other = generated.components.get(target)
        if component.value is None and (other is None or other.value is None):
            continue
        row = {"reference_component": key[1], "generated_component": target[1] if target else None,
               "reference": component.value, "generated": other.value if other else None}
        if other is None:
            row.update(status="unresolved", equal=None)
        else:
            row.update(compare_values(component.value, other.value))
        rows[key] = row
    matched = sum(row.get("equal") is True for row in rows.values())
    rt = sum(c.value is not None for c in reference.components.values())
    gt = sum(c.value is not None for c in generated.components.values())
    return _metric(matched, rt, gt, unit="component_with_nonnull_value",
                   unknown_reference=sum(r.get("equal") is None for r in rows.values()),
                   denominator="all non-null values on each side, including unmatched; both-null values excluded"), rows


def _permission(cert: Mapping, permissions: Mapping | None, reference_contracts: Mapping | None,
                generated_contracts: Mapping | None) -> tuple[Mapping, list[str]]:
    from .equivalence_permissions import validate_permission_binding
    permissions = permissions if isinstance(permissions, Mapping) else {}
    errors = []
    errors.extend(validate_permission_binding(cert, permissions, reference_contracts, generated_contracts))
    if permissions.get("schema") != "pcbagent.equivalence_permissions.v1":
        errors.append("frozen input permissions schema missing")
    rules = permissions.get("rules", [])
    rules = rules if isinstance(rules, list) else []
    hits = [r for r in rules if isinstance(r, Mapping) and r.get("id") == cert.get("permission_id")]
    if len(hits) != 1:
        return {}, errors + ["permission_id must identify exactly one frozen rule"]
    rule = hits[0]
    permission_kind = ("connector_contact_permutation" if cert.get("kind") == "connector_connection_correspondence"
                       else cert.get("kind"))
    if rule.get("kind") != permission_kind or rule.get("reference_component") != cert.get("reference_component"):
        errors.append("permission kind/component mismatch")
    if rule.get("applies") is not True:
        errors.append("permission applicability is not confirmed")
    errors += evidence_errors(rule.get("evidence"), allow_input=True)
    input_evidence = [e for e in rule.get("evidence", []) if isinstance(e, Mapping) and e.get("source_kind") == "input"] if isinstance(rule.get("evidence"), list) else []
    if not input_evidence or any(e.get("document_sha256") != permissions.get("input_sha256") for e in input_evidence):
        errors.append("permission is not tied to the frozen input SHA256")
    return rule, errors


def _gpio_certificate(cert: Mapping, permissions: Mapping | None, reference: NetlistGraph,
                      generated: NetlistGraph, mapping: Mapping, reference_contracts: Mapping | None,
                      generated_contracts: Mapping | None) -> tuple[dict[str, str], list[str]]:
    from .certificate_facts import effective_gpio_fact
    rule, errors = _permission(cert, permissions, reference_contracts, generated_contracts)
    rc, gc = cert.get("reference_component"), cert.get("generated_component")
    rk, gk = (reference.source, rc), (generated.source, gc)
    if rk not in reference.components or gk not in generated.components or mapping.get(rk) != gk:
        return {}, errors + ["certificate components lack a proved one-to-one correspondence"]
    if not _part(reference, rk) or _part(reference, rk) != _part(generated, gk):
        errors.append("GPIO permutation requires identical core part identity")
    pins = cert.get("pin_map")
    if not isinstance(pins, Mapping) or not pins or not all(isinstance(k, str) and isinstance(v, str) for k, v in pins.items()):
        return {}, errors + ["nonempty physical pin_map required"]
    if set(pins) != set(pins.values()) or len(set(pins.values())) != len(pins):
        errors.append("pin_map must be a closed global bijection, with all other pins fixed")
    if not isinstance(rule.get("pin_ids"), list) or not set(pins) <= set(rule["pin_ids"]):
        errors.append("input does not authorize every permuted physical pin")
    if set(reference.pin_map(rk)) != set(generated.pin_map(gk)):
        errors.append("physical package pin sets differ")
    allowed = rule.get("allowed_usages", [])
    if not isinstance(allowed, list) or not allowed:
        errors.append("input does not enumerate allowed uses")
        allowed = []
    if cert.get("proposed_configuration_witness") is not None:
        from .gpio_configuration import apply_configuration_witness
        if errors:
            return dict(pins), errors
        reference_contracts, generated_contracts, configuration_errors = apply_configuration_witness(
            cert, reference, generated, reference_contracts or {}, generated_contracts or {}, mapping)
        errors.extend(configuration_errors)
        if errors:
            return dict(pins), errors
    for rp, gp in pins.items():
        if (*rk, rp) not in reference.pins or (*gk, gp) not in generated.pins:
            errors.append(f"{rp}->{gp}: physical pin does not exist")
            continue
        rf, reference_errors = effective_gpio_fact(reference, reference_contracts, rc, rp)
        gf, generated_errors = effective_gpio_fact(generated, generated_contracts, gc, gp)
        errors.extend(f"{rp}->{gp}: reference {e}" for e in reference_errors)
        errors.extend(f"{rp}->{gp}: generated {e}" for e in generated_errors)
        rb, gb = pin_binding(reference_contracts, rc, rp), pin_binding(generated_contracts, gc, gp)
        errors.extend(f"{rp}->{gp}: {e}" for e in evidence_errors(rf.get("evidence")) + evidence_errors(gf.get("evidence")))
        if component_contract(reference_contracts, rc).get("supplier_part") != _part(reference, rk) or component_contract(generated_contracts, gc).get("supplier_part") != _part(generated, gk):
            errors.append(f"{rp}->{gp}: pin facts belong to a different part")
        role = rf.get("semantic_role")
        if role not in {"logic_in", "logic_out", "logic_io"} or gf.get("semantic_role") != role:
            errors.append(f"{rp}->{gp}: roles are not equal supported GPIO roles")
        usage, mode = rb.get("usage"), rb.get("mode")
        if not usage or usage != gb.get("usage") or usage not in allowed:
            errors.append(f"{rp}->{gp}: selected usage is not equal and input-authorized")
        for fact in (rf, gf):
            capabilities = fact.get("capabilities", [])
            if not isinstance(capabilities, list) or "gpio" not in capabilities or usage not in capabilities:
                errors.append(f"{rp}->{gp}: datasheet does not support the requested GPIO capability")
            if not isinstance(fact.get("modes"), list) or mode not in fact["modes"]:
                errors.append(f"{rp}->{gp}: selected mode unsupported")
        if not mode or mode != gb.get("mode") or not rf.get("electrical_type") or rf.get("electrical_type") != gf.get("electrical_type"):
            errors.append(f"{rp}->{gp}: mode or electrical type differs")
        if role_mode_compatible(role, mode) is not True:
            errors.append(f"{rp}->{gp}: selected GPIO role and mode are inconsistent")
        for field in ("required_connection", "allow_nc"):
            if type(rf.get(field)) is not bool or rf.get(field) != gf.get(field):
                errors.append(f"{rp}->{gp}: incomplete or different {field} policy")
        if not isinstance(rb.get("resources"), list) or rb.get("resources") != gb.get("resources"):
            errors.append(f"{rp}->{gp}: resource assignments are missing or differ")
        for fact in (rf, gf):
            if fact.get("gpio_remap_safe") is not True:
                errors.append(f"{rp}->{gp}: mode-specific remap safety has not been confirmed")
            available = fact.get("resources")
            claimed = rb.get("resources")
            if not isinstance(available, list) or not isinstance(claimed, list) or any(r not in available for r in claimed):
                errors.append(f"{rp}->{gp}: selected resources lack datasheet capability support")
        domain = rb.get("domain")
        if not domain or domain != gb.get("domain"):
            errors.append(f"{rp}->{gp}: voltage domain identities differ or are missing")
            continue
        rd = (reference_contracts or {}).get("domains", {}).get(domain, {})
        gd = (generated_contracts or {}).get("domains", {}).get(domain, {})
        errors.extend(f"{rp}->{gp}: {e}" for e in evidence_errors(rd.get("evidence"), allow_input=True) + evidence_errors(gd.get("evidence"), allow_input=True))
        voltage = decimal_number(rd.get("voltage_v"))
        if voltage is None or voltage != decimal_number(gd.get("voltage_v")):
            errors.append(f"{rp}->{gp}: domain voltage differs or is unknown")
        for fact in (rf, gf):
            low, high = decimal_number(fact.get("voltage_min_v")), decimal_number(fact.get("voltage_max_v"))
            if low is None or high is None or voltage is None or not low <= voltage <= high:
                errors.append(f"{rp}->{gp}: domain voltage not proved within pin limits")
    return dict(pins), errors


def _permuted_graph(graph: NetlistGraph, permutations: Mapping[str, Mapping[str, str]]) -> NetlistGraph:
    pins = dict(graph.pins)
    members = defaultdict(set)
    for cid, pin_map in permutations.items():
        for reference_pin, actual_pin in pin_map.items():
            key, actual_key = (graph.source, cid, reference_pin), (graph.source, cid, actual_pin)
            pins[key] = replace(pins[key], net=graph.pins[actual_key].net)
    for key, pin in pins.items():
        if pin.net is not None:
            members[pin.net].add(key)
    nets = {key: NetNode(key, frozenset(ps)) for key, ps in members.items()}
    return replace(graph, pins=MappingProxyType(pins), nets=MappingProxyType(nets))


def _interval_certificate(cert: Mapping, permissions: Mapping | None, reference: NetlistGraph,
                          generated: NetlistGraph, mapping: Mapping,
                          reference_contracts: Mapping | None, generated_contracts: Mapping | None) -> list[str]:
    from .certificate_facts import value_source_errors
    rule, errors = _permission(cert, permissions, reference_contracts, generated_contracts)
    rk, gk = (reference.source, cert.get("reference_component")), (generated.source, cert.get("generated_component"))
    if rk not in reference.components or gk not in generated.components or mapping.get(rk) != gk:
        return errors + ["certificate values lack proved component correspondence"]
    errors.extend(value_source_errors(rule, reference_contracts, reference.components[rk].type))
    conditions = rule.get("conditions")
    context = (permissions or {}).get("context", {})
    if not isinstance(conditions, Mapping) or not isinstance(context, Mapping) or any(k not in context or context[k] != v for k, v in conditions.items()):
        errors.append("interval applicability conditions are missing or unsatisfied")
    quantities = [rule.get("minimum"), rule.get("maximum"), reference.components[rk].value, generated.components[gk].value]
    if not all(isinstance(q, str) for q in quantities):
        return errors + ["bounded dimensional interval and both nominal values required"]
    lo, hi, rv, gv = [normalized_value(q) for q in quantities]
    if lo[0] in {"text", "missing"} or len({q[0] for q in (lo, hi, rv, gv)}) != 1:
        errors.append("interval and both values must have the same explicit physical dimension")
    elif not lo[1] <= rv[1] <= hi[1] or not lo[1] <= gv[1] <= hi[1]:
        errors.append("one or both values fall outside the evidence-backed interval")
    return errors


def compare_consistency(generated: NetlistGraph, reference: NetlistGraph, *,
                        reference_contracts: Mapping[str, Any] | None = None,
                        generated_contracts: Mapping[str, Any] | None = None,
                        permissions: Mapping[str, Any] | None = None,
                        certificates: Sequence[Mapping[str, Any]] = (),
                        max_match_states: int = 100000, match_timeout_s: float = 10.0) -> dict[str, Any]:
    """Four consistency metrics, frozen-role checks and auditable difference credits."""
    if not isinstance(certificates, Sequence) or isinstance(certificates, (str, bytes)) or not all(isinstance(c, Mapping) for c in certificates):
        raise ValueError("certificates must be a sequence of objects")
    mapping, pair_evidence, raw_graph_report = _pair(reference, generated, max_states=max_match_states, timeout_seconds=match_timeout_s)
    raw_value_mapping = dict(mapping)
    raw_mapping, connection_pair_evidence = extend_connection_correspondence(reference, generated, mapping)
    raw_connection, raw_rows, nc = _connection_rows(reference, generated, raw_mapping)
    raw_value, value_rows = _value_rows(reference, generated, raw_value_mapping)
    value_search = raw_graph_report.get("value_witness_search", {})
    raw_value["witness_lower_bound"] = raw_value["score"]
    raw_value["upper_bound"] = (raw_value["score"] if value_search.get("equivalent") is True else
                                2 * min(raw_value["reference_total"], raw_value["generated_total"]) /
                                (raw_value["reference_total"] + raw_value["generated_total"])
                                if raw_value["reference_total"] + raw_value["generated_total"] else None)
    raw_value["optimality"] = "proved_all_equal" if value_search.get("equivalent") is True else "not_optimized_partial_witness"
    cert_reports = []
    valid_certificates = []
    for index, cert in enumerate(certificates):
        if any(not isinstance(cert.get(k), str) or not cert[k] for k in ("id", "kind", "permission_id", "reference_component", "generated_component")):
            cert_reports.append({"id": cert.get("id") if isinstance(cert.get("id"), str) else None,
                                 "kind": cert.get("kind") if isinstance(cert.get("kind"), str) else None,
                                 "index": index, "status": "unresolved", "reasons": ["certificate identity, kind, permission and component IDs must be nonempty strings"]})
        else:
            valid_certificates.append(cert)
    certificates = valid_certificates
    permutations = {}
    connector_projections = {}
    connector_open_projections = {}
    accepted_gpio = []
    connector_kinds = {"connector_contact_permutation", "connector_connection_correspondence"}
    permutation_kinds = {"gpio_permutation", *connector_kinds}
    present_kinds = {c.get("kind") for c in certificates}
    mixed_permutations = "gpio_permutation" in present_kinds and bool(connector_kinds & present_kinds)
    identifiers = Counter(c.get("id") for c in certificates if isinstance(c.get("id"), str))
    overlaps = Counter((c.get("generated_component"), p) for c in certificates if c.get("kind") in permutation_kinds and isinstance(c.get("pin_map"), Mapping) for p in c["pin_map"])
    for cert in certificates:
        if cert.get("kind") == "value_interval":
            continue
        errors = []
        if not isinstance(cert.get("id"), str) or not cert["id"] or identifiers[cert["id"]] != 1:
            errors.append("certificate id must be nonempty and globally unique")
        connector_proof = {}
        if cert.get("kind") not in permutation_kinds:
            errors.append("certificate kind is not implemented")
            pin_map = {}
        else:
            if cert.get("kind") in connector_kinds:
                from .connector_equivalence import check_connector_certificate, check_connector_connection_correspondence
                checker = (check_connector_connection_correspondence if cert["kind"] == "connector_connection_correspondence"
                           else check_connector_certificate)
                pin_map, specific, connector_proof = checker(
                    cert, permissions or {}, reference, generated, reference_contracts or {}, generated_contracts or {})
            else:
                pin_map, specific = _gpio_certificate(cert, permissions, reference, generated, mapping, reference_contracts, generated_contracts)
            errors += specific
            if mixed_permutations:
                errors.append("mixed GPIO and connector changes need a joint configuration proof not implemented by these individual certificates")
            if any(overlaps[(cert.get("generated_component"), p)] > 1 for p in pin_map):
                errors.append("overlapping GPIO or connector certificates are not independently creditable")
        cert_reports.append({"id": cert.get("id"), "kind": cert.get("kind"), "status": "unresolved" if errors else "accepted", "reasons": errors})
        if not errors and connector_proof:
            cert_reports[-1].update(connector_proof)
        if not errors and cert.get("proposed_configuration_witness") is not None:
            from .gpio_configuration import firmware_configuration
            cert_reports[-1].update(proof_scope="exists_source_validated_static_configuration",
                firmware_reconfiguration_required=True, original_firmware_compatibility_claimed=False,
                firmware_configuration=firmware_configuration(cert))
        if not errors:
            permutations.setdefault(cert["generated_component"], {}).update(pin_map)
            accepted_gpio.append(cert)
            if connector_proof.get("projection_operations"):
                connector_projections[cert["id"]] = connector_proof
            if connector_proof.get("open_projection_operations"):
                connector_open_projections[cert["id"]] = connector_proof
    adjusted_reference, projection_graph = reference, generated
    if connector_open_projections:
        from .connector_open_contacts import apply_open_projection
        adjusted_reference = apply_open_projection(reference, [row for proof in connector_open_projections.values()
            for row in proof["open_projection_operations"]["reference"]])
        projection_graph = apply_open_projection(generated, [row for proof in connector_open_projections.values()
            for row in proof["open_projection_operations"]["generated"]])
    if connector_projections:
        from .connector_ground_returns import apply_ground_projection
        projection_graph = apply_ground_projection(projection_graph,
            [operation for proof in connector_projections.values() for operation in proof["projection_operations"]])
    adjusted_graph = _permuted_graph(projection_graph, permutations)
    if permutations:
        improved, improved_evidence, adjusted_graph_report = _pair(adjusted_reference, adjusted_graph, max_states=max_match_states, timeout_seconds=match_timeout_s)
        # This correspondence is only for adjusted metrics. Raw metrics and
        # diagnostics were frozen before any certificate was examined.
        if any(k in mapping and mapping[k] != v for k, v in improved.items()) or any(v in mapping.values() and mapping.get(k) != v for k, v in improved.items()):
            reason = "permutation-derived component correspondence conflicts with existing proof"
            rejected_ids = {c["id"] for c in accepted_gpio}
            for report in cert_reports:
                if report.get("id") in rejected_ids and report["status"] == "accepted":
                    report["status"] = "unresolved"
                    report["reasons"].append(reason)
            cert_reports.append({"id": None, "kind": "global_bijection", "status": "unresolved", "reasons": [reason]})
            adjusted_graph, adjusted_graph_report, accepted_gpio = generated, raw_graph_report, []
            permutations, connector_projections, connector_open_projections = {}, {}, {}
            adjusted_reference = reference
        else:
            for ev in improved_evidence:
                if (reference.source, ev["reference"]) not in mapping:
                    pair_evidence.append({**ev, "under_certificates": [c["id"] for c in accepted_gpio]})
            mapping.update(improved)
    else:
        adjusted_graph_report = raw_graph_report
    if (any(c["kind"] in connector_kinds for c in accepted_gpio) and
            adjusted_graph_report["topology"]["equivalent"] is not True):
        for report in cert_reports:
            if report["status"] == "accepted" and report["kind"] in connector_kinds:
                report["status"] = "unresolved"
                report["reasons"].append("combined connector permutations lack the required complete global topology witness")
        accepted_gpio, permutations = [], {}
        mapping = dict(raw_value_mapping)
        pair_evidence = [ev for ev in pair_evidence if "under_certificates" not in ev]
        adjusted_graph, adjusted_graph_report = generated, raw_graph_report
        adjusted_reference = reference
    adjusted_connection_mapping, adjusted_connection_evidence = extend_connection_correspondence(adjusted_reference, adjusted_graph, mapping)
    adjusted_connection, adjusted_rows, adjusted_nc = _connection_rows(adjusted_reference, adjusted_graph, adjusted_connection_mapping)
    regressions = [key for key, row in raw_rows.items() if row["status"] == "equal" and adjusted_rows[key]["status"] != "equal"]
    if accepted_gpio and regressions:
        affected_ids = {c["id"] for c in accepted_gpio}
        for report in cert_reports:
            if report["id"] in affected_ids and report["status"] == "accepted":
                report["status"] = "unresolved"
                report["reasons"].append("applying these certificates would invalidate previously proved complete neighbor sets")
        accepted_gpio = []
        mapping = dict(raw_value_mapping)
        adjusted_connection_mapping = dict(raw_mapping)
        adjusted_connection_evidence = connection_pair_evidence
        pair_evidence = [ev for ev in pair_evidence if "under_certificates" not in ev]
        adjusted_graph_report = raw_graph_report
        adjusted_connection, adjusted_rows, adjusted_nc = raw_connection, raw_rows, nc
    # A structural connector witness must never supply a value correspondence,
    # including as a dependency of a separately submitted interval certificate.
    value_mapping = (raw_value_mapping if any(c["kind"] == "connector_connection_correspondence" for c in accepted_gpio)
                     else mapping)
    _, adjusted_value_rows = _value_rows(reference, generated, value_mapping)
    credits = []
    adjusted_connection_rows = dict(raw_rows)
    # A connection-only correspondence never established a component's value
    # position. Count new structural witnesses against the strict value map.
    newly_paired = set(mapping) - set(raw_value_mapping)
    changed_units = [key for key, row in adjusted_rows.items() if row["status"] == "equal" and raw_rows[key]["status"] != "equal"]
    for key in changed_units:
        peers = reference.connected_pins(key) | {key}
        involved = [c["id"] for c in accepted_gpio if any(p[1] == c["reference_component"] and p[2] in c["pin_map"] and c["pin_map"][p[2]] != p[2] for p in peers)
                    or connector_projections.get(c["id"], {}).get("reference_ground_network") == list(reference.pins[key].net or ())]
        previous_unresolved = raw_rows[key]["status"] == "unresolved"
        if previous_unresolved:
            if adjusted_graph_report["topology"]["equivalent"] is not True or not (involved or any(reference.pins[p].component in newly_paired for p in peers)):
                continue
            involved = [c["id"] for c in accepted_gpio]
        if involved:
            credits.append({"metric": "connection", "unit": list(key[1:]), "certificate_ids": involved,
                            "rule": "connector_ground_return_projected_neighbor_set" if any(cid in connector_projections for cid in involved) else
                                    "previous_unresolved_with_verified_global_witness" if previous_unresolved else
                                    "connector_connection_correspondence_complete_neighbor_set" if any(c["kind"] == "connector_connection_correspondence" and c["id"] in involved for c in accepted_gpio) else
                                    "connector_bijection_complete_neighbor_set" if any(c["kind"] == "connector_contact_permutation" and c["id"] in involved for c in accepted_gpio)
                                    else "gpio_bijection_complete_neighbor_set",
                            "previous_status": raw_rows[key]["status"]})
            adjusted_connection_rows[key] = adjusted_rows[key]
    connection_matched = sum(r["status"] == "equal" for r in adjusted_connection_rows.values())
    adjusted_connection = _metric(connection_matched, raw_connection["reference_total"], raw_connection["generated_total"],
                                  unit=raw_connection["unit"], denominator=raw_connection["denominator"],
                                  credited_units=sum(c["metric"] == "connection" for c in credits))
    value_credited = set()
    value_position_certificates = [c for c in accepted_gpio if c["kind"] == "gpio_permutation"]
    if value_position_certificates and adjusted_graph_report["topology"]["equivalent"] is True:
        for key in newly_paired:
            if adjusted_value_rows.get(key, {}).get("equal") is True and value_rows.get(key, {}).get("equal") is not True:
                value_credited.add(key)
                credits.append({"metric": "value", "unit": [key[1]], "certificate_ids": [c["id"] for c in value_position_certificates],
                                "rule": "previous_unresolved_with_verified_global_witness", "previous_status": "unresolved"})
    interval_overlaps = Counter(c.get("reference_component") for c in certificates if c.get("kind") == "value_interval")
    for cert in certificates:
        if cert.get("kind") != "value_interval":
            continue
        errors = _interval_certificate(cert, permissions, reference, generated, value_mapping, reference_contracts, generated_contracts)
        if not isinstance(cert.get("id"), str) or not cert["id"] or identifiers[cert["id"]] != 1:
            errors.append("certificate id must be nonempty and globally unique")
        if interval_overlaps[cert.get("reference_component")] > 1:
            errors.append("overlapping value certificates are not independently creditable")
        key = (reference.source, cert.get("reference_component"))
        if key not in value_rows or value_rows[key].get("equal") is not False:
            errors.append("certificate must identify a known raw value difference")
        cert_reports.append({"id": cert.get("id"), "kind": cert.get("kind"), "status": "unresolved" if errors else "accepted", "reasons": errors})
        if not errors:
            value_credited.add(key)
            credits.append({"metric": "value", "unit": [key[1]], "certificate_ids": [cert["id"]], "rule": "evidenced_applicable_value_interval"})
    adjusted_value = _metric(raw_value["matched"] + len(value_credited), raw_value["reference_total"], raw_value["generated_total"],
                             unit=raw_value["unit"], denominator=raw_value["denominator"], credited_units=len(value_credited))
    reference_check, generated_check = check_pin_contracts(reference, reference_contracts), check_pin_contracts(generated, generated_contracts)
    return {"schema": "pcbagent.case_consistency.v1",
            "metrics": {"raw_connection": raw_connection, "raw_value": raw_value,
                        "adjusted_connection": adjusted_connection, "adjusted_value": adjusted_value},
            "coverage": {"reference_components": len(reference.components), "generated_components": len(generated.components),
                         "paired_components": len(raw_mapping), "reference_pairing_fraction": len(raw_mapping) / len(reference.components) if reference.components else None,
                         "generated_pairing_fraction": len(raw_mapping) / len(generated.components) if generated.components else None,
                         "unpaired_reference": sorted(k[1] for k in reference.components.keys() - raw_mapping.keys()),
                         "unpaired_generated": sorted(k[1] for k in generated.components.keys() - set(raw_mapping.values())),
                         "value_paired_components": len(raw_value_mapping),
                         "adjusted_paired_components": len(adjusted_connection_mapping)},
            "pairing": {"evidence": pair_evidence, "ambiguous_groups": adjusted_graph_report["matching"]["ambiguous_groups"],
                        "values_used_for_structural_domains": False, "diagnostic_hypotheses_used": False,
                        "connection_only_evidence": connection_pair_evidence,
                        "adjusted_connection_only_evidence": adjusted_connection_evidence,
                        "value_witness_search": value_search,
                        "witness_pairs": [{"reference": a[1], "generated": b[1]} for a, b in sorted(raw_mapping.items())],
                        "value_witness_pairs": [{"reference": a[1], "generated": b[1]} for a, b in sorted(raw_value_mapping.items())],
                        "adjusted_witness_pairs": [{"reference": a[1], "generated": b[1]} for a, b in sorted(adjusted_connection_mapping.items())]},
            "topology": {"raw": raw_graph_report["topology"], "under_certificates": adjusted_graph_report["topology"]},
            "pin_checks": {"reference": reference_check, "generated": generated_check},
            "certificates": cert_reports, "credits": credits,
            "diagnostics": {"connections": list(raw_rows.values()), "values": list(value_rows.values()),
                            "nc_and_singletons": nc, "adjusted_nc_and_singletons": adjusted_nc},
            "acceptance": "fail" if generated_check["status"] == "fail" else
                          "static_consistency_verified" if generated_check["status"] == "pass" and
                          all(c["status"] == "accepted" for c in cert_reports) and
                          adjusted_graph_report["topology"]["equivalent"] is True and
                          adjusted_connection["score"] == 1 and adjusted_value["score"] in (1, None) else "unresolved",
            "scope": "conservative static consistency; no subjective equivalence credit or complete functional certification"}


__all__ = ["compare_consistency"]

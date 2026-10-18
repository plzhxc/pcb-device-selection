"""Finite proposed GPIO configurations, separate from historical firmware facts."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from itertools import permutations
from typing import Any

from .certificate_facts import digest, effective_gpio_fact, certificate_source_errors
from .pin_contracts import component_contract, pin_binding, check_pin_contracts, decimal_number, evidence_errors
from .reference_binding import graph_binding_digest

SCHEMA = "pcbagent.gpio_configuration_witness.v1"
FIELDS = frozenset({"usage", "mode", "domain", "resources", "logical_function"})


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _scope(reference: Any, generated: Any, rc: Mapping, gc: Mapping) -> dict:
    return {"reference_graph_sha256": graph_binding_digest(reference), "generated_graph_sha256": graph_binding_digest(generated),
            "reference_contracts_sha256": digest(rc), "generated_contracts_sha256": digest(gc)}


def _domain_key(graph: Any, contracts: Mapping, did: str, *, correspondence: Mapping | None = None) -> tuple | None:
    from .static_voltages import has_profiles
    if has_profiles(contracts):
        # This correspondence consumer has no complete multi-scenario witness.
        # Returning no domain identity rejects it; never use a nominal point.
        return None
    domains = _map(contracts.get("domains")); domain = _map(domains.get(did))
    reference = _map(domains.get(domain.get("reference"))) if domain.get("reference") is not None else domain
    def anchors(raw: Mapping) -> tuple | None:
        voltage = decimal_number(raw.get("voltage_v")); net = raw.get("net")
        if not net or voltage is None or evidence_errors(raw.get("evidence"), allow_input=True):
            return None
        endpoints = []
        for key, component in graph.components.items():
            if not component.is_core:
                continue
            cc = component_contract(contracts, key[1])
            for pid, pin in graph.pin_map(key).items():
                fact = _map(_map(cc.get("pins")).get(pid))
                if pin.net and pin.net[1] == net and fact.get("electrical_type") in {"power_in", "power_out", "ground"}:
                    if any(certificate_source_errors(contracts, component.anchor_id,
                            _map(fact.get("field_evidence")).get(field, fact.get("evidence")), [field])
                           for field in ("semantic_role", "electrical_type")):
                        return None
                    counterpart = correspondence.get(key) if correspondence is not None else key
                    if counterpart is None:
                        return None
                    endpoints.append((counterpart[1], component.anchor_id, pid))
        return (str(voltage), tuple(sorted(endpoints))) if endpoints else None
    actual, zero = anchors(domain), anchors(reference)
    if actual is None or zero is None or decimal_number(reference.get("voltage_v")) != 0 or reference.get("reference") is not None:
        return None
    return actual, zero


def apply_configuration_witness(cert: Mapping, reference: Any, generated: Any,
                                 reference_contracts: Mapping, generated_contracts: Mapping,
                                 correspondence: Mapping) -> tuple[dict, dict, list[str]]:
    """Verify a complete finite choice on each side, without changing its facts.

    Initial resource support is deliberately limited to documented empty GPIO
    resource sets. Nonempty resource strings cannot prove device-wide exclusion.
    Voltage domains must already have actual sourced supply/reference anchors.
    """
    witness = _map(cert.get("proposed_configuration_witness"))
    rc, gc = deepcopy(dict(reference_contracts)), deepcopy(dict(generated_contracts)); errors = []
    if (set(witness) != {"schema", "scope", "reference", "generated", "domain_map"} or
            witness.get("schema") != SCHEMA or witness.get("scope") != _scope(reference, generated, reference_contracts, generated_contracts)):
        return rc, gc, ["proposed configuration is not bound to both original graphs/contracts"]
    cid, gid = cert.get("reference_component"), cert.get("generated_component")
    pinmap = _map(cert.get("pin_map"))
    rtable, gtable = _map(witness.get("reference")), _map(witness.get("generated"))
    if set(rtable) != set(reference.pin_map((reference.source, cid))) or set(gtable) != set(generated.pin_map((generated.source, gid))):
        return rc, gc, ["configuration witness must cover every physical pin of both affected instances"]
    domain_map = _map(witness.get("domain_map"))
    if set(domain_map) != set(_map(rc.get("domains"))) or len(set(domain_map.values())) != len(domain_map):
        return rc, gc, ["proposed domain map must bijectively cover the reference domains"]
    for rd, gd in domain_map.items():
        rk = _domain_key(reference, rc, rd, correspondence=correspondence)
        gk = _domain_key(generated, gc, gd)
        if rk is None or rk != gk:
            errors.append("domain correspondence lacks equal actual supply/reference anchors and operating voltage")
    for side, graph, original, copied, component_id, table in (
            ("reference", reference, reference_contracts, rc, cid, rtable),
            ("generated", generated, generated_contracts, gc, gid, gtable)):
        for pid, raw in table.items():
            entry = _map(raw); old = pin_binding(original, component_id, pid)
            if set(entry) != FIELDS or not isinstance(entry.get("logical_function"), str) or not entry["logical_function"]:
                errors.append(side + " configuration pin has invalid fields")
                continue
            if any(old.get(field) is not None and entry.get(field) != old[field] for field in ("usage", "mode", "resources", "domain")):
                errors.append(side + " proposed configuration overwrites an independently known choice")
                continue
            binding = copied["components"][component_id]["bindings"][pid]
            binding.update({field: deepcopy(entry[field]) for field in FIELDS if field != "logical_function"})
            binding["configuration_basis"] = "proposed_existential_witness"
            fact, fact_errors = effective_gpio_fact(graph, copied, component_id, pid)
            errors.extend(side + " " + pid + ": " + error for error in fact_errors)
            if entry.get("usage") == "gpio" and (entry.get("resources") != [] or fact.get("resources") != []):
                errors.append(side + " GPIO needs a sourced empty resource set; nonempty mux resources require an explicit exclusion model")
        for pid in rtable:
            target = pinmap.get(pid, pid)
            if target not in gtable or rtable[pid].get("logical_function") != gtable[target].get("logical_function"):
                errors.append("logical functions do not follow the complete physical GPIO bijection")
    if errors:
        return rc, gc, list(dict.fromkeys(errors))
    # Rename only independently proved domain identities in the proposed copy.
    # Physical nets and the stored historical reference contracts remain intact.
    rc["domains"] = {domain_map[key]: {**deepcopy(value), "reference": domain_map.get(value.get("reference"))}
                     for key, value in rc["domains"].items()}
    for cc in rc["components"].values():
        for binding in cc.get("bindings", {}).values():
            if binding.get("domain") is not None:
                binding["domain"] = domain_map.get(binding["domain"])
        for supply in cc.get("supplies", {}).values():
            if supply.get("domain") is not None:
                supply["domain"] = domain_map.get(supply["domain"])
    for side, graph, copied, component_id in (("reference", reference, rc, cid), ("generated", generated, gc, gid)):
        report = check_pin_contracts(graph, copied)
        nets = {p.net[1] for p in graph.pin_map((graph.source, component_id)).values() if p.net}
        for row in report["diagnostics"]:
            if row["status"] in {"fail", "unresolved"} and (row.get("component") in {None, component_id} or row.get("net") in nets):
                errors.append(side + " proposed configuration fails an applicable static check: " + row["rule"])
    return rc, gc, list(dict.fromkeys(errors))


def firmware_configuration(cert: Mapping) -> list[dict]:
    witness = _map(cert.get("proposed_configuration_witness")); rows = []
    for rp, config in _map(witness.get("reference")).items():
        gp = _map(cert.get("pin_map")).get(rp, rp); other = _map(_map(witness.get("generated")).get(gp))
        if _map(config).get("usage") == "gpio":
            rows.append({"logical_function": config["logical_function"], "reference_pin": rp, "generated_pin": gp,
                         "reference_mode": config.get("mode"), "generated_mode": other.get("mode"),
                         "reference_component": cert["reference_component"], "generated_component": cert["generated_component"]})
    return rows


def propose_gpio_configuration_witness(reference: Any, generated: Any, reference_contracts: Mapping,
                                        generated_contracts: Mapping, cert: Mapping, correspondence: Mapping) -> dict:
    """Use independently validated generated choices as candidates, not facts.

    This first finite constructor requires existing provable voltage domains;
    it does not invent supply voltages or search undocumented resource settings.
    """
    cid, gid = cert["reference_component"], cert["generated_component"]
    rtable, gtable, dmap, errors = {}, {}, {}, []
    for rd in _map(reference_contracts.get("domains")):
        key = _domain_key(reference, reference_contracts, rd, correspondence=correspondence)
        choices = [gd for gd in _map(generated_contracts.get("domains")) if key is not None and key == _domain_key(generated, generated_contracts, gd)]
        if len(choices) != 1:
            errors.append("source-proved reference voltage domain has no unique generated counterpart")
        else:
            dmap[rd] = choices[0]
    inverse_domains = {v: k for k, v in dmap.items()}
    for rp in reference.pin_map((reference.source, cid)):
        gp = _map(cert.get("pin_map")).get(rp, rp)
        rb, gb = pin_binding(reference_contracts, cid, rp), pin_binding(generated_contracts, gid, gp)
        function = "reference-terminal:" + cid + ":" + rp
        ge = {field: deepcopy(gb.get(field)) for field in ("usage", "mode", "domain", "resources")}
        re = {field: deepcopy(rb.get(field) if rb.get(field) is not None else ge[field]) for field in ge}
        if rb.get("domain") is None and ge["domain"] is not None:
            re["domain"] = inverse_domains.get(ge["domain"])
        rtable[rp], gtable[gp] = {**re, "logical_function": function}, {**ge, "logical_function": function}
    witness = {"schema": SCHEMA, "scope": _scope(reference, generated, reference_contracts, generated_contracts),
               "reference": rtable, "generated": gtable, "domain_map": dmap}
    candidate = {**dict(cert), "proposed_configuration_witness": witness}
    if not errors:
        _, _, errors = apply_configuration_witness(candidate, reference, generated, reference_contracts, generated_contracts, correspondence)
    return {"candidate": candidate, "status": "proposed" if not errors else "unresolved", "reasons": errors,
            "proof_scope": "proposed static reconfiguration, not original firmware compatibility"}


def propose_equivalence_certificates(reference: Any, generated: Any, reference_contracts: Mapping,
                                     generated_contracts: Mapping, permissions: Mapping, *, max_states: int = 64) -> dict:
    """Select one independently checked candidate per instance, within a bound.

    ``search_complete`` concerns this first-valid, single-instance search only;
    it does not claim exhaustive permutations or joint multi-instance coverage.
    Candidate validation reuses the scoring consumer, but no scores or credits
    are returned by this proposal API.
    """
    if type(max_states) is not int or not 1 <= max_states <= 4096:
        raise ValueError("max_states must be in 1..4096")
    from .consistency import _pair, _permuted_graph, _gpio_certificate, compare_consistency
    from .netlist_compare import compare_netlist_graphs
    correspondence, _, _ = _pair(reference, generated, max_states=10000, timeout_seconds=2)
    candidates, diagnostics, states, selected_instances = [], [], 0, set()
    def result(complete: bool) -> dict:
        from .connector_equivalence import propose_connector_certificates
        connector = propose_connector_certificates(reference, generated, reference_contracts, generated_contracts, permissions)
        combined = [*candidates, *connector["certificates"]]
        extra_diagnostics = list(connector["diagnostics"])
        if combined:
            checked = compare_consistency(generated, reference, reference_contracts=reference_contracts,
                generated_contracts=generated_contracts, permissions=permissions, certificates=combined,
                max_match_states=10000, match_timeout_s=2)
            rejected = [row for row in checked["certificates"] if row["status"] != "accepted"]
            if rejected:
                # Never emit a mixed collection which the actual scoring path
                # rejects. This initial proposer does not choose between
                # incompatible joint configurations on the caller's behalf.
                extra_diagnostics.extend(rejected)
                combined = []
        return {"certificates": combined, "diagnostics": [*diagnostics, *extra_diagnostics], "states": states,
                "search_complete": complete,
                "search_scope": "first valid single-instance permutation; joint multi-instance changes not searched",
                "selection_policy": "one independently consumer-validated candidate per instance",
                "connector_search": {k: v for k, v in connector.items() if k not in {"certificates", "diagnostics"}}}
    rules = permissions.get("rules", [])
    rules = rules if isinstance(rules, list) else []
    for rule in sorted((r for r in rules if isinstance(r, Mapping)),
                       key=lambda r: (str(r.get("reference_component")), str(r.get("id")))):
        if rule.get("kind") != "gpio_permutation" or rule.get("reference_component") in selected_instances:
            continue
        rk = (reference.source, rule.get("reference_component")); gk = correspondence.get(rk)
        ids = rule.get("pin_ids", [])
        if (gk is None or not isinstance(ids, list) or len(ids) < 2 or
                not all(isinstance(pid, str) for pid in ids) or len(ids) != len(set(ids)) or
                not set(ids) <= set(reference.pin_map(rk)) or not set(ids) <= set(generated.pin_map(gk))):
            continue
        ids = sorted(ids)
        for targets in permutations(ids):
            if states >= max_states:
                return result(False)
            states += 1
            pinmap = dict(zip(ids, targets))
            if all(key == value for key, value in pinmap.items()):
                continue
            transformed = _permuted_graph(generated, {gk[1]: pinmap})
            topology = compare_netlist_graphs(transformed, reference, max_states=10000, timeout_seconds=2)
            if topology["topology"]["equivalent"] is not True:
                continue
            cert = {"id": "gpio-candidate:" + str(states), "kind": "gpio_permutation", "permission_id": rule["id"],
                    "reference_component": rk[1], "generated_component": gk[1], "pin_map": pinmap}
            proposal = propose_gpio_configuration_witness(reference, generated, reference_contracts, generated_contracts, cert, correspondence)
            if proposal["status"] != "proposed":
                diagnostics.append({"id": cert["id"], "status": "unresolved", "reasons": proposal["reasons"]})
                continue
            candidate = proposal["candidate"]
            _, errors = _gpio_certificate(candidate, permissions, reference, generated, correspondence,
                                           reference_contracts, generated_contracts)
            if not errors:
                # Include previously selected instances: the same consumer must
                # preserve its global pairing and previously proved connections.
                checked = compare_consistency(generated, reference, reference_contracts=reference_contracts,
                    generated_contracts=generated_contracts, permissions=permissions,
                    certificates=[*candidates, candidate], max_match_states=10000, match_timeout_s=2)
                errors = [reason for row in checked["certificates"] if row["status"] != "accepted"
                          for reason in row["reasons"]]
                if len(checked["certificates"]) != len(candidates) + 1 and not errors:
                    errors.append("consumer did not accept exactly the complete selected certificate set")
            if errors:
                diagnostics.append({"id": cert["id"], "status": "unresolved", "reasons": errors})
                continue
            candidates.append(candidate)
            selected_instances.add(rk[1])
            break
    return result(True)


__all__ = ["apply_configuration_witness", "propose_gpio_configuration_witness", "propose_equivalence_certificates", "firmware_configuration"]

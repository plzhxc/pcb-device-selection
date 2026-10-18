"""Private abstract connector co-design proofs, exclusively for evaluation.

The proof establishes an available new harness/static design, never the rating
of an old physical connector or compatibility with its original harness.
Reference historical facts remain untouched. Board-local proofs do not certify
interoperability between different boards.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
import json
from typing import Any

from .certificate_facts import digest, effective_gpio_fact
from .common_terminal_contracts import DEFINITIONS, VERSION
from .electrical_values import compare_values
from .pin_contracts import check_pin_contracts, component_contract, pin_binding
from .reference_binding import graph_binding_digest

KIND = "connector_contact_permutation"
CONNECTION_KIND = "connector_connection_correspondence"
CONNECTION_SCHEMA = "pcbagent.connection_correspondence.v1"
CONNECTION_CLAIMS = {"wire_incidence_correspondence": True, "value_equivalence": False,
                     "same_static_circuit": False, "functional_equivalence": False}


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def proof_scope(reference: Any, generated: Any, rc: Mapping, gc: Mapping) -> dict:
    return {"reference_graph_sha256": graph_binding_digest(reference),
            "generated_graph_sha256": graph_binding_digest(generated),
            "reference_contracts_sha256": digest(rc), "generated_contracts_sha256": digest(gc),
            "input_sha256": _map(gc.get("case_input_snapshot")).get("sha256")}


def resolve_connector(graph: Any, permission: Mapping) -> tuple[Any, list[str]]:
    """Resolve a unique abstract interface without selecting by contact order."""
    selector = _map(permission.get("selector")); count = selector.get("contact_count")
    parts = selector.get("core_device_ids", [])
    if (type(count) is not int or not 2 <= count <= 32 or not isinstance(parts, list) or not parts or
            not all(isinstance(part, str) for part in parts) or len(set(parts)) != len(parts)):
        return None, ["connector selector lacks bounded contact count and core identities"]
    cores = {part: [key for key, c in graph.components.items() if c.is_core and c.anchor_id == part] for part in parts}
    if any(len(keys) != 1 for keys in cores.values()):
        return None, ["each selected core must identify one actual instance"]
    selected, hits = {keys[0] for keys in cores.values()}, []
    for key, component in graph.components.items():
        if (component.type != "connector" or component.is_abstract is not True or
                component.anchor_id is not None or set(graph.pin_map(key)) != {str(i) for i in range(1, count + 1)}):
            continue
        adjacent = {graph.pins[peer].component for pin in graph.pin_map(key).values() if pin.net
                    for peer in graph.nets[pin.net].pins if graph.components[graph.pins[peer].component].is_core}
        if adjacent == selected:
            hits.append(key)
    return (hits[0], []) if len(hits) == 1 else (None, ["interface selector does not resolve exactly one private abstract connector"])


def _uniform_contract(graph: Any, contracts: Mapping, key: Any) -> list[str]:
    cc = component_contract(contracts, key[1]); pins = graph.pin_map(key); errors = []
    definition = DEFINITIONS["connector"]
    payload = {"version": VERSION, "definition": definition}
    expected_sha = digest(payload)
    accepted_quotes = {json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                       json.dumps(definition, sort_keys=True)}
    if cc.get("pin_space") != "logical" or cc.get("supplier_part") is not None or cc.get("fixed_identity"):
        errors.append("connector contract is not a pure abstract logical contact definition")
    if set(_map(cc.get("pins"))) != set(pins):
        errors.append("uniform contact contract must cover every logical contact")
    for pid, fact in _map(cc.get("pins")).items():
        fact = _map(fact)
        if (fact.get("semantic_role") != "passive_terminal" or fact.get("electrical_type") != "passive" or
                fact.get("modes") != ["passive"] or fact.get("resources") != [] or
                fact.get("capabilities") not in (["passive"], ["passive_terminal"])):
            errors.append(pid + ": contact does not have a uniform passive electrical contract")
        refs = fact.get("evidence", [])
        if not isinstance(refs, list) or not any(
                _map(ref).get("source_kind") == "template" and
                _map(ref).get("source_ref") in {"pcbagent.common.connector@" + VERSION, "pcbagent.common_terminal.connector@" + VERSION} and
                _map(ref).get("document_sha256") == expected_sha and _map(ref).get("quote") in accepted_quotes and
                _map(ref).get("locator") in {"DEFINITIONS.connector", "validation_platform.common_terminal_contracts.DEFINITIONS.connector"}
                for ref in refs):
            errors.append(pid + ": contact facts lack the exact current generic template evidence")
    return errors


def _complete_witness(reference: Any, transformed: Any, mapping: Mapping,
                      connector: Any, target: Any, rc: Mapping, gc: Mapping, *,
                      require_equal_values: bool = True) -> list[str]:
    """Verify one full incidence, concrete identity and non-connector value map."""
    if (set(mapping) != set(reference.components) or set(mapping.values()) != set(transformed.components) or
            len(mapping) != len(set(mapping.values())) or mapping.get(connector) != target):
        return ["connector proof needs one complete global component bijection"]
    errors, netmap, reverse = [], {}, {}
    for rk, gk in mapping.items():
        a, b = reference.components[rk], transformed.components[gk]
        if (a.type != b.type or a.is_abstract != b.is_abstract or a.anchor_id != b.anchor_id or
                a.is_abstract is not True and not a.is_core and a.name != b.name):
            errors.append("global witness changes a concrete component type/model identity")
        if require_equal_values and a.type != "connector" and compare_values(a.value, b.value).get("equal") is not True and not (a.value is None and b.value is None):
            errors.append("non-connector values are not identical under this same global witness")
        if a.is_core:
            rsource = _map(_map(rc.get("source_bundles")).get(a.anchor_id))
            gsource = _map(_map(gc.get("source_bundles")).get(b.anchor_id))
            if not rsource or rsource != gsource:
                errors.append("core physical source identities differ between the bound scenarios")
            for pid in transformed.pin_map(gk):
                _, issues = effective_gpio_fact(transformed, gc, gk[1], pid)
                errors.extend("generated core source: " + issue for issue in issues)
        rp, gp = reference.pin_map(rk), transformed.pin_map(gk)
        if set(rp) != set(gp):
            errors.append("global witness changed a physical pin inventory")
            continue
        for pid, pin in rp.items():
            an, bn = pin.net, gp[pid].net
            if an is None or bn is None:
                if (an is None) != (bn is None):
                    errors.append("global witness changed an unconnected contact or component pin")
            elif netmap.setdefault(an, bn) != bn or reverse.setdefault(bn, an) != an:
                errors.append("complete pin incidence does not induce a global net bijection")
    if set(netmap) != set(reference.nets) or set(reverse) != set(transformed.nets):
        errors.append("global witness does not cover every actual net")
    return list(dict.fromkeys(errors))


def _connection_semantics(reference: Any, generated: Any, mapping: Mapping,
                          rc: Mapping, gc: Mapping) -> tuple[list[str], list[dict]]:
    """Preserve known modes/polarity; never invent absent historical settings.

    All non-connector pin IDs remain fixed in the incidence witness. Unknown
    historical attributes are disclosed rather than copied from the candidate;
    this proof does not establish historical electrical/functional equivalence.
    """
    errors, unknown = [], []
    for rk, gk in mapping.items():
        a, b = reference.components[rk], generated.components[gk]
        rcc, gcc = component_contract(rc, rk[1]), component_contract(gc, gk[1])
        if a.type == "connector":
            continue
        if not a.is_core and a.type not in {"resistor", "capacitor", "inductor"}:
            errors.append("connection correspondence has no implemented semantics for this non-fixed component type")
        missing = []
        if not a.is_core:
            rm, gm = rcc.get("logical_pin_mapping"), gcc.get("logical_pin_mapping")
            if rm is not None and gm is not None and rm != gm:
                errors.append("connection correspondence changes a known non-connector logical terminal mapping")
            rp = _map(_map(_map(rcc.get("logical_terminal_contract")).get("voltage_obligation")).get("parameters"))
            gp = _map(_map(_map(gcc.get("logical_terminal_contract")).get("voltage_obligation")).get("parameters"))
            for field in ("polarization", "positive_port", "polarity", "contact_mode", "default_state"):
                if field in rp and field in gp and rp[field] != gp[field]:
                    errors.append("connection correspondence changes known component " + field)
                elif field in gp and field not in rp:
                    missing.append(field)
            if a.type == "capacitor" and "polarization" not in rp:
                missing.append("polarization")
            if "rated_voltage_v" not in rp:
                missing.append("rated_voltage_v")
        for pid in set(reference.pin_map(rk)) & set(generated.pin_map(gk)):
            rb, gb = pin_binding(rc, rk[1], pid), pin_binding(gc, gk[1], pid)
            rf, gf = _map(_map(rcc.get("pins")).get(pid)), _map(_map(gcc.get("pins")).get(pid))
            for field in ("semantic_role", "electrical_type"):
                if (rf.get(field) not in (None, "unknown") and gf.get(field) not in (None, "unknown")
                        and rf[field] != gf[field]):
                    errors.append("connection correspondence changes a known pin " + field)
            for field in ("mode", "usage"):
                if rb.get(field) is not None and gb.get(field) is not None and rb[field] != gb[field]:
                    errors.append("connection correspondence changes a known pin " + field)
                elif rb.get(field) is None and gb.get(field) is not None:
                    missing.append("pin:" + pid + ":" + field)
            for collection in ("configuration", "requirements"):
                rconditions, gconditions = _map(rb.get(collection)), _map(gb.get(collection))
                for field in rconditions.keys() & gconditions.keys():
                    rv, gv = _map(rconditions[field]).get("value"), _map(gconditions[field]).get("value")
                    if rv is not None and gv is not None and (type(rv) is not type(gv) or rv != gv):
                        errors.append("connection correspondence changes a known frozen operating condition")
        if missing:
            unknown.append({"reference_component": rk[1], "generated_component": gk[1],
                            "fields": sorted(set(missing)), "historical_attributes_inferred": False})
    return list(dict.fromkeys(errors)), unknown


def check_connector_certificate(cert: Mapping, permissions: Mapping, reference: Any, generated: Any,
                                 rc: Mapping, gc: Mapping) -> tuple[dict, list[str], dict]:
    """Consume a complete proposed contact map; no score is awarded here."""
    if cert.get("kind") != KIND:
        return {}, ["same-static-circuit checker requires connector_contact_permutation"], {}
    return _check_connector_certificate(cert, permissions, reference, generated, rc, gc, connection_only=False)


def check_connector_connection_correspondence(cert: Mapping, permissions: Mapping,
                                               reference: Any, generated: Any,
                                               rc: Mapping, gc: Mapping) -> tuple[dict, list[str], dict]:
    """A separate wire-only theorem under the same bounded harness authority."""
    claims = cert.get("claims")
    if (cert.get("kind") != CONNECTION_KIND or cert.get("schema") != CONNECTION_SCHEMA or
            not isinstance(claims, Mapping) or claims != CONNECTION_CLAIMS or
            any(type(value) is not bool for value in claims.values())):
        return {}, ["connection correspondence requires its exact schema and connection-only claims"], {}
    return _check_connector_certificate(cert, permissions, reference, generated, rc, gc, connection_only=True)


def _check_connector_certificate(cert: Mapping, permissions: Mapping, reference: Any, generated: Any,
                                 rc: Mapping, gc: Mapping, *, connection_only: bool) -> tuple[dict, list[str], dict]:
    from .consistency import _permission, _pair, _permuted_graph
    rule, errors = _permission(cert, permissions, rc, gc)
    if errors:
        return {}, errors, {}
    permission = _map(rule.get("connector_permission"))
    rk, rerrors = resolve_connector(reference, permission)
    gk, gerrors = resolve_connector(generated, permission)
    errors.extend(rerrors + gerrors)
    if rk is None or gk is None:
        return {}, errors, {}
    if cert.get("reference_component") != rk[1] or cert.get("generated_component") != gk[1]:
        errors.append("certificate component does not match the independently selected interface")
    if cert.get("scope") != proof_scope(reference, generated, rc, gc):
        errors.append("connector certificate is not bound to both actual graphs, contracts and full input")
    pinmap = cert.get("pin_map")
    rids, gids = set(reference.pin_map(rk)), set(generated.pin_map(gk))
    if (not isinstance(pinmap, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in pinmap.items()) or
            set(pinmap) != rids or set(pinmap.values()) != gids or len(pinmap) != len(set(pinmap.values()))):
        return {}, errors + ["every contact, including spares, must participate in the complete bijection"], {}
    errors.extend(_uniform_contract(reference, rc, rk) + _uniform_contract(generated, gc, gk))
    from .connector_open_contacts import prove_open_contacts
    open_reference, open_generated, open_errors, open_proof = prove_open_contacts(reference, generated, rc, gc, rk, gk, pinmap)
    errors.extend(open_errors)
    if cert.get("open_projection_sha256") != open_proof.get("open_projection_sha256"):
        errors.append("certificate open-representation digest differs from the independently recomputed operation list")
    report = check_pin_contracts(open_generated, gc)
    if report["status"] != "pass":
        errors.append("generated static electrical checks must all pass before connector equivalence")
    cc = component_contract(gc, gk[1]); logical = _map(cc.get("logical_terminal_contract"))
    params = _map(_map(logical.get("voltage_obligation")).get("parameters"))
    terminal_map = _map(logical.get("terminal_mapping"))
    if terminal_map != {pid: "contact_" + pid for pid in gids}:
        errors.append("generated contact mapping must be complete canonical logical numbering")
    translations = []
    for rp, gp in pinmap.items():
        pin = generated.pin_map(gk)[gp]; terminal = "contact_" + gp
        state = _map(params.get("contact_usage")).get(terminal)
        port = _map(_map(params.get("contact_port_bindings")).get(terminal))
        if state != ("assigned" if generated.connected_pins(pin.key) else "spare") or generated.connected_pins(pin.key) and (
                not isinstance(port.get("port"), str) or not port["port"] or type(port.get("bit")) is not int or port["bit"] < 0):
            errors.append("actual contact use does not match its complete external-port selection")
        endpoints = [{"generated_component": peer[1], "supplier_part": generated.components[generated.pins[peer].component].anchor_id,
                      "physical_pin": peer[2]} for peer in sorted(generated.nets[pin.net].pins) if generated.components[generated.pins[peer].component].is_core] if pin.net else []
        translations.append({"reference_contact": rp, "generated_contact": gp, "contact_usage": state,
            "actual_connection_state": "connected" if generated.connected_pins(pin.key) else "NC",
            "generated_signal_function": pin_binding(gc, gk[1], gp).get("signal_function"),
            "generated_external_port": dict(port), "fixed_core_endpoints": endpoints})
    if errors:
        return dict(pinmap), list(dict.fromkeys(errors)), {}
    from .connector_ground_returns import prove_ground_returns
    projected, projection_errors, projection_proof = prove_ground_returns(
        open_reference, open_generated, rc, gc, rk, gk, pinmap, permission, cert.get("additional_ground_contacts"))
    errors.extend(projection_errors)
    if projection_proof and cert.get("projection_sha256") != projection_proof["projection_sha256"]:
        errors.append("certificate projection digest differs from the independently recomputed operation list")
    if not projection_proof and cert.get("projection_sha256") is not None:
        errors.append("certificate claims a projection digest without any proved added return")
    if errors:
        return dict(pinmap), list(dict.fromkeys(errors)), {}
    transformed = _permuted_graph(projected, {gk[1]: pinmap})
    mapping, _, topology = _pair(open_reference, transformed, max_states=100000, timeout_seconds=5)
    if topology["topology"]["equivalent"] is not True:
        errors.append("contact map does not prove full circuit topology equivalence")
    errors.extend(_complete_witness(open_reference, transformed, mapping, rk, gk, rc, gc,
                                    require_equal_values=not connection_only))
    unknown = []
    if connection_only:
        semantic_errors, unknown = _connection_semantics(open_reference, transformed, mapping, rc, gc)
        errors.extend(semantic_errors)
    if errors:
        return dict(pinmap), list(dict.fromkeys(errors)), {}
    proof = {"proof_scope": ("board_local_new_harness_with_bounded_source_ground_returns" if projection_proof else
                            "board_local_exists_same_static_circuit_with_new_harness"),
        "board_id": permission.get("board_id"), "interface_id": permission.get("interface_id"),
        "input_sha256": _map(gc.get("case_input_snapshot")).get("sha256"),
        "new_harness_required": True, "original_harness_compatibility_claimed": False,
        "historical_reference_firmware_verified": False, "historical_reference_part_ratings_verified": False,
        "whole_case_interoperability_verified": False,
        "generated_static_report_sha256": digest(report),
        "generated_static_report_graph_sha256": graph_binding_digest(open_generated),
        "component_witness": [{"reference": r[1], "generated": g[1]} for r, g in sorted(mapping.items())],
        "harness_translation": sorted(translations, key=lambda row: int(row["reference_contact"])),
        **projection_proof, **open_proof}
    if connection_only:
        net_pairs = {pin.net: transformed.pin_map(mapping[pin.component])[pin.id].net
                     for pin in open_reference.pins.values() if pin.net is not None and pin.component in mapping}
        proof.update(proof_scope="board_local_connection_correspondence_with_new_harness",
            schema=CONNECTION_SCHEMA, claims=dict(CONNECTION_CLAIMS), value_credit_allowed=False,
            non_connector_pin_ids_preserved=True, reference_unknown_attributes=unknown,
            generated_static_gate_passed=report["status"] == "pass",
            pin_witness=[{"reference_component": r[1], "generated_component": g[1],
                          "pin_ids": sorted(open_reference.pin_map(r))}
                         for r, g in sorted(mapping.items()) if open_reference.components[r].type != "connector"],
            net_witness=[{"reference": list(r), "generated": list(g)} for r, g in sorted(net_pairs.items()) if g is not None],
            value_differences_not_credited=[{"reference_component": r[1], "generated_component": g[1],
                "reference_value": open_reference.components[r].value, "generated_value": transformed.components[g].value}
                for r, g in sorted(mapping.items()) if open_reference.components[r].type != "connector"
                and not (open_reference.components[r].value is None and transformed.components[g].value is None)
                and compare_values(open_reference.components[r].value, transformed.components[g].value).get("equal") is not True])
    return dict(pinmap), list(dict.fromkeys(errors)), proof if not errors else {}


def propose_connector_certificates(reference: Any, generated: Any, rc: Mapping, gc: Mapping,
                                    permissions: Mapping) -> dict:
    """Use direct core endpoint signatures, then verify the complete circuit."""
    from .consistency import _pair
    mapping, _, _ = _pair(reference, generated, max_states=10000, timeout_seconds=2)
    candidates, diagnostics, used = [], [], set()
    rules = permissions.get("rules", [])
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, Mapping) or rule.get("kind") != KIND:
            continue
        p = _map(rule.get("connector_permission"))
        rk, errors = resolve_connector(reference, p); gk, issues = resolve_connector(generated, p)
        errors += issues
        if rk is None or gk is None or rk in used:
            diagnostics.append({"status": "unresolved", "reasons": errors or ["interface already has a selected candidate"]})
            continue
        groups = [defaultdict(list), defaultdict(list)]
        for side, graph, key in ((0, reference, rk), (1, generated, gk)):
            for pid, pin in graph.pin_map(key).items():
                anchors = []
                if graph.connected_pins(pin.key):
                    for peer in graph.nets[pin.net].pins:
                        component = graph.pins[peer].component
                        if graph.components[component].is_core:
                            target = mapping.get(component) if side == 0 else component
                            if target is None:
                                errors.append("contact has an unproved core instance correspondence")
                            else:
                                anchors.append((target[1], peer[2]))
                    if not anchors:
                        errors.append("first proposer requires a direct core anchor for every connected contact")
                signature = ("wired", tuple(sorted(anchors))) if graph.connected_pins(pin.key) else ("unused",)
                groups[side][signature].append(pid)
        equal_groups = set(groups[0]) == set(groups[1]) and all(len(v) == len(groups[1].get(k, [])) for k, v in groups[0].items())
        pinmap, additional = {}, []
        if equal_groups:
            pinmap = {r: g for key, ids in groups[0].items()
                      for r, g in zip(sorted(ids, key=int), sorted(groups[1][key], key=int))}
        elif _map(p.get("spare_contact_policy")).get("mode") == "additional_ground_returns":
            # Match every original active function first. Only residual unused
            # reference contacts can pair with residual generated incidences;
            # the independent ground/source/full-graph proof checks each one.
            remaining = {key: sorted(ids, key=int) for key, ids in groups[1].items()}
            unused = []
            for signature, ids in sorted(groups[0].items()):
                if signature[0] == "unused":
                    unused.extend((pid, signature) for pid in sorted(ids, key=int))
                    continue
                targets = remaining.get(signature, [])
                if len(targets) < len(ids):
                    errors.append("original active contact function is missing")
                    continue
                for rp, gp in zip(sorted(ids, key=int), targets[:len(ids)]):
                    pinmap[rp] = gp
                remaining[signature] = targets[len(ids):]
            for rp, signature in sorted(unused, key=lambda row: int(row[0])):
                targets = remaining.get(signature, [])
                if targets:
                    pinmap[rp] = targets.pop(0)
                    continue
                choices = sorted((pid, sig) for sig, ids in remaining.items() if sig[0] == "wired" for pid in ids)
                if not choices:
                    errors.append("unused contact has no candidate counterpart")
                    continue
                gp, sig = choices[0]; remaining[sig].remove(gp)
                pinmap[rp] = gp
                additional.append({"reference_contact": rp, "generated_contact": gp})
            if any(remaining.values()):
                errors.append("generated contact inventory exceeds the complete interface bijection")
        else:
            errors.append("complete contact signature group counts differ")
        if errors:
            diagnostics.append({"status": "unresolved", "reasons": list(dict.fromkeys(errors))})
            continue
        from .connector_open_contacts import prove_open_contacts
        open_reference, open_generated, issues, open_proof = prove_open_contacts(reference, generated, rc, gc, rk, gk, pinmap)
        if issues:
            diagnostics.append({"status": "unresolved", "reasons": issues})
            continue
        if all(r == g for r, g in pinmap.items()) and not additional and not open_proof:
            continue
        cert = {"id": "connector-candidate:" + str(len(candidates) + 1), "kind": KIND, "permission_id": rule["id"],
                "reference_component": rk[1], "generated_component": gk[1], "pin_map": pinmap,
                "scope": proof_scope(reference, generated, rc, gc)}
        if open_proof:
            cert["open_projection_sha256"] = open_proof["open_projection_sha256"]
        if additional:
            from .connector_ground_returns import prove_ground_returns
            additional.sort(key=lambda row: int(row["reference_contact"]))
            _, issues, projection = prove_ground_returns(open_reference, open_generated, rc, gc, rk, gk, pinmap, p, additional)
            if issues:
                diagnostics.append({"id": cert["id"], "status": "unresolved", "reasons": issues})
                continue
            cert.update(additional_ground_contacts=additional, projection_sha256=projection["projection_sha256"])
        _, errors, _ = check_connector_certificate(cert, permissions, reference, generated, rc, gc)
        # A weaker proof target never enlarges the action authorized by this
        # exact private-harness input. The strict checker above stays intact.
        if errors:
            connection_cert = {**cert, "id": "connector-connection-candidate:" + str(len(candidates) + 1),
                "kind": CONNECTION_KIND, "schema": CONNECTION_SCHEMA, "claims": dict(CONNECTION_CLAIMS)}
            _, connection_errors, _ = check_connector_connection_correspondence(
                connection_cert, permissions, reference, generated, rc, gc)
            if not connection_errors:
                cert, errors = connection_cert, []
            else:
                errors = list(dict.fromkeys([*errors, *("connection correspondence: " + issue for issue in connection_errors)]))
        if errors:
            diagnostics.append({"id": cert["id"], "status": "unresolved", "reasons": errors})
        else:
            candidates.append(cert); used.add(rk)
    return {"certificates": candidates, "diagnostics": diagnostics, "states": len(candidates),
            "search_complete": not diagnostics, "search_scope": "unique direct-core-contact signatures; no intermediate or joint-interface search"}


__all__ = ["check_connector_certificate", "check_connector_connection_correspondence",
           "propose_connector_certificates", "resolve_connector", "proof_scope", "CONNECTION_KIND"]

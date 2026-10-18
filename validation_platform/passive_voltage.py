"""Bounded voltage obligations for explicitly selected abstract passives.

Ratings describe the selected procurement constraint, not a verified physical
manufacturer part. Only actual declared rail nets establish static potentials.
A logic domain label never establishes the voltage of its signal network.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from .pin_contracts import decimal_number, evidence_errors


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


from .static_voltages import rail_potential, resolved_difference


def check_passive_voltage(component: Any, pins: Mapping, contract: Mapping, domains: Mapping, *,
                          logic_net_proofs: Mapping | None = None, static_context=None) -> list[dict]:
    """Check differential stress without inventing passive-pin input limits."""
    cid = component.key[1]
    rows = []

    def add(rule: str, status: str, message: str, **detail: Any) -> None:
        rows.append({"rule": rule, "status": status, "component": cid, "pin": None,
                     "message": message, **detail})

    obligation = _map(_map(contract.get("logical_terminal_contract")).get("voltage_obligation"))
    capacitor = component.type == "capacitor"
    kind = "capacitor_terminal_difference" if capacitor else "connector_contact_insulation"
    rule = "CAPACITOR_VOLTAGE_RATING" if capacitor else "CONNECTOR_INSULATION_RATING"
    selection = _map(obligation.get("selection"))
    specification = _map(selection.get("specification"))
    parameters = _map(obligation.get("parameters"))
    mapping = _map(obligation.get("terminal_mapping"))
    declared_mapping = specification.get("terminal_mapping")
    if declared_mapping is None:
        declared_mapping = contract.get("logical_pin_mapping")
    if declared_mapping is None and set(mapping) == set(mapping.values()):
        declared_mapping = {pid: pid for pid in mapping}
    try:
        selected = (selection.get("kind") == "frozen_abstract_design_selection" and
            len(selection.get("plan_sha256", "")) == 64 and bool(selection.get("locator")) and
            selection.get("specification_sha256") == _hash(specification) and
            _map(specification.get("parameters")) == parameters and
            _map(specification.get("device")).get("type") == component.type and
            _map(specification.get("device")).get("is_abstract") is True and
            _map(specification.get("device")).get("id") is None)
    except (ValueError, TypeError):
        selected = False
    template_refs = obligation.get("template_evidence")
    if (obligation.get("kind") != kind or not selected or evidence_errors(template_refs) or
            any(_map(ref).get("source_kind") != "template" for ref in template_refs or []) or
            set(mapping) != set(pins) or len(set(mapping.values())) != len(pins) or declared_mapping != mapping):
        add(rule, "unresolved", "Voltage obligation requires a complete logical terminal map, template evidence and frozen abstract selection")
        return rows
    scope = {"rating_basis": "required_rating_of_selected_abstract_part",
             "selection_plan_sha256": selection["plan_sha256"],
             "selection_locator": selection["locator"],
             "scope": "selected static scenario; no transient, AC, creepage or physical part qualification claim"}
    parameter = "rated_voltage_v" if capacitor else "rated_contact_to_contact_voltage_v"
    rating = decimal_number(parameters.get(parameter))
    if type(parameters.get(parameter)) not in {int, float} or rating is None or rating <= 0:
        add(rule, "unresolved" if parameters.get(parameter) is None else "fail",
            "Selected component has no established finite positive continuous voltage rating", required_parameter=parameter, **scope)
        return rows
    if capacitor and (set(mapping.values()) != {"a", "b"} or len(pins) != 2):
        add(rule, "unresolved", "Capacitor requires exactly the two declared a/b terminals", **scope)
        return rows
    if capacitor:
        polarization, positive = parameters.get("polarization"), parameters.get("positive_port")
        if (polarization not in {"non_polarized", "polarized"} or
                polarization == "non_polarized" and positive is not None or
                polarization == "polarized" and positive not in {"a", "b"}):
            add("CAPACITOR_POLARITY", "unresolved", "Explicit consistent polarization and positive terminal are required", **scope)
            return rows
    active = {pid: pin for pid, pin in pins.items() if pin.net is not None}
    if capacitor and len(active) != 2:
        add(rule, "unresolved", "Both capacitor terminals need actual net potentials", **scope)
        return rows
    if not capacitor and len(active) < 2:
        add(rule, "not_applicable", "Fewer than two connected contacts impose no contact-pair voltage in this selected static scenario", **scope)
        return rows
    potentials = {pid: rail_potential(pin.net[1], domains, context=static_context) for pid, pin in active.items()}
    # Application-level direct-interface evidence is deliberately limited to
    # transparent connector contacts. A capacitor on a signal changes loading
    # and cannot inherit this proof or treat the signal domain as a constant.
    if not capacitor:
        for pid, pin in active.items():
            proofs = _map(logic_net_proofs).get(pin.net[1], [])
            if potentials[pid]["status"] != "pass" and isinstance(proofs, list) and len(proofs) == 1:
                proof = _map(proofs[0])
                if proof.get("status") == "pass" and proof.get("rule") == "LOGIC_PASSIVE_INTERCONNECT" and proof.get("net") == pin.net[1]:
                    potentials[pid] = dict(proof)

    def interval(potential: Mapping) -> tuple | None:
        if potential.get("proof_path") == "reviewed_static_direct_interface_application":
            window = _map(potential.get("application_receiver_window_v"))
            low, high = decimal_number(window.get("minimum")), decimal_number(window.get("maximum"))
        else:
            low, high = decimal_number(potential.get("minimum_v")), decimal_number(potential.get("maximum_v"))
        return (low, high) if low is not None and high is not None and low <= high else None
    differences, missing = [], []
    for index, a in enumerate(sorted(active)):
        for b in sorted(active)[index + 1:]:
            if active[a].net == active[b].net:
                delta_min = delta_max = difference = decimal_number(0)
            elif static_context is not None:
                resolved = resolved_difference(static_context, active[a].net[1], active[b].net[1])
                if resolved["status"] != "pass":
                    missing.append([a, b]); continue
                delta_min, delta_max = decimal_number(resolved["minimum_v"]), decimal_number(resolved["maximum_v"])
                difference = max((delta_min, delta_max), key=lambda x: x.copy_abs())
            else:
                pa, pb = potentials[a], potentials[b]
                ia, ib = interval(pa), interval(pb)
                if pa["status"] != "pass" or pb["status"] != "pass" or pa["reference_net"] != pb["reference_net"] or ia is None or ib is None:
                    missing.append([a, b])
                    continue
                delta_min, delta_max = ia[0] - ib[1], ia[1] - ib[0]
                difference = max((delta_min, delta_max), key=lambda x: x.copy_abs())
            application = any(potentials[p].get("proof_path") == "reviewed_static_direct_interface_application" for p in (a, b))
            differences.append({"pins": [a, b], "difference_v": str(difference), "absolute_difference_v": str(difference.copy_abs()),
                                "minimum_difference_v": str(delta_min), "maximum_difference_v": str(delta_max),
                                "basis": "reviewed_static_application_receiver_window" if application else "numeric_static_potential_or_host_guarantee"})
    exceeded = [d for d in differences if decimal_number(d["absolute_difference_v"]) > rating]
    status = "fail" if exceeded else "unresolved" if missing else "pass"
    add(rule, status, "Compare selected continuous rating with same-reference static differences or an independently supported direct-interface application window",
        rated_voltage_v=str(rating), terminal_potentials=potentials, comparisons=differences,
        unresolved_pairs=missing, exceeded_pairs=exceeded, **scope)
    if capacitor and parameters["polarization"] == "polarized":
        if not differences:
            add("CAPACITOR_POLARITY", "unresolved", "Terminal polarity cannot be established from unknown potentials", **scope)
        else:
            pair = differences[0]
            oriented = (decimal_number(pair["minimum_difference_v"]) if mapping[pair["pins"][0]] == positive
                        else decimal_number(pair["maximum_difference_v"]).copy_negate())
            add("CAPACITOR_POLARITY", "pass" if oriented >= 0 else "fail", "Selected positive terminal must not be below the negative terminal",
                positive_port=positive, positive_minus_negative_v=str(oriented), **scope)
    return rows


__all__ = ["check_passive_voltage", "rail_potential"]

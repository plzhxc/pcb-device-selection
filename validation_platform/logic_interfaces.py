"""Finite static logic checks; no waveform, driver-subtype or net-name guesses.

An external host is an explicit frozen input boundary, not an inferred MCU.
Reviewed manufacturer application examples provide a separate proof path from
numeric operating limits. They never manufacture an undocumented Vin limit.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .pin_contracts import decimal_number, evidence_errors, evidence_field_scope_errors

HOST_FIELDS = tuple("host_logic_" + field for field in (
    "voh_min_v", "vol_max_v", "drive_min_v", "drive_max_v", "vih_min_v", "vil_max_v",
    "input_min_v", "input_max_v", "input_current_max_a"))


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def logic_supply_context(signal_domain: Any, supply_domain: Any, reference_domain: Any,
                         domains: Mapping) -> tuple[dict, list[str]]:
    """Validate a source-selected supply, never search for equal-voltage rails.

The caller must establish ``supply_domain`` from an authenticated named supply
and its actual physical pin. A separate signal domain is metadata: this helper
neither gives it a net nor connects any signal to the supply. Equal nominal
voltages are only a consistency check after the supply identity was selected.
"""
    errors = []
    for label, identifier in (("signal", signal_domain), ("supply", supply_domain),
                              ("reference", reference_domain)):
        if not isinstance(identifier, str) or not identifier or identifier not in domains:
            errors.append(label + " domain must be explicitly declared")
    if errors:
        return {}, errors
    from .static_voltages import domains_have_profiles
    if domains_have_profiles(domains):
        return {}, ["STATIC_PROFILE_UNSUPPORTED: logic interface needs joint source-relative interval thresholds; nominal fallback is forbidden"]
    signal, supply, reference = (_map(domains[x]) for x in (signal_domain, supply_domain, reference_domain))
    for label, declaration in (("signal", signal), ("supply", supply), ("reference", reference)):
        if evidence_errors(declaration.get("evidence"), allow_input=True):
            errors.append(label + " domain requires source/input evidence")
    voltage, nominal = decimal_number(supply.get("voltage_v")), decimal_number(signal.get("voltage_v"))
    if voltage is None or voltage <= 0 or nominal is None or nominal != voltage:
        errors.append("signal nominal voltage must match the independently selected actual supply")
    if signal.get("reference") != reference_domain or supply.get("reference") != reference_domain:
        errors.append("signal and named supply must retain the same explicit reference domain")
    if decimal_number(reference.get("voltage_v")) != 0 or reference.get("reference") is not None:
        errors.append("reference must be an actual zero domain without a parent reference")
    supply_net, reference_net = supply.get("net"), reference.get("net")
    if (not isinstance(supply_net, str) or not supply_net or
            not isinstance(reference_net, str) or not reference_net or supply_net == reference_net):
        errors.append("named supply and zero reference require distinct actual connected nets")
    if signal.get("net") is not None and signal.get("net") != supply_net:
        errors.append("signal context is already associated with a different actual net")
    if errors:
        return {}, errors
    return {"signal_domain": signal_domain, "supply_domain": supply_domain,
            "reference_domain": reference_domain, "supply_net": supply_net,
            "reference_net": reference_net, "supply_voltage_v": str(voltage)}, []


def documented_logic_direction(fact: Mapping) -> str | None:
    """Known input/output direction is independent of unknown drive subtype."""
    role = fact.get("semantic_role")
    direction = "input" if role in {"logic_in", "clock_in"} else "output" if role in {"logic_out", "clock_out"} else None
    levels = _map(fact.get("logic_levels"))
    if direction != levels.get("direction") or not levels.get("source_rule_id"):
        return None
    refs = _map(fact.get("field_evidence")).get("logic_levels", levels.get("evidence"))
    if evidence_errors(refs) or evidence_field_scope_errors(refs, ["logic_levels"]):
        return None
    return direction


def check_logic_pair(driver: Mapping, receiver: Mapping, *, same_reference: bool | None,
                     application_supported: bool = False) -> list[dict]:
    """Compare supplied numeric guarantees; evidence authentication is upstream.

``application_supported`` must come from the bounded reviewed-profile checker,
not from a model verdict. It replaces only missing operating-window proof, never
logic thresholds, reference identity, load bounds or a numeric contradiction.
"""
    rows = []
    def add(rule: str, status: str, message: str, **details: Any) -> None:
        rows.append({"rule": rule, "status": status, "message": message, **details})
    if same_reference is not True:
        add("LOGIC_REFERENCE", "fail" if same_reference is False else "unresolved", "Logic endpoints require the same actual reference")
        return rows
    for rule, output_key, input_key, op in (
            ("LOGIC_HIGH", "voh_min_v", "vih_min_v", "gte"),
            ("LOGIC_LOW", "vol_max_v", "vil_max_v", "lte")):
        output, threshold = decimal_number(driver.get(output_key)), decimal_number(receiver.get(input_key))
        if output is None or threshold is None:
            add(rule, "unresolved", "A documented driver guarantee or receiver threshold is missing")
        else:
            good = output >= threshold if op == "gte" else output <= threshold
            add(rule, "pass" if good else "fail", "Driver guarantee compared with receiver threshold",
                driver=str(output), receiver=str(threshold), relation=op)
    maximum = decimal_number(driver.get("maximum_output_current_a"))
    load = decimal_number(receiver.get("input_current_max_a"))
    if driver.get("maximum_output_current_a") is not None:
        if maximum is None or maximum < 0 or load is None or load < 0:
            add("LOGIC_OUTPUT_LOAD", "unresolved", "The documented output guarantee requires a finite nonnegative receiver load bound")
        else:
            add("LOGIC_OUTPUT_LOAD", "pass" if load <= maximum else "fail", "Receiver static load must stay within the documented output bound",
                maximum_a=str(maximum), input_current_max_a=str(load))
    drive_low, drive_high = decimal_number(driver.get("drive_min_v")), decimal_number(driver.get("drive_max_v"))
    input_low, input_high = decimal_number(receiver.get("operating_min_v")), decimal_number(receiver.get("operating_max_v"))
    if all(value is not None for value in (drive_low, drive_high, input_low, input_high)):
        good = input_low <= drive_low <= drive_high <= input_high
        add("LOGIC_OPERATING_WINDOW", "pass" if good else "fail", "Entire declared driver envelope must fit documented receiver operating limits",
            proof_path="numeric_operating_limits")
    elif application_supported:
        add("LOGIC_OPERATING_WINDOW", "pass", "Actual interface satisfies the separately reviewed manufacturer application profile; no numeric device limit is inferred",
            proof_path="reviewed_manufacturer_application_profile")
    else:
        add("LOGIC_OPERATING_WINDOW", "unresolved", "Complete operating-envelope evidence or an applicable independently reviewed interface profile is missing")
    return rows


def check_external_logic(fact: Mapping, binding: Mapping, *, domains: Mapping,
                         supplier_part: str | None, input_sha256: str | None,
                         pin_net: str | None, other_active_pins: list) -> list[dict]:
    """Check one core pin directly wired to a declared external logic endpoint.

The adapter supplies ``external_logic_ports`` and ``external_rail_ports`` from
actual compilation members. Their declared domains never imply extra wires.
"""
    rows = []
    def add(rule: str, status: str, message: str, **detail: Any) -> None:
        rows.append({"rule": rule, "status": status, "message": message, **detail})
    direction = documented_logic_direction(fact)
    if direction is None:
        add("LOGIC_LEVEL_SOURCE", "unresolved", "Reviewed direction-specific operating logic guarantees are unavailable")
        return rows
    levels = _map(fact.get("logic_levels"))
    did = binding.get("domain"); domain = _map(domains.get(did))
    rid = domain.get("reference"); reference = _map(domains.get(rid))
    logic_ports = binding.get("external_logic_ports")
    rail_ports = binding.get("external_rail_ports")
    logic_ports = logic_ports if isinstance(logic_ports, list) else []
    rail_ports = rail_ports if isinstance(rail_ports, list) else []
    if not pin_net or len(logic_ports) != 1:
        add("LOGIC_EXTERNAL_ENDPOINT", "unresolved", "An explicit actual external logic endpoint is missing")
        return rows
    if any(_map(port).get("net") != pin_net or _map(port).get("domain") != did for port in logic_ports):
        add("LOGIC_EXTERNAL_ENDPOINT", "fail", "Actual external logic endpoint and core pin domain disagree")
        return rows
    if other_active_pins:
        add("LOGIC_DIRECT_TOPOLOGY", "unresolved", "A direct external interface proof cannot include additional active or unknown endpoints", pins=other_active_pins)
        return rows
    profile = _map(fact.get("application_interface_profile"))
    refs = _map(fact.get("field_evidence")).get("application_interface_profile", profile.get("evidence"))
    # Only the evaluated source rule may associate a separate signal domain
    # with its named physical supply. A model-written domain name or the same
    # nominal voltage is insufficient. Older same-domain facts remain valid.
    profile_context = (profile.get("profile") == "same_supply_direct_spi" and bool(profile.get("source_rule_id"))
        and profile.get("capability") == binding.get("usage") and profile.get("direction") == direction
        and profile.get("signal_domain", profile.get("supply_domain")) == did
        and profile.get("reference_domain") == rid
        and isinstance(profile.get("physical_supply_pin"), str) and bool(profile.get("physical_supply_pin"))
        and isinstance(profile.get("physical_reference_pin"), str) and bool(profile.get("physical_reference_pin"))
        and not evidence_errors(refs) and not evidence_field_scope_errors(refs, ["application_interface_profile"]))
    supply_did = profile.get("supply_domain") if profile_context else did
    context, context_errors = logic_supply_context(did, supply_did, rid, domains)
    if context_errors:
        add("LOGIC_REFERENCE", "unresolved", "Actual named supply and zero-reference context is incomplete",
            reasons=context_errors, signal_domain=did, supply_domain=supply_did,
            association_basis="source_reviewed_named_supply" if profile_context else "same_domain")
        return rows
    rail_ok = all(any(_map(port).get("domain") == key and _map(port).get("net") == _map(domains[key]).get("net")
                      for port in rail_ports) for key in (supply_did, rid))
    if not rail_ok:
        add("LOGIC_REFERENCE", "fail", "External host supply/reference endpoints do not both reach the actual internal rails")
        return rows
    host = {}; missing = []
    requirements = _map(binding.get("requirements"))
    for field in HOST_FIELDS:
        selection = _map(requirements.get(field)); authentication = _map(selection.get("selection_authentication"))
        value = decimal_number(selection.get("value"))
        if (value is None or evidence_errors(selection.get("evidence"), allow_input=True)
                or authentication.get("method") != "exact_frozen_input_selection"
                or authentication.get("field") != "requirements." + field
                or authentication.get("supplier_part") != supplier_part or authentication.get("applies_to") != "all_instances"
                or not input_sha256 or authentication.get("input_sha256") != input_sha256):
            missing.append(field)
        else:
            host[field.removeprefix("host_logic_")] = value
    if missing:
        add("LOGIC_HOST_BOUNDARY", "unresolved", "External host guarantees must be explicit authenticated frozen input selections", fields=missing)
        return rows
    if (not host["drive_min_v"] <= host["vol_max_v"] < host["voh_min_v"] <= host["drive_max_v"]
            or not host["input_min_v"] <= host["vil_max_v"] < host["vih_min_v"] <= host["input_max_v"]
            or host["input_current_max_a"] < 0):
        add("LOGIC_HOST_BOUNDARY", "fail", "Host guarantees have inconsistent intervals, thresholds or current")
        return rows
    application_supported = False
    if profile:
        voltage = decimal_number(context.get("supply_voltage_v"))
        supported = (profile_context and profile.get("supply_domain") == supply_did
            and voltage is not None and voltage == decimal_number(profile.get("supply_voltage_v"))
            and profile.get("reference_domain") == rid)
        if supported:
            application_supported = (0 <= host["drive_min_v"] <= host["drive_max_v"] <= voltage
                                     and host["input_min_v"] <= 0 <= voltage <= host["input_max_v"])
            add("LOGIC_APPLICATION_PROFILE", "pass" if application_supported else "fail",
                "Reviewed direct same-supply SPI example requires a rail-bounded host driver and host receiver spanning the common rails",
                source_rule_id=profile["source_rule_id"], supply_context=context,
                association_basis="source_reviewed_named_supply",
                proof_scope="static manufacturer application pattern, not undocumented numeric Vin ratings")
        else:
            add("LOGIC_APPLICATION_PROFILE", "unresolved", "Application profile identity, source scope or actual context does not match")
    if direction == "input":
        driver, receiver = host, levels
    else:
        driver = levels
        receiver = {**host, "operating_min_v": host["input_min_v"], "operating_max_v": host["input_max_v"]}
    rows.extend(check_logic_pair(driver, receiver, same_reference=True, application_supported=application_supported))
    if rows and all(row["status"] in {"pass", "not_applicable"} for row in rows):
        if direction == "input":
            add("LOGIC_PASSIVE_INTERCONNECT", "pass", "An explicit host drive guarantee bounds this actual direct signal network",
                net=pin_net, reference_net=reference["net"], proof_path="authenticated_host_drive_envelope",
                minimum_v=str(host["drive_min_v"]), maximum_v=str(host["drive_max_v"]),
                scope="selected static host guarantee; no dynamic overshoot proof")
        elif application_supported:
            add("LOGIC_PASSIVE_INTERCONNECT", "pass", "The independently supported direct-interface application also covers transparent passive contacts within its selected receiver window",
                net=pin_net, reference_net=reference["net"], proof_path="reviewed_static_direct_interface_application",
                application_receiver_window_v={"minimum": str(host["input_min_v"]), "maximum": str(host["input_max_v"])},
                source_rule_id=profile["source_rule_id"], evidence=refs,
                device_voltage_limits_inferred=False,
                scope="application-level static interconnect compatibility; not a numerical device output envelope, transient or timing guarantee")
    return rows


__all__ = ["HOST_FIELDS", "logic_supply_context", "documented_logic_direction", "check_logic_pair", "check_external_logic"]

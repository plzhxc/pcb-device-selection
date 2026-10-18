"""Formal net/value relation extraction for PCBAgent metrics.

Active metrics use only endpoint relations:
- core IC pin <-> core IC pin;
- core IC pin <-> SYS:PWR / SYS:GND / SYS:NC.

Intermediate non-IC parts are ignored by the line metric.  R/C/L values are
checked separately, keyed by their own endpoint relation, so a right value on a
wrong network does not count as correct.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from validation_platform.connect_abstraction import (
    _connect_type,
    _device_identity_info,
    _pin_name_for,
    _strict_pin_key,
    _token_key,
    build_ic_instance_labels,
    build_identity_alias,
    classify_net,
    extract_connects,
)
from validation_platform.device_conduction_rules import conductive_groups
from validation_platform.graph import parse_value


FORMAL_SYS_PREFIXES = ("SYS:PWR:", "SYS:GND", "SYS:NC")
FORMAL_QUANTITY_TYPES = frozenset({"IC", "R", "C", "L"})
FORMAL_PATH_TYPES = frozenset({"WIRE", "R", "C", "L"})


def formal_connection_counts(gen_netlist: Dict[str, Any], ref_netlist: Dict[str, Any]) -> Dict[str, Any]:
    """Count architecture-stable endpoint relation matches.

    Direct wires and R/C/L paths belong to the deterministic fixture contract.
    Relations that only exist through interchangeable D/Q/SW/etc. path
    devices remain diagnostic; requiring their exact internal topology would
    contradict the path-device abstraction used by component acceptance.
    """
    identity_alias = build_identity_alias(gen_netlist, ref_netlist)
    instance_labels = build_ic_instance_labels(
        gen_netlist,
        ref_netlist,
        identity_alias=identity_alias,
    )
    gen_counter, gen_excluded, gen_diagnostic = _formal_connection_counter_parts(
        gen_netlist,
        identity_alias=identity_alias,
        instance_labels=instance_labels.get("gen") or {},
    )
    ref_counter, ref_excluded, ref_diagnostic = _formal_connection_counter_parts(
        ref_netlist,
        identity_alias=identity_alias,
        instance_labels=instance_labels.get("ref") or {},
    )
    matched = sum(min(ref_count, gen_counter.get(key, 0)) for key, ref_count in ref_counter.items())
    return {
        "matched": matched,
        "ref_total": sum(ref_counter.values()),
        "gen_total": sum(gen_counter.values()),
        "missing": _counter_deficit(gen_counter, ref_counter, limit=40),
        "extra": _counter_surplus(gen_counter, ref_counter, limit=40),
        "diagnostic_ref_total": sum(ref_diagnostic.values()),
        "diagnostic_gen_total": sum(gen_diagnostic.values()),
        "excluded_ref_total": len(ref_excluded),
        "excluded_gen_total": len(gen_excluded),
        "excluded_ref_by_path_type": _excluded_path_reason_counts(ref_excluded),
        "excluded_gen_by_path_type": _excluded_path_reason_counts(gen_excluded),
    }


def formal_connection_counter(
    netlist: Dict[str, Any],
    *,
    identity_alias: Optional[Dict[str, str]] = None,
    instance_labels: Optional[Dict[str, str]] = None,
) -> Counter:
    """Build formal IC-centric relations over direct and R/C/L paths only."""
    formal, _, _ = _formal_connection_counter_parts(
        netlist,
        identity_alias=identity_alias,
        instance_labels=instance_labels,
    )
    return formal


def _formal_connection_counter_parts(
    netlist: Dict[str, Any],
    *,
    identity_alias: Optional[Dict[str, str]] = None,
    instance_labels: Optional[Dict[str, str]] = None,
) -> Tuple[Counter, Dict[str, set[str]], Counter]:
    """Return formal, excluded-path diagnostics, and all relation counters."""

    identity_alias = identity_alias or {}
    instance_labels = instance_labels or {}

    # Use the existing path extractor for IC-to-IC and IC-to-power/GND through
    # local devices, then collapse away the path elements.
    extracted = extract_connects(
        netlist,
        identity_alias=identity_alias,
        instance_labels=instance_labels,
    )
    counter: Counter = Counter()
    diagnostic: Counter = Counter()
    unstable_types_by_key: Dict[str, set[str]] = defaultdict(set)
    for item in extracted.get("connects") or []:
        key = _formal_relation_key(item.get("strict_endpoints") or [])
        if not key:
            continue
        diagnostic[key] = 1
        path_types = {
            str(element or "").split("@", 1)[0].upper()
            for element in (item.get("path") or [])
            if str(element or "").strip()
        } or {"WIRE"}
        if path_types <= FORMAL_PATH_TYPES:
            counter[key] = 1
        else:
            unstable_types_by_key[key].update(
                path_types - FORMAL_PATH_TYPES
            )

    # Existing path extraction intentionally drops NC pins; add those direct
    # IC-pin-to-NC relations explicitly.
    for key in _nc_relation_keys(netlist, identity_alias=identity_alias, instance_labels=instance_labels):
        counter[key] = 1
        diagnostic[key] = 1
    excluded = {
        key: types
        for key, types in unstable_types_by_key.items()
        if key not in counter
    }
    return counter, excluded, diagnostic


def _excluded_path_reason_counts(excluded: Dict[str, set[str]]) -> Dict[str, int]:
    reasons: Counter = Counter()
    for path_types in excluded.values():
        reason = "+".join(sorted(path_types)) or "OTHER"
        reasons[reason] += 1
    return dict(sorted(reasons.items()))


def formal_value_counts(gen_netlist: Dict[str, Any], ref_netlist: Dict[str, Any]) -> Dict[str, Any]:
    """Count stable R/C/L value matches keyed by endpoint relation.

    Only relations anchored to core IC pins, power, ground, or NC are formal.
    Anonymous local-net labels and connector/path-device identities are
    implementation details: they remain diagnostic, but cannot be compared
    reliably after path abstraction.
    """
    identity_alias = build_identity_alias(gen_netlist, ref_netlist)
    instance_labels = build_ic_instance_labels(
        gen_netlist,
        ref_netlist,
        identity_alias=identity_alias,
    )
    gen_counter = formal_value_counter(
        gen_netlist,
        identity_alias=identity_alias,
        instance_labels=instance_labels.get("gen") or {},
    )
    ref_counter = formal_value_counter(
        ref_netlist,
        identity_alias=identity_alias,
        instance_labels=instance_labels.get("ref") or {},
    )
    gen_formal, gen_excluded = _partition_formal_value_counter(gen_counter)
    ref_formal, ref_excluded = _partition_formal_value_counter(ref_counter)
    matched = sum(
        min(ref_count, gen_formal.get(key, 0))
        for key, ref_count in ref_formal.items()
    )
    return {
        "matched": matched,
        "ref_total": sum(ref_formal.values()),
        "gen_total": sum(gen_formal.values()),
        "missing": _counter_deficit(gen_formal, ref_formal, limit=40),
        "extra": _counter_surplus(gen_formal, ref_formal, limit=40),
        "diagnostic_ref_total": sum(ref_counter.values()),
        "diagnostic_gen_total": sum(gen_counter.values()),
        "excluded_ref_total": sum(ref_excluded.values()),
        "excluded_gen_total": sum(gen_excluded.values()),
        "excluded_ref_by_reason": _excluded_value_reason_counts(ref_excluded),
        "excluded_gen_by_reason": _excluded_value_reason_counts(gen_excluded),
    }


def _partition_formal_value_counter(counter: Counter) -> Tuple[Counter, Counter]:
    formal: Counter = Counter()
    excluded: Counter = Counter()
    for key, count in counter.items():
        target = formal if _is_formal_value_key(str(key)) else excluded
        target[key] += count
    return formal, excluded


def _is_formal_value_key(key: str) -> bool:
    """Return whether a value relation has architecture-stable endpoints."""

    return not any(
        token in str(key or "")
        for token in ("NET:", "DEV:", "UNBOUND")
    )


def _excluded_value_reason_counts(counter: Counter) -> Dict[str, int]:
    reasons: Counter = Counter()
    for key, count in counter.items():
        text = str(key or "")
        if "DEV:" in text:
            reason = "path_device_endpoint"
        elif "NET:" in text:
            reason = "anonymous_or_protocol_local_net"
        elif "UNBOUND" in text:
            reason = "unbound_endpoint"
        else:
            reason = "non_formal_endpoint"
        reasons[reason] += int(count)
    return dict(sorted(reasons.items()))


def formal_value_counter(
    netlist: Dict[str, Any],
    *,
    identity_alias: Optional[Dict[str, str]] = None,
    instance_labels: Optional[Dict[str, str]] = None,
) -> Counter:
    """Build value keys: component kind + value bucket + endpoint relation."""
    identity_alias = identity_alias or {}
    instance_labels = instance_labels or {}
    devices = _formal_devices(netlist, identity_alias=identity_alias, instance_labels=instance_labels)
    net_endpoints = _net_formal_endpoints(devices)
    counter: Counter = Counter()
    for dev in devices.values():
        kind = str(dev.get("type") or "")
        if kind not in {"R", "C", "L"}:
            continue
        value = _component_value_bucket(dev, kind)
        if value is None:
            continue
        for side_a, side_b in _component_endpoint_sides(dev, net_endpoints):
            if not side_a and not side_b:
                continue
            relation = _side_relation_key(side_a, side_b)
            counter[f"{kind}@{value}@{relation}"] += 1
    return counter


def component_type_deviation(gen_netlist: Dict[str, Any], ref_netlist: Dict[str, Any]) -> Dict[str, Any]:
    """Return formal IC/R/C/L count deltas plus full type diagnostics.

    Connection/path devices are intentionally abstracted out of the formal
    topology metric and are not part of the deterministic fixture contract.
    Their counts remain visible in ``by_type`` and the diagnostic totals, but
    only IC/R/C/L contribute to the acceptance numerator.
    """
    identity_alias = build_identity_alias(gen_netlist, ref_netlist)
    gen_devices = _formal_devices(gen_netlist, identity_alias=identity_alias, instance_labels={})
    ref_devices = _formal_devices(ref_netlist, identity_alias=identity_alias, instance_labels={})
    gen_types = Counter(dev["type"] for dev in gen_devices.values())
    ref_types = Counter(dev["type"] for dev in ref_devices.values())
    by_type: Dict[str, Dict[str, int]] = {}
    formal_delta = 0
    diagnostic_delta = 0
    excluded_path_delta = 0
    for kind in sorted(set(gen_types) | set(ref_types)):
        gen_count = int(gen_types.get(kind, 0))
        ref_count = int(ref_types.get(kind, 0))
        delta = abs(gen_count - ref_count)
        diagnostic_delta += delta
        evaluated = kind in FORMAL_QUANTITY_TYPES
        if evaluated:
            formal_delta += delta
        else:
            excluded_path_delta += delta
        by_type[kind] = {
            "gen": gen_count,
            "ref": ref_count,
            "abs_delta": delta,
            "evaluated": evaluated,
        }

    gen_ics = Counter(
        str(dev.get("identity") or "")
        for dev in gen_devices.values()
        if dev.get("type") == "IC" and dev.get("compare_eligible", True)
    )
    ref_ics = Counter(
        str(dev.get("identity") or "")
        for dev in ref_devices.values()
        if dev.get("type") == "IC" and dev.get("compare_eligible", True)
    )
    missing = _identity_deficit(gen_ics, ref_ics)
    extra = _identity_surplus(gen_ics, ref_ics)
    return {
        "type_abs_delta_sum": formal_delta,
        "diagnostic_type_abs_delta_sum": diagnostic_delta,
        "excluded_path_type_abs_delta_sum": excluded_path_delta,
        "evaluated_types": sorted(FORMAL_QUANTITY_TYPES),
        "by_type": by_type,
        "gen_total": len(gen_devices),
        "ref_total": len(ref_devices),
        "core_ic_complete": not missing and not extra,
        "core_ic_missing": missing,
        "core_ic_extra": extra,
    }


def _formal_relation_key(endpoints: Iterable[Any]) -> str:
    labels = sorted(str(endpoint or "") for endpoint in endpoints if endpoint)
    if len(labels) != 2:
        return ""
    a, b = labels
    if _is_artificial_endpoint(a) or _is_artificial_endpoint(b):
        return ""
    if not (a.startswith("IC:") or b.startswith("IC:")):
        return ""
    if a.startswith("SYS:IO:") or b.startswith("SYS:IO:"):
        return ""
    allowed = lambda item: item.startswith("IC:") or item.startswith(FORMAL_SYS_PREFIXES)
    if not (allowed(a) and allowed(b)):
        return ""
    return f"{a} -- {b}"


def _nc_relation_keys(
    netlist: Dict[str, Any],
    *,
    identity_alias: Dict[str, str],
    instance_labels: Dict[str, str],
) -> List[str]:
    devices = _formal_devices(netlist, identity_alias=identity_alias, instance_labels=instance_labels, keep_nc=True)
    keys: List[str] = []
    for dev in devices.values():
        if dev.get("type") != "IC":
            continue
        identity = str(dev.get("instance_identity") or dev.get("identity") or "UNKNOWN")
        for pin, net in (dev.get("pins") or {}).items():
            if classify_net(net).get("code") != "NC":
                continue
            pin_name = _pin_name_for(dev, pin)
            endpoint = f"IC:{identity}:PIN:{_strict_pin_key(pin, net, pin_name)}"
            if not _is_artificial_endpoint(endpoint):
                keys.append(_side_relation_key([endpoint], ["SYS:NC"]))
    return keys


def _formal_devices(
    netlist: Dict[str, Any],
    *,
    identity_alias: Dict[str, str],
    instance_labels: Dict[str, str],
    keep_nc: bool = False,
) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for dev_id, entry in (netlist or {}).items():
        props = entry.get("props") or {}
        raw_pins = {
            str(pin): str(net).strip()
            for pin, net in (entry.get("pins") or {}).items()
            if str(net or "").strip()
        }
        pins = raw_pins if keep_nc else {
            pin: net for pin, net in raw_pins.items()
            if classify_net(net).get("code") != "NC"
        }
        dtype = _connect_type(props)
        identity, identity_source = _device_identity_info(props, dtype, identity_alias, pins=raw_pins)
        out[str(dev_id)] = {
            "id": str(dev_id),
            "props": props,
            "type": dtype,
            "identity": identity,
            "base_identity": identity,
            "instance_identity": instance_labels.get(str(dev_id), identity),
            "identity_source": identity_source,
            "pins": pins,
            "resolved_supplier": identity if re.fullmatch(r"C\d+", str(identity), flags=re.I) else "",
        }
    return out


def _net_formal_endpoints(devices: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    parent: Dict[str, str] = {}
    zero_edges: List[Tuple[str, str]] = []

    def find(net: str) -> str:
        parent.setdefault(net, net)
        if parent[net] != net:
            parent[net] = find(parent[net])
        return parent[net]

    def union(a: str, b: str) -> None:
        if not a or not b:
            return
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Do not globally merge 0-ohm resistors here.  A 0R link is still a
    # component whose value/location must be checked; treating it as a net
    # equivalence can pull unrelated IC endpoints into every capacitor on a rail
    # and corrupt the value metric.
    for dev in devices.values():
        if str(dev.get("type") or "") != "R" or not _is_zero_ohm_bridge(dev):
            continue
        pins = dev.get("pins") or {}
        for group in conductive_groups(dev):
            group_pins = list(group.pins)
            if len(group_pins) != 2:
                continue
            a_net = str(pins.get(group_pins[0]) or "").strip()
            b_net = str(pins.get(group_pins[1]) or "").strip()
            if a_net and b_net and a_net != b_net:
                zero_edges.append((a_net, b_net))

    direct: Dict[str, set] = defaultdict(set)
    for dev in devices.values():
        dev_type = str(dev.get("type") or "")
        identity = str(dev.get("instance_identity") or dev.get("identity") or "UNKNOWN")
        for pin, net in (dev.get("pins") or {}).items():
            role = classify_net(net)
            if role.get("class") == "power":
                direct[net].add(f"SYS:PWR:{role['code']}")
            elif role.get("class") == "gnd":
                direct[net].add("SYS:GND")
            elif role.get("code") == "NC":
                direct[net].add("SYS:NC")
            elif dev_type == "IC":
                pin_name = _pin_name_for(dev, pin)
                direct[net].add(f"IC:{identity}:PIN:{_strict_pin_key(pin, net, pin_name)}")
            elif dev_type not in {"R", "C", "L"} and _is_value_anchor_device(dev):
                resolved = str(dev.get("resolved_supplier") or "").strip()
                if resolved:
                    direct[net].add(f"DEV:{resolved}:PIN:{_value_dev_pin_key(dev, pin, net)}")
    for dev in devices.values():
        for net in (dev.get("pins") or {}).values():
            role = classify_net(net)
            if role.get("class") == "power":
                direct[net].add(f"SYS:PWR:{role['code']}")
            elif role.get("class") == "gnd":
                direct[net].add("SYS:GND")
            elif role.get("code") == "NC":
                direct[net].add("SYS:NC")

    by_root: Dict[str, set] = defaultdict(set)
    for net, values in direct.items():
        by_root[find(net)].update(values)
    out: Dict[str, List[str]] = {}
    for net in direct:
        out[net] = _reduce_value_endpoint_side(sorted(by_root[find(net)]))
    _add_local_zero_ohm_anchor_endpoints(out, zero_edges)
    return out


def _add_local_zero_ohm_anchor_endpoints(
    endpoints_by_net: Dict[str, List[str]],
    zero_edges: Iterable[Tuple[str, str]],
) -> None:
    """Expose IC/SYS anchors through local 0R links without merging rails.

    This is deliberately narrower than net-equivalence.  It only helps nets
    that currently have no formal anchor, such as connector-side speaker nets
    reached through 0R links.  Power/GND/IC nets already have anchors and are
    left unchanged, which prevents rail decoupling keys from being polluted by
    signal endpoints.
    """
    edges = list(zero_edges)
    if not edges:
        return

    def anchors(items: Iterable[str]) -> List[str]:
        return [item for item in items if item.startswith("IC:") or item.startswith("SYS:")]

    for _ in range(4):
        changed = False
        for a_net, b_net in edges:
            a_items = endpoints_by_net.get(a_net) or []
            b_items = endpoints_by_net.get(b_net) or []
            a_anchors = anchors(a_items)
            b_anchors = anchors(b_items)
            if not a_anchors and b_anchors:
                merged = _reduce_value_endpoint_side([*a_items, *b_anchors])
                if merged != a_items:
                    endpoints_by_net[a_net] = merged
                    changed = True
            if not b_anchors and a_anchors:
                merged = _reduce_value_endpoint_side([*b_items, *a_anchors])
                if merged != b_items:
                    endpoints_by_net[b_net] = merged
                    changed = True
        if not changed:
            break


def _is_zero_ohm_bridge(dev: Dict[str, Any]) -> bool:
    props = dev.get("props") or {}
    for raw in (
        str(props.get("value") or ""),
        str(props.get("deviceName") or props.get("DeviceName") or ""),
        str(props.get("deviceNotes") or ""),
    ):
        value = parse_value(raw, "R")
        if value is not None and abs(value) <= 0.5:
            return True
    return False


def _is_value_anchor_device(dev: Dict[str, Any]) -> bool:
    props = dev.get("props") or {}
    raw_type = str(props.get("deviceType") or props.get("device_type") or "").strip().lower()
    designator = str(props.get("designator") or "").strip().upper()
    device_text = " ".join(
        str(props.get(key) or "")
        for key in ("deviceName", "DeviceName", "deviceNotes", "value")
    ).upper()
    prefix_match = re.match(r"([A-Z]+)", designator)
    prefix = prefix_match.group(1) if prefix_match else ""
    return (
        raw_type in {"connector", "connectors", "terminal", "header", "usb_connector", "type_c"}
        or prefix in {"J", "JP", "P", "CON", "FPC", "FFC", "USB", "TYPEC"}
        or any(token in device_text for token in ("CONNECTOR", "CONN", "FPC", "FFC", "HEADER", "TYPE-C", "TYPE C"))
    )


def _value_dev_pin_key(dev: Dict[str, Any], pin: str, net: str) -> str:
    props = dev.get("props") or {}
    raw_type = str(props.get("deviceType") or props.get("device_type") or "").strip().lower()
    designator = str(props.get("designator") or "").strip().upper()
    device_text = " ".join(
        str(props.get(key) or "")
        for key in ("deviceName", "DeviceName", "deviceNotes", "value")
    ).upper()
    prefix_match = re.match(r"([A-Z]+)", designator)
    prefix = prefix_match.group(1) if prefix_match else ""
    if (
        raw_type in {"connector", "connectors", "esd_protection", "terminal", "header"}
        or prefix in {"J", "JP", "P", "CON", "FPC", "FFC", "USB", "TYPEC"}
        or any(token in device_text for token in ("CONNECTOR", "CONN", "FPC", "FFC", "HEADER", "TYPE-C", "TYPE C"))
    ):
        pin_token = re.sub(r"[^A-Z0-9]+", "_", str(pin or "").upper()).strip("_")
        if pin_token:
            return f"PIN_{pin_token}"
    pin_name = _pin_name_for(dev, pin)
    for raw in (pin_name, net):
        role = classify_net(raw)
        code = str(role.get("code") or "")
        if role.get("class") in {
            "usb",
            "reset",
            "boot",
            "clock",
            "sd",
            "spi",
            "uart",
            "i2c",
            "i2s",
            "analog",
            "pwm",
            "key",
            "gpio",
            "power",
            "gnd",
        } and code not in {"", "NC"}:
            if role.get("class") == "power":
                return "PWR"
            if role.get("class") == "gnd":
                return "GND"
            return code
    return _strict_pin_key(pin, net, pin_name)


def _is_power_pin_endpoint(endpoint: str) -> bool:
    match = re.search(r":PIN:([^|]+)$", str(endpoint or ""))
    if not match:
        return False
    token = re.sub(r"[^A-Z0-9]+", "_", match.group(1).upper()).strip("_")
    if token in {
        "PWR",
        "POWER",
        "VCC",
        "VDD",
        "VDDA",
        "VDDD",
        "VDDM",
        "VDDIO",
        "AVDD",
        "DVDD",
        "PVDD",
        "VIN",
        "VOUT",
        "VBUS",
        "VBAT",
        "VSYS",
    }:
        return True
    return bool(token.startswith(("VCC", "VDD", "AVDD", "DVDD", "PVDD", "VIN", "VOUT", "VBUS", "VBAT", "VSYS")))


def _is_ground_pin_endpoint(endpoint: str) -> bool:
    match = re.search(r":PIN:([^|]+)$", str(endpoint or ""))
    if not match:
        return False
    token = re.sub(r"[^A-Z0-9]+", "_", match.group(1).upper()).strip("_")
    if token in {
        "GND",
        "GROUND",
        "AGND",
        "DGND",
        "PGND",
        "SGND",
        "GNDA",
        "GNDD",
        "AU_GND",
        "AUDIO_GND",
        "POWER_GND",
        "VSS",
        "VSSA",
        "VSSD",
        "0V",
    }:
        return True
    return token.startswith(("GND_", "AGND_", "DGND_", "PGND_", "SGND_"))


def _reduce_value_endpoint_side(endpoints: Iterable[str]) -> List[str]:
    values = [str(item or "") for item in endpoints if item]
    if not values:
        return []
    reduced = []
    has_power = False
    has_ground = False
    for item in values:
        if item.startswith("SYS:PWR:") or item == "SYS:PWR" or _is_power_pin_endpoint(item):
            has_power = True
            continue
        if item == "SYS:GND" or _is_ground_pin_endpoint(item):
            has_ground = True
            continue
        reduced.append(item)
    # The formal value metric is anchored on core IC/system relations.  A net may
    # also touch connectors, keys, ESDs, or other non-core endpoint devices; those
    # identities are useful for review, but they should not make an otherwise
    # identical IC-to-rail value relation fail.
    if any(item.startswith("IC:") for item in reduced):
        reduced = [
            item for item in reduced
            if not item.startswith("DEV:") and not item.startswith("NET:")
        ]
        reduced = _collapse_ic_endpoint_aliases(reduced)
    if has_power and not reduced:
        return ["SYS:PWR"]
    if has_ground and not reduced:
        return ["SYS:GND"]
    if has_power:
        reduced.append("SYS:PWR")
    if has_ground:
        reduced.append("SYS:GND")
    return sorted(set(reduced))


def _collapse_ic_endpoint_aliases(endpoints: Iterable[str]) -> List[str]:
    values = [str(item or "") for item in endpoints if item]
    concrete = set()
    signal_aliases: List[Tuple[str, str, str]] = []
    passthrough = []
    for item in values:
        if not item.startswith("IC:") or ":PIN:" not in item:
            passthrough.append(item)
            continue
        identity, pin = item[3:].split(":PIN:", 1)
        if pin.startswith("SIG:"):
            signal_aliases.append((item, identity, pin.split("SIG:", 1)[1]))
        else:
            concrete.add((identity, pin))
            passthrough.append(item)
    for item, identity, pin in signal_aliases:
        if (identity, pin) not in concrete:
            passthrough.append(item)
    return passthrough


def _component_endpoint_sides(
    dev: Dict[str, Any],
    net_endpoints: Dict[str, List[str]],
) -> Iterable[Tuple[List[str], List[str]]]:
    pins = dev.get("pins") or {}
    for group in conductive_groups(dev):
        group_pins = list(group.pins)
        if len(group_pins) == 2:
            a_net = pins.get(group_pins[0], "")
            b_net = pins.get(group_pins[1], "")
            yield _endpoints_for_net(a_net, net_endpoints), _endpoints_for_net(b_net, net_endpoints)
            continue
        # Fallback for unusual packages: compare every formal net pair in the group.
        nets = [pins.get(pin, "") for pin in group_pins]
        for idx, a_net in enumerate(nets):
            for b_net in nets[idx + 1:]:
                yield _endpoints_for_net(a_net, net_endpoints), _endpoints_for_net(b_net, net_endpoints)


def _endpoints_for_net(net: str, net_endpoints: Dict[str, List[str]]) -> List[str]:
    endpoints = [item for item in net_endpoints.get(str(net or ""), []) if not _is_artificial_endpoint(item)]
    if endpoints:
        return _reduce_value_endpoint_side(endpoints)
    role = classify_net(net)
    code = role.get("code") or "UNKNOWN"
    if role.get("class") in {"power", "gnd"} or code == "NC":
        if role.get("class") == "power":
            return ["SYS:PWR"]
        if role.get("class") == "gnd":
            return ["SYS:GND"]
        return ["SYS:NC"]
    if role.get("class") not in {"local", "none"}:
        return [f"NET:{code}"]
    return []


def _component_value_bucket(dev: Dict[str, Any], kind: str) -> Optional[str]:
    props = dev.get("props") or {}
    for raw in (
        str(props.get("value") or ""),
        str(props.get("deviceName") or props.get("DeviceName") or ""),
        str(props.get("deviceNotes") or ""),
    ):
        value = parse_value(raw, kind)
        if value is not None and value > 0:
            return _value_bucket(value)
    return None


def _value_bucket(value: float) -> str:
    import math

    if value == 0:
        return "0"
    return str(round(math.log10(value) * 2) / 2)


def _side_relation_key(side_a: List[str], side_b: List[str]) -> str:
    side_a = _reduce_value_endpoint_side(side_a)
    side_b = _reduce_value_endpoint_side(side_b)
    a = "|".join(sorted(side_a)) if side_a else "UNBOUND"
    b = "|".join(sorted(side_b)) if side_b else "UNBOUND"
    left, right = sorted([a, b])
    return f"{left} -- {right}"


def _counter_deficit(actual: Counter, expected: Counter, *, limit: int) -> List[Dict[str, Any]]:
    out = []
    for key, expected_count in expected.items():
        actual_count = int(actual.get(key, 0))
        if actual_count >= expected_count:
            continue
        delta = int(expected_count - actual_count)
        out.append({
            "key": key,
            "expected_count": int(expected_count),
            "actual_count": actual_count,
            "ref_count": int(expected_count),
            "gen_count": actual_count,
            "missing": delta,
        })
    return sorted(out, key=lambda item: (-item["missing"], item["key"]))[:limit]


def _counter_surplus(actual: Counter, expected: Counter, *, limit: int) -> List[Dict[str, Any]]:
    out = []
    for key, actual_count in actual.items():
        expected_count = int(expected.get(key, 0))
        if actual_count <= expected_count:
            continue
        delta = int(actual_count - expected_count)
        out.append({
            "key": key,
            "expected_count": expected_count,
            "actual_count": int(actual_count),
            "ref_count": expected_count,
            "gen_count": int(actual_count),
            "extra": delta,
        })
    return sorted(out, key=lambda item: (-item["extra"], item["key"]))[:limit]


def _identity_deficit(actual: Counter, expected: Counter) -> List[Dict[str, Any]]:
    out = []
    for identity, expected_count in expected.items():
        actual_count = int(actual.get(identity, 0))
        if actual_count >= expected_count:
            continue
        out.append({
            "identity": identity,
            "expected_count": int(expected_count),
            "actual_count": actual_count,
            "ref_count": int(expected_count),
            "gen_count": actual_count,
            "missing": int(expected_count - actual_count),
        })
    return sorted(out, key=lambda item: (-item["missing"], item["identity"]))


def _identity_surplus(actual: Counter, expected: Counter) -> List[Dict[str, Any]]:
    out = []
    for identity, actual_count in actual.items():
        expected_count = int(expected.get(identity, 0))
        if actual_count <= expected_count:
            continue
        out.append({
            "identity": identity,
            "expected_count": expected_count,
            "actual_count": int(actual_count),
            "ref_count": expected_count,
            "gen_count": int(actual_count),
            "extra": int(actual_count - expected_count),
        })
    return sorted(out, key=lambda item: (-item["extra"], item["identity"]))


def _is_artificial_endpoint(endpoint: str) -> bool:
    if not endpoint.startswith("IC:MODEL:"):
        return False
    if ":ROLE:" in endpoint:
        label = endpoint.rsplit(":ROLE:", 1)[1]
    elif ":PIN:" in endpoint:
        label = endpoint.rsplit(":PIN:", 1)[1]
    else:
        return False
    if label.startswith("LOCAL:"):
        return True
    if label.startswith("SIG:"):
        label = label.split("SIG:", 1)[1]
    token = _token_key(label)
    if re.fullmatch(r"(?:\d+N\d+|N\d+|\d+N)", token):
        return True
    return bool(re.match(
        r"^SOC_(?:CFG|ADC|RC|R\d|C2|IC2|L|L10|ESD|ESD_R2|ESD_CHIP)_",
        token,
    ))

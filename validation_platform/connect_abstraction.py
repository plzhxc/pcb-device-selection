"""Pin-aware end-to-end connect abstraction for netlist comparison.

The old topology metric compared bags of net type signatures.  This module
builds a stricter representation: endpoint-to-endpoint connections where
endpoints are IC pins, system IO, power rails, or ground.  Passive devices are
kept as path elements and are normalized by electrical class rather than
designator.  Exact R/C/L values are evaluated by the independent value metric.
"""
from __future__ import annotations

import hashlib
import re
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from validation_platform.device_conduction_rules import conductive_groups
from validation_platform.graph import normalize_type, parse_value


LOCAL_TYPES = {"R", "C", "L", "D_OR_LED", "Q", "SW", "XTAL", "AUDIO", "FUSE"}
TRAVERSABLE_LOCAL_TYPES = frozenset(LOCAL_TYPES)
ENDPOINT_TYPES = {"IC"}
CONNECTOR_TYPE = "CON"
MAX_INTERNAL_DEVICES = 2
MAX_NET_PAIR_TERMS = 32
MAX_PATH_EXPANSIONS_PER_ENDPOINT = 50_000
_PIN_DB_CACHE: Dict[str, Dict[str, Dict[str, str]]] = {}
_PIN_DB_DISABLED = False
_DEVICE_MODEL_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}


@dataclass(frozen=True)
class Term:
    node: str
    device: str
    pin: str
    net: str


def extract_connects(
    netlist: Dict[str, Any],
    *,
    identity_alias: Optional[Dict[str, str]] = None,
    instance_labels: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Extract canonical connect keys from a flat netlist dict.

    Returns counters for two comparison levels:
    - role: IC identity + net/pin role.  This is the main metric because some
      reference netlists only expose pin numbers.
    - strict: IC identity + raw/function pin when available.
    """
    devices = _build_devices(netlist, identity_alias=identity_alias, instance_labels=instance_labels)
    warnings = _quality_warnings(devices)
    net_terms = _build_net_terms(devices)
    graph = _build_connect_graph(devices, net_terms)
    connects, truncated_endpoints = _enumerate_paths(devices, graph)
    if truncated_endpoints:
        _append_path_budget_warning(warnings, truncated_endpoints)
    by_role = defaultdict(list)
    by_strict = defaultdict(list)
    for item in connects:
        by_role[item["role_key"]].append(item)
        by_strict[item["strict_key"]].append(item)
    # The active connect metric compares unique functional connect classes.
    # Repeated BGA power pins, repeated decoupling caps, and LED chains are
    # reviewed through device/value counts instead of dominating topology F1.
    role_counter = Counter({key: 1 for key in by_role})
    strict_counter = Counter({key: 1 for key in by_strict})
    return {
        "role_counter": role_counter,
        "strict_counter": strict_counter,
        "role_details": dict(by_role),
        "strict_details": dict(by_strict),
        "connects": connects,
        "warnings": warnings["items"],
        "warning_summary": warnings["summary"],
        "summary": {
            "connect_count": len(connects),
            "role_key_count": len(role_counter),
            "strict_key_count": len(strict_counter),
            "device_count": len(devices),
            "warning_count": warnings["summary"]["total"],
            "path_budget_truncated_endpoints": truncated_endpoints,
        },
    }


def build_identity_alias(*netlists: Dict[str, Any]) -> Dict[str, str]:
    """Map normalized IC deviceName to a unique C-number seen in either side."""
    candidates: Dict[str, set] = defaultdict(set)
    for netlist in netlists:
        for entry in (netlist or {}).values():
            props = entry.get("props") or {}
            if _connect_type(props) != "IC":
                continue
            supplier = _supplier_c_part(props)
            name_key = _device_name_key(props)
            if supplier and name_key:
                candidates[name_key].add(supplier)
    return {name: next(iter(values)) for name, values in candidates.items() if len(values) == 1}


def build_ic_instance_labels(
    gen_netlist: Dict[str, Any],
    ref_netlist: Dict[str, Any],
    *,
    identity_alias: Optional[Dict[str, str]] = None,
) -> Dict[str, Dict[str, str]]:
    """Match duplicate IC instances by local topology, not by designator.

    Single-instance IC identities keep the base identity.  Duplicate identities
    receive ``<identity>#I<n>`` labels when paired, and side-specific unmatched
    labels when no reasonable pair exists.
    """
    gen_devices = _build_devices(gen_netlist, identity_alias=identity_alias)
    ref_devices = _build_devices(ref_netlist, identity_alias=identity_alias)
    gen_features = _ic_feature_vectors(gen_devices)
    ref_features = _ic_feature_vectors(ref_devices)
    gen_by_id = _ic_ids_by_identity(gen_devices)
    ref_by_id = _ic_ids_by_identity(ref_devices)
    labels = {"gen": {}, "ref": {}}

    for identity in sorted(set(gen_by_id) | set(ref_by_id)):
        gen_ids = gen_by_id.get(identity, [])
        ref_ids = ref_by_id.get(identity, [])
        if len(gen_ids) <= 1 and len(ref_ids) <= 1:
            continue

        used_ref = set()
        used_gen = set()
        pair_index = 1

        # A post-selection fixture can carry an explicit reference designator
        # for a repeated physical IC instance.  That is stronger evidence than
        # a topology similarity score, especially while passive generation is
        # still converging.  Public generated designators may be renumbered
        # later, so use the dedicated provenance field rather than assuming
        # generated and reference designators happen to match.
        ref_by_designator: Dict[str, List[str]] = defaultdict(list)
        for ref_id in ref_ids:
            designator = _comparison_designator(ref_devices.get(ref_id) or {})
            if designator:
                ref_by_designator[designator].append(ref_id)
        exact_pairs: List[Tuple[str, str, str]] = []
        for gen_id in gen_ids:
            props = (gen_devices.get(gen_id) or {}).get("props") or {}
            fixture_designator = _token_key(
                props.get("_fixture_reference_designator")
            )
            if not fixture_designator:
                continue
            candidates_for_designator = ref_by_designator.get(fixture_designator) or []
            if len(candidates_for_designator) == 1:
                exact_pairs.append(
                    (fixture_designator, candidates_for_designator[0], gen_id)
                )
        for _, ref_id, gen_id in sorted(exact_pairs):
            if ref_id in used_ref or gen_id in used_gen:
                continue
            label = f"{identity}#I{pair_index}"
            labels["ref"][ref_id] = label
            labels["gen"][gen_id] = label
            used_ref.add(ref_id)
            used_gen.add(gen_id)
            pair_index += 1

        candidates = []
        for ref_id in ref_ids:
            for gen_id in gen_ids:
                if ref_id in used_ref or gen_id in used_gen:
                    continue
                score = _counter_jaccard(ref_features.get(ref_id, Counter()), gen_features.get(gen_id, Counter()))
                candidates.append((score, ref_id, gen_id))
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        for score, ref_id, gen_id in candidates:
            if ref_id in used_ref or gen_id in used_gen:
                continue
            # If the vectors have no overlap, matching is arbitrary and should
            # not make the two sides look equivalent.
            if score <= 0 and (ref_features.get(ref_id) or gen_features.get(gen_id)):
                continue
            label = f"{identity}#I{pair_index}"
            labels["ref"][ref_id] = label
            labels["gen"][gen_id] = label
            used_ref.add(ref_id)
            used_gen.add(gen_id)
            pair_index += 1
        for ref_id in ref_ids:
            if ref_id not in used_ref:
                labels["ref"][ref_id] = f"{identity}#REF_UNMATCHED_{_token_key(ref_id)[-16:]}"
        for gen_id in gen_ids:
            if gen_id not in used_gen:
                labels["gen"][gen_id] = f"{identity}#GEN_UNMATCHED_{_token_key(gen_id)[-16:]}"

    return labels


def _comparison_designator(dev: Dict[str, Any]) -> str:
    props = dev.get("props") or {}
    return _token_key(
        props.get("designator")
        or props.get("Designator")
        or dev.get("id")
    )


def _build_devices(
    netlist: Dict[str, Any],
    *,
    identity_alias: Optional[Dict[str, str]] = None,
    instance_labels: Optional[Dict[str, str]] = None,
) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for dev_id, entry in (netlist or {}).items():
        props = entry.get("props") or {}
        pins = {
            str(pin): str(net)
            for pin, net in (entry.get("pins") or {}).items()
            if str(net or "").strip() and not _is_nc_net(str(net))
        }
        dtype = _connect_type(props)
        identity, identity_source = _device_identity_info(props, dtype, identity_alias, pins=pins)
        compare_eligible = dtype != "IC" or identity_source != "designator"
        instance_identity = (instance_labels or {}).get(str(dev_id), identity)
        out[str(dev_id)] = {
            "id": str(dev_id),
            "props": props,
            "type": dtype,
            "identity": identity,
            "base_identity": identity,
            "instance_identity": instance_identity,
            "identity_source": identity_source,
            "compare_eligible": compare_eligible,
            "supplier": str(props.get("supplierPart") or props.get("Supplier Part") or "").strip(),
            "resolved_supplier": identity if _is_c_part(identity) else "",
            "display": _device_display(props, str(dev_id)),
            "pins": pins,
        }
    return out


def _connect_type(props: Dict[str, Any]) -> str:
    fixture_kind = str(
        props.get("_fixture_reference_kind") or ""
    ).strip().upper()
    if fixture_kind in {
        "R",
        "C",
        "L",
        "D_OR_LED",
        "Q",
        "SW",
        "XTAL",
        "FUSE",
        "CON",
        "OTHER",
    }:
        return fixture_kind
    dtype = normalize_type(props)
    text = " ".join(
        str(props.get(key) or "")
        for key in (
            "deviceName",
            "DeviceName",
            "deviceNotes",
            "description",
            "deviceType",
            "designator",
            "_physical_device_type",
        )
    ).upper()
    designator = str(props.get("designator") or props.get("Designator") or "").strip().upper()
    physical_type = str(props.get("_physical_device_type") or "").strip().lower()
    if physical_type == "switch":
        return "SW"
    if physical_type == "connector" or (dtype not in LOCAL_TYPES and _looks_like_connector(text, designator)):
        return CONNECTOR_TYPE
    # Some protection arrays use U designators and may be normalized as IC by
    # generic rules, but for connect abstraction they are local path elements,
    # not core endpoints.  D/Q/MOS/TVS/ESD devices should be compared through
    # the path between core IC pins, system IO, power, or ground.
    if dtype == "IC" and _looks_like_local_protection_or_discrete(props):
        return "D_OR_LED"
    led_like = (
        "WS2812" in text
        or "SK6812" in text
        or "RGB LED" in text
        or "RGBLED" in text
        or bool(re.search(r"\bLED\b", text))
    )
    if led_like:
        if not any(token in text for token in ("LED DRIVER", "DRIVER IC", "CONTROLLER")):
            return "D_OR_LED"
    return dtype


def _looks_like_connector(text: str, designator: str = "") -> bool:
    prefix_match = re.match(r"([A-Z]+)", designator or "")
    prefix = prefix_match.group(1) if prefix_match else ""
    if prefix in {"J", "P", "CN", "CON", "FPC", "FFC"}:
        return True
    if any(
        token in text
        for token in (
            "排针",
            "排母",
            "连接器",
            "卡座",
            "插针",
            "排座",
            "香蕉头",
            "香蕉座",
            "BANANA JACK",
            "BINDING POST",
        )
    ):
        return True
    return bool(re.search(
        r"\b(CONNECTOR|HEADER|SOCKET|FPC|FFC|FLEX|SLOT|CARD\s+EDGE|USB\s+TYPE|TYPE[- ]?C|BANANA\s+JACK|BINDING\s+POST|排针|排母|连接器|卡座|香蕉头|香蕉座)\b",
        text,
    ))


def _is_nc_net(net: str) -> bool:
    """Treat module-prefixed NC aliases such as M01__NC as real no-connects."""
    role = classify_net(net)
    return role.get("code") == "NC"


def _looks_like_local_protection_or_discrete(props: Dict[str, Any]) -> bool:
    designator = str(props.get("designator") or "").strip().upper()
    prefix_match = re.match(r"([A-Z]+)", designator)
    prefix = prefix_match.group(1) if prefix_match else ""
    if prefix in {"D", "TVS", "ESD", "Q", "MOS", "LED"}:
        return True
    text = " ".join(
        str(props.get(key) or "")
        for key in ("deviceName", "DeviceName", "deviceNotes", "description", "deviceType")
    ).upper()
    raw_type = str(props.get("deviceType") or props.get("device_type") or "").strip().lower()
    protection_like = bool(re.search(
        r"\b(ESD|TVS|DIODE|SCHOTTKY|ZENER|PROTECTION\s+ARRAY)\b",
        text,
    )) or any(token in text for token in ("静电保护", "保护阵列", "二极管"))
    if raw_type in {"chip", "module", "mcu", "processor", "controller", "wifi_module"} and not protection_like:
        return False
    if _looks_like_core_driver_or_controller(text):
        return False
    return protection_like or bool(re.search(
        r"\b(TRANSISTOR|TRIODE|MOSFET)\b",
        text,
    )) or any(token in text for token in ("三极管",))


def _looks_like_core_driver_or_controller(text: str) -> bool:
    return bool(re.search(
        r"\b(GATE\s+DRIVER|MOTOR\s+DRIVER|BLDC\s+DRIVER|DRIVER\s+IC|CONTROLLER|MCU|SOC|PMIC)\b",
        text,
    )) or any(token in text for token in ("驱动芯片", "栅极驱动", "电机驱动", "控制器", "微控制器"))


def _quality_warnings(devices: Dict[str, Dict[str, Any]], limit: int = 200) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    counts: Counter = Counter()

    def add(code: str, dev: Dict[str, Any], message: str, **extra: Any) -> None:
        counts[code] += 1
        if len(items) >= limit:
            return
        payload = {
            "severity": "warning",
            "code": code,
            "device": dev.get("id"),
            "identity": dev.get("identity"),
            "display": dev.get("display"),
            "message": message,
        }
        payload.update(extra)
        items.append(payload)

    for dev in devices.values():
        if dev.get("type") != "IC":
            continue
        if not dev.get("compare_eligible", True):
            add(
                "IC_MISSING_C_PART_COMPARE_SKIPPED",
                dev,
                "IC 器件原始网表缺少 supplierPart/C 编号，已跳过严格 connect 比较",
            )
            continue
        supplier = str(dev.get("supplier") or "").strip()
        has_c_part = bool(re.fullmatch(r"C\d+", supplier, flags=re.I))
        if not has_c_part and dev.get("identity_source") == "alias_c_part":
            add(
                "IC_C_PART_INFERRED_FROM_PEER",
                dev,
                "IC 器件原始网表缺少 C 编号，已根据另一侧同名器件唯一 C 编号迁移",
                inferred_c_part=dev.get("identity"),
            )
        elif not has_c_part and dev.get("identity_source") in {"dedicated_model", "dedicated_model_inferred"}:
            # Large platform parts such as RK3566/LPDDR4 are intentionally
            # represented by reusable model IDs instead of LCSC C numbers.
            pass
        elif not has_c_part:
            add(
                "IC_MISSING_C_PART",
                dev,
                "IC 器件缺少 C 编号，且无法从另一侧唯一同名器件迁移，signature 降级为 deviceName",
            )
        for pin, net in (dev.get("pins") or {}).items():
            pin_name = _pin_name_for(dev, pin)
            if pin_name:
                continue
            if _token_key(pin).isdigit():
                add(
                    "IC_PIN_NUMBER_FALLBACK",
                    dev,
                    "IC pin 未从参数库解析到 pin_name，connect 标签退回 pin number/net role",
                    pin=str(pin),
                    net=str(net),
                    supplier_part=supplier,
                )

    return {
        "items": items,
        "summary": {
            "total": sum(counts.values()),
            "by_code": dict(counts),
            "truncated": sum(counts.values()) > len(items),
            "shown": len(items),
        },
    }


def _append_path_budget_warning(warnings: Dict[str, Any], endpoints: int) -> None:
    code = "CONNECT_PATH_EXPANSION_BUDGET_EXCEEDED"
    summary = warnings["summary"]
    by_code = summary.setdefault("by_code", {})
    by_code[code] = int(by_code.get(code) or 0) + endpoints
    summary["total"] = int(summary.get("total") or 0) + endpoints
    if len(warnings["items"]) < 200:
        warnings["items"].append(
            {
                "severity": "warning",
                "code": code,
                "device": None,
                "identity": None,
                "display": "connect path search",
                "message": (
                    "连接路径搜索达到每端点扩展预算；结果已安全截断，"
                    "需要修复器件类型或导通规则后再作正式比较"
                ),
                "endpoints": endpoints,
                "budget_per_endpoint": MAX_PATH_EXPANSIONS_PER_ENDPOINT,
            }
        )
        summary["shown"] = len(warnings["items"])
    summary["truncated"] = True


def _ic_ids_by_identity(devices: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = defaultdict(list)
    for dev_id, dev in devices.items():
        if dev.get("type") == "IC" and dev.get("compare_eligible", True):
            out[dev.get("identity") or "UNKNOWN"].append(dev_id)
    for ids in out.values():
        ids.sort()
    return dict(out)


def _ic_feature_vectors(devices: Dict[str, Dict[str, Any]]) -> Dict[str, Counter]:
    net_index: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for dev_id, dev in devices.items():
        for pin, net in (dev.get("pins") or {}).items():
            net_index[net].append((dev_id, pin))

    out: Dict[str, Counter] = {}
    for dev_id, dev in devices.items():
        if dev.get("type") != "IC" or not dev.get("compare_eligible", True):
            continue
        vec: Counter = Counter()
        for pin, net in (dev.get("pins") or {}).items():
            pin_name = _pin_name_for(dev, pin)
            pin_role = _role_pin_key(pin, net, pin_name)
            net_role = classify_net(net)["code"]
            vec[f"PIN:{pin_role}"] += 1
            vec[f"NET:{net_role}"] += 1
            for other_id, other_pin in net_index.get(net, []):
                if other_id == dev_id:
                    continue
                other = devices.get(other_id) or {}
                other_type = other.get("type") or "OTHER"
                if other_type == "IC":
                    other_name = _pin_name_for(other, other_pin)
                    other_role = _role_pin_key(other_pin, net, other_name)
                    vec[f"IC_NEIGHBOR:{pin_role}:{other.get('identity')}:{other_role}"] += 1
                elif other_type in {"R", "C", "L"}:
                    vec[f"LOCAL:{pin_role}:{_element_key(other, other_pin, other_pin)}"] += 1
                elif other_type == CONNECTOR_TYPE:
                    vec[f"SYSIO:{pin_role}:{net_role}"] += 1
                else:
                    vec[f"LOCAL:{pin_role}:{other_type}:{other.get('identity')}"] += 1
        out[dev_id] = vec
    return out


def _local_nodes_for_device(dev: Dict[str, Any]) -> List[str]:
    if dev.get("type") not in TRAVERSABLE_LOCAL_TYPES:
        return []
    groups = conductive_groups(dev)
    if not groups:
        return []
    if len(groups) == 1 and groups[0].key == "MAIN":
        return [f"D:{dev.get('id')}"]
    return [f"D:{dev.get('id')}:{group.key}" for group in groups]


def _local_node_for_pin(dev: Dict[str, Any], pin: str) -> str:
    pin_key = str(pin)
    groups = conductive_groups(dev)
    if len(groups) == 1 and groups[0].key == "MAIN":
        return f"D:{dev.get('id')}"
    for group in groups:
        if pin_key in group.pins:
            return f"D:{dev.get('id')}:{group.key}"
    return f"D:{dev.get('id')}"


def _build_net_terms(devices: Dict[str, Dict[str, Any]]) -> Dict[str, List[Term]]:
    net_terms: Dict[str, List[Term]] = defaultdict(list)
    connector_terms: Dict[str, List[Term]] = defaultdict(list)

    for dev_id, dev in devices.items():
        if dev["type"] not in (
            TRAVERSABLE_LOCAL_TYPES | ENDPOINT_TYPES | {CONNECTOR_TYPE}
        ):
            # Unknown devices are electrically opaque.  Treating an unknown
            # CPU/PCH package as one local conductor connects every pin to
            # every other pin and makes the path search both false and
            # unbounded.
            continue
        for pin, net in dev["pins"].items():
            term = Term(node="", device=dev_id, pin=pin, net=net)
            if dev["type"] == CONNECTOR_TYPE:
                connector_terms[net].append(term)
            elif dev["type"] in ENDPOINT_TYPES:
                net_terms[net].append(Term(
                    node=_endpoint_node(dev, pin, net, level="raw"),
                    device=dev_id,
                    pin=pin,
                    net=net,
                ))
            else:
                net_terms[net].append(Term(
                    node=_local_node_for_pin(dev, pin),
                    device=dev_id,
                    pin=pin,
                    net=net,
                ))

    for net, terms in connector_terms.items():
        role = classify_net(net)
        if role["class"] not in {"power", "gnd"}:
            node = f"E:SYSIO:{role['code']}"
            net_terms[net].append(Term(node=node, device=node, pin="1", net=net))

    for net in list(set(net_terms) | set(connector_terms)):
        role = classify_net(net)
        if role["class"] in {"power", "gnd"}:
            prefix = "GND" if role["class"] == "gnd" else "PWR"
            node = f"E:SYS:{prefix}:{role['code']}"
            net_terms[net].append(Term(node=node, device=node, pin=role["code"], net=net))

    return dict(net_terms)


def _build_connect_graph(
    devices: Dict[str, Dict[str, Any]],
    net_terms: Dict[str, List[Term]],
) -> Dict[str, Any]:
    nodes: Dict[str, Dict[str, Any]] = {}
    adj: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for dev_id, dev in devices.items():
        if dev["type"] == CONNECTOR_TYPE:
            continue
        if dev["type"] in ENDPOINT_TYPES:
            continue
        if dev["type"] not in TRAVERSABLE_LOCAL_TYPES:
            continue
        for node in _local_nodes_for_device(dev):
            nodes[node] = {"kind": "local", "device": dev_id}

    for net_index, (net, terms) in enumerate(sorted(net_terms.items(), key=lambda item: _token_key(item[0]))):
        role = classify_net(net)
        net_node = _net_node_id(net_index, net)
        nodes[net_node] = {"kind": "net", "net": net, "role": role}
        clean_terms = _dedupe_terms(terms)
        for term in clean_terms:
            if term.node.startswith("E:"):
                nodes.setdefault(term.node, _endpoint_info(devices, term, role))
            elif term.node.startswith("D:"):
                nodes.setdefault(term.node, {"kind": "local", "device": term.device})
            edge = {
                "net": net,
                "role": role,
                "pins": {term.node: term.pin},
            }
            adj[term.node].append({"to": net_node, "edge": edge})
            adj[net_node].append({"to": term.node, "edge": edge})

    return {"nodes": nodes, "adj": dict(adj)}


def _net_node_id(index: int, net: str) -> str:
    return f"N:{index}:{_token_key(net)[:80] or 'NET'}"


def _pairs_for_net(terms: List[Term], role: Dict[str, str]) -> List[Tuple[Term, Term]]:
    if len(terms) < 2:
        return []
    endpoints = [term for term in terms if term.node.startswith("E:")]
    locals_ = [term for term in terms if term.node.startswith("D:")]
    boundary = [
        term for term in endpoints
        if term.node.startswith("E:SYS:GND") or term.node.startswith("E:SYS:PWR")
    ]
    non_boundary = [term for term in terms if term not in boundary]

    pairs: List[Tuple[Term, Term]] = []
    if role["class"] in {"power", "gnd"} and boundary:
        b = boundary[0]
        for term in non_boundary:
            pairs.append((b, term))
        return pairs

    if endpoints:
        for i, a in enumerate(endpoints):
            for b in endpoints[i + 1:]:
                pairs.append((a, b))
        for endpoint in endpoints:
            for local in locals_:
                pairs.append((endpoint, local))
        return pairs

    if boundary:
        b = boundary[0]
        for local in locals_:
            pairs.append((b, local))
        return pairs

    if len(terms) > MAX_NET_PAIR_TERMS:
        # Avoid O(n^2) explosions on accidental high-degree signal nets.  Keep
        # endpoint/local branches and skip local/local fanout details.
        for endpoint in endpoints:
            for local in locals_:
                pairs.append((endpoint, local))
        if len(endpoints) <= 8:
            for i, a in enumerate(endpoints):
                for b in endpoints[i + 1:]:
                    pairs.append((a, b))
        return pairs

    for i, a in enumerate(terms):
        for b in terms[i + 1:]:
            pairs.append((a, b))
    return pairs


def _enumerate_paths(
    devices: Dict[str, Dict[str, Any]],
    graph: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], int]:
    nodes = graph["nodes"]
    adj = graph["adj"]
    endpoints = sorted(
        node for node, info in nodes.items()
        if info.get("kind") == "endpoint"
        and str(info.get("role_endpoint") or "").startswith("IC:")
    )
    out: Dict[str, Dict[str, Any]] = {}
    truncated_endpoints = 0

    for start in endpoints:
        expansions = 0
        truncated = False
        stack: List[Tuple[str, List[str], List[Dict[str, Any]], int]] = [(start, [start], [], 0)]
        while stack:
            current, path, edges, internal_count = stack.pop()
            for item in adj.get(current, []):
                expansions += 1
                if expansions > MAX_PATH_EXPANSIONS_PER_ENDPOINT:
                    truncated = True
                    stack.clear()
                    break
                nxt = item["to"]
                if nxt in path:
                    continue
                if not _can_traverse(nodes, current, nxt, path):
                    continue
                edge = item["edge"]
                info = nodes.get(nxt) or {}
                if info.get("kind") == "endpoint":
                    if nxt == start:
                        continue
                    record = _path_record(devices, nodes, path + [nxt], edges + [edge])
                    if record:
                        out.setdefault((record["role_key"], tuple(record["nodes"])), record)
                    continue
                next_internal_count = internal_count + (1 if info.get("kind") == "local" else 0)
                if next_internal_count > MAX_INTERNAL_DEVICES:
                    continue
                stack.append((nxt, path + [nxt], edges + [edge], next_internal_count))
        if truncated:
            truncated_endpoints += 1

    return (
        sorted(out.values(), key=lambda item: (item["role_key"], item["nodes"])),
        truncated_endpoints,
    )


def _can_traverse(
    nodes: Dict[str, Dict[str, Any]],
    current: str,
    nxt: str,
    path: List[str],
) -> bool:
    current_info = nodes.get(current) or {}
    next_info = nodes.get(nxt) or {}
    if current_info.get("kind") != "net":
        return True
    role = current_info.get("role") or {}
    if role.get("class") not in {"power", "gnd"}:
        return True

    # Power/GND nets are boundary rails.  Stop at SYS:PWR/GND instead of
    # walking through every decoupling capacitor or pull network on the rail.
    # Otherwise one IC pin can grow into a huge rail fanout tree and produce
    # non-actionable paths such as IC -- R -- C -- GND.
    role_endpoint = str(next_info.get("role_endpoint") or "")
    if role_endpoint.startswith("SYS:"):
        return True
    if len(path) >= 2:
        prev_info = nodes.get(path[-2]) or {}
        prev_endpoint = str(prev_info.get("role_endpoint") or "")
        if prev_endpoint.startswith("SYS:"):
            return True
    return False


def _path_record(
    devices: Dict[str, Dict[str, Any]],
    nodes: Dict[str, Dict[str, Any]],
    path: List[str],
    edges: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if len(path) < 2:
        return None
    a_node, b_node = path[0], path[-1]
    a_info = nodes.get(a_node) or {}
    b_info = nodes.get(b_node) or {}
    if a_info.get("kind") != "endpoint" or b_info.get("kind") != "endpoint":
        return None
    if a_info.get("device") and a_info.get("device") == b_info.get("device"):
        return None
    # The connect metric is IC-centric: start from an IC pin and walk through
    # passives until another IC, ground, power or system IO is reached.  Pure
    # system-to-system passives, such as standalone decoupling caps between a
    # rail and GND, are covered by device/value metrics and should not dominate
    # topology F1.
    if not (
        str(a_info.get("role_endpoint") or "").startswith("IC:")
        or str(b_info.get("role_endpoint") or "").startswith("IC:")
    ):
        return None

    elements = []
    for idx, node in enumerate(path[1:-1], start=1):
        node_info = nodes.get(node) or {}
        if node_info.get("kind") != "local":
            continue
        dev_id = (nodes.get(node) or {}).get("device")
        dev = devices.get(dev_id or "")
        if not dev:
            return None
        in_pin = edges[idx - 1]["pins"].get(node, "")
        out_pin = edges[idx]["pins"].get(node, "")
        if _token_key(in_pin) and _token_key(in_pin) == _token_key(out_pin):
            return None
        elements.append(_element_key(dev, in_pin, out_pin))

    if not elements:
        elements = ["WIRE"]

    strict_a = a_info["strict_endpoint"]
    strict_b = b_info["strict_endpoint"]
    role_a = a_info["role_endpoint"]
    role_b = b_info["role_endpoint"]
    reverse = (role_b, strict_b) < (role_a, strict_a)
    if reverse:
        strict_a, strict_b = strict_b, strict_a
        role_a, role_b = role_b, role_a
        elements = list(reversed(elements))
        path = list(reversed(path))
        edges = list(reversed(edges))

    role_key = _connect_key(role_a, role_b, elements)
    strict_key = _connect_key(strict_a, strict_b, elements)
    return {
        "role_key": role_key,
        "strict_key": strict_key,
        "endpoints": [role_a, role_b],
        "strict_endpoints": [strict_a, strict_b],
        "path": elements,
        "nets": _dedupe_ordered([edge["net"] for edge in edges]),
        "nodes": path,
    }


def _connect_key(a: str, b: str, elements: Iterable[str]) -> str:
    return f"{a} -- {' -- '.join(elements)} -- {b}"


def _dedupe_ordered(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _endpoint_info(devices: Dict[str, Dict[str, Any]], term: Term, role: Dict[str, str]) -> Dict[str, Any]:
    if term.node.startswith("E:SYSIO:"):
        code = term.node.split("E:SYSIO:", 1)[1]
        endpoint = f"SYS:IO:{code}"
        return {
            "kind": "endpoint",
            "strict_endpoint": endpoint,
            "role_endpoint": endpoint,
            "device": term.device,
        }
    if term.node.startswith("E:SYS:GND:"):
        return {
            "kind": "endpoint",
            "strict_endpoint": "SYS:GND",
            "role_endpoint": "SYS:GND",
            "device": term.device,
        }
    if term.node.startswith("E:SYS:PWR:"):
        code = term.node.split("E:SYS:PWR:", 1)[1]
        endpoint = f"SYS:PWR:{code}"
        return {
            "kind": "endpoint",
            "strict_endpoint": endpoint,
            "role_endpoint": endpoint,
            "device": term.device,
        }

    dev = devices.get(term.device) or {}
    identity = dev.get("instance_identity") or dev.get("identity") or "UNKNOWN"
    pin_name = _pin_name_for(dev, term.pin)
    strict_pin = _strict_pin_key(term.pin, term.net, pin_name)
    role_pin = _role_pin_key(term.pin, term.net, pin_name)
    return {
        "kind": "endpoint",
        "strict_endpoint": f"IC:{identity}:PIN:{strict_pin}",
        "role_endpoint": f"IC:{identity}:ROLE:{role_pin}",
        "device": term.device,
    }


def _endpoint_node(dev: Dict[str, Any], pin: str, net: str, *, level: str) -> str:
    identity = dev.get("instance_identity") or dev.get("identity") or "UNKNOWN"
    return f"E:IC:{identity}:{_token_key(str(pin))}:{_token_key(str(net))}:{level}"


def _device_identity(props: Dict[str, Any], dtype: str, identity_alias: Optional[Dict[str, str]] = None) -> str:
    return _device_identity_info(props, dtype, identity_alias)[0]


def _device_identity_info(
    props: Dict[str, Any],
    dtype: str,
    identity_alias: Optional[Dict[str, str]] = None,
    pins: Optional[Dict[str, str]] = None,
) -> Tuple[str, str]:
    name_key = _device_name_key(props)
    model = _device_model_for_props(props) if dtype == "IC" else None
    if dtype == "IC" and model:
        model_id = _token_key(str(model.get("model_id") or ""))
        if model_id:
            return f"MODEL:{model_id}", "dedicated_model"
    inferred_model = _infer_model_from_pin_nets(props, pins or {})
    if dtype == "IC" and inferred_model:
        return f"MODEL:{inferred_model}", "dedicated_model_inferred"
    supplier = _raw_supplier_c_part(props)
    if supplier:
        return supplier, "supplier_c_part"
    if identity_alias and name_key and name_key in identity_alias:
        return identity_alias[name_key], "alias_c_part"
    supplier_raw = str(props.get("supplierPart") or props.get("Supplier Part") or "").strip()
    if supplier_raw:
        return _token_key(supplier_raw), "supplier_non_c"
    if name_key:
        return name_key[:80], "device_name"
    designator = str(props.get("designator") or "").strip()
    return f"{dtype}:{_token_key(designator) or 'UNKNOWN'}", "designator"


def _supplier_c_part(props: Dict[str, Any]) -> str:
    for key in ("supplierPart", "Supplier Part", "deviceName", "DeviceName", "Manufacturer Part"):
        text = str(props.get(key) or "").strip()
        match = re.search(r"\bC\d+\b", text, flags=re.I)
        if match:
            return match.group(0).upper()
    return ""


def _raw_supplier_c_part(props: Dict[str, Any]) -> str:
    for key in ("supplierPart", "Supplier Part"):
        text = str(props.get(key) or "").strip()
        match = re.search(r"\bC\d+\b", text, flags=re.I)
        if match:
            return match.group(0).upper()
    return ""


def _device_name_key(props: Dict[str, Any]) -> str:
    text = str(
        props.get("deviceName")
        or props.get("DeviceName")
        or props.get("Manufacturer Part")
        or ""
    ).strip()
    text = _strip_lcsc_prefix(text)
    text = re.sub(r"\b(?:TSSOP|SOP|SOIC|QFN|QFP|LQFP|BGA|DFN|SOT|TO)-?\d*\b", "", text, flags=re.I)
    text = re.sub(r"\b(?:WI[- ]?FI|BT|BLUETOOTH|COMBO|MODULE|MOD)\b", "", text, flags=re.I)
    token = _token_key(text)
    if token:
        return token
    if text:
        digest = hashlib.sha1(text.casefold().encode("utf-8")).hexdigest()[:16]
        return f"NAME_{digest}"
    return ""


def _is_c_part(text: str) -> bool:
    return bool(re.fullmatch(r"C\d+", str(text or ""), flags=re.I))


def _device_model_for_props(props: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    keys = [
        props.get("supplierPart"),
        props.get("Supplier Part"),
        props.get("deviceName"),
        props.get("DeviceName"),
        props.get("Manufacturer Part"),
    ]
    cache_key = "|".join(_token_key(str(key or "")) for key in keys)
    if cache_key in _DEVICE_MODEL_CACHE:
        return _DEVICE_MODEL_CACHE[cache_key]
    try:
        from .device_models import get_device_model

        model = get_device_model(*keys)
    except Exception:
        model = None
    _DEVICE_MODEL_CACHE[cache_key] = model
    return model


def _infer_model_from_pin_nets(props: Dict[str, Any], pins: Dict[str, str]) -> str:
    if not pins:
        return ""
    nets = " ".join(str(net or "").upper() for net in pins.values())
    name = str(props.get("deviceName") or props.get("DeviceName") or "").upper()
    # LPDDR4 memory chips in EasyEDA references can appear as just an LCSC ID
    # in deviceName.  Their DDR net family is a stronger identity signal than
    # the opaque C-number for review alignment.
    if "LPDDR4_" in nets and "RK3566" not in name:
        return "LPDDR4"
    return ""


def _device_display(props: Dict[str, Any], fallback: str) -> str:
    return str(
        props.get("supplierPart")
        or props.get("deviceName")
        or props.get("designator")
        or fallback
    )


def _strip_lcsc_prefix(text: str) -> str:
    return re.sub(r"\bC\d+\s*/\s*", "", text, flags=re.I).strip()


def _strict_pin_key(pin: str, net: str, pin_name: str = "") -> str:
    raw = _normalize_strict_pin_token(_token_key(pin_name) or _token_key(pin))
    role = _role_pin_key(pin, net, pin_name)
    if role.startswith("SIG:") and raw == _normalize_strict_pin_token(role.split("SIG:", 1)[1]):
        return raw[:48]
    if not raw or raw.isdigit():
        return role
    if raw in {"VDD", "VCC", "AVDD", "DVDD", "PVDD", "VIN", "VBAT", "VBUS", "GND", "AGND", "DGND"}:
        return role
    if raw in {
        "RESETB",
        "RESET_N",
        "RESETN",
        "RSTB",
        "RST_N",
        "SCL",
        "SDA",
        "MCLK",
        "BCLK",
        "LRCK",
        "SLEEP",
        "INT",
        "PWRON",
        "PWRONKEY",
        "PWRON_KEY",
    }:
        return role
    return raw[:48]


def _normalize_strict_pin_token(token: str) -> str:
    token = _token_key(token)
    if token in {"RESET_B", "RESETB", "RESET_N", "RESETN", "RST_B", "RSTB", "RST_N", "RSTN"}:
        return "RESETB"
    if token in {"PWRONKEY", "PWRON_KEY", "POWERON", "POWER_ON"}:
        return "PWRON_KEY"
    return token


def _role_pin_key(pin: str, net: str, pin_name: str = "") -> str:
    role = classify_net(net)
    if role["class"] in {"power", "gnd", "none"}:
        return role["code"]
    if pin_name:
        name_role = classify_net(pin_name)
        pin_token = _token_key(pin_name)
        gpio = _gpio_code(pin_token)
        if gpio and role["class"] not in {"signal", "local", "none", "gpio"}:
            return role["code"]
        if name_role["class"] in {"power", "gnd"}:
            return name_role["code"]
        if gpio:
            return gpio
        if name_role["class"] in {"usb", "reset", "boot", "clock", "sd", "spi", "uart", "i2c", "i2s", "analog", "pwm", "key"}:
            return name_role["code"]
        if _is_meaningful_pin_token(pin_token):
            return f"SIG:{pin_token[:48]}"
    if role["class"] not in {"signal", "local", "none"}:
        return role["code"]
    code = role["code"]
    if code.startswith("SIG:"):
        pin_token = _token_key(pin_name) or _token_key(pin)
        gpio = _gpio_code(pin_token)
        if gpio:
            return gpio
    return code


def _is_meaningful_pin_token(token: str) -> bool:
    if not token or token.isdigit():
        return False
    return token not in {"NC", "N_C", "RESERVED", "DNC", "EP", "PAD"}


def _pin_name_for(dev: Dict[str, Any], pin: str) -> str:
    props = dev.get("props") or {}
    supplier = str(
        dev.get("resolved_supplier")
        or props.get("supplierPart")
        or props.get("Supplier Part")
        or props.get("_physical_supplier_part")
        or ""
    ).strip()
    if not _is_c_part(supplier):
        supplier = _supplier_c_part(props)
    if not supplier:
        model = _device_model_for_props(props)
        model_supplier = str((model or {}).get("physical_supplier_part") or "").strip()
        if _is_c_part(model_supplier):
            supplier = model_supplier
    if not supplier:
        return ""
    pins = _device_parameter_pins(supplier)
    info = pins.get(str(pin)) or {}
    return str(info.get("name") or "").strip()


def _device_parameter_pins(supplier_part: str) -> Dict[str, Dict[str, str]]:
    global _PIN_DB_DISABLED
    supplier_part = str(supplier_part or "").strip()
    if not supplier_part or _PIN_DB_DISABLED:
        return {}
    if supplier_part in _PIN_DB_CACHE:
        return _PIN_DB_CACHE[supplier_part]
    try:
        import mysql.connector  # type: ignore

        conn = mysql.connector.connect(
            host=os.environ.get("DEVICE_PARAMETERS_HOST", "10.0.0.102"),
            port=int(os.environ.get("DEVICE_PARAMETERS_PORT", "3307")),
            user=os.environ.get("DEVICE_PARAMETERS_USER", "root"),
            password=os.environ.get("DEVICE_PARAMETERS_PASSWORD", "123456"),
            database=os.environ.get("DEVICE_PARAMETERS_DATABASE", "device_parameters"),
            connection_timeout=int(float(os.environ.get("DEVICE_PARAMETERS_TIMEOUT", "2"))),
        )
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT p.pin_number, p.pin_index, p.pin_name, p.pin_description
            FROM pins p
            JOIN devices d ON p.device_id = d.device_id
            WHERE d.supplier_part = %s
            ORDER BY p.pin_index
            """,
            (supplier_part,),
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception:
        _PIN_DB_DISABLED = True
        _PIN_DB_CACHE[supplier_part] = {}
        return {}

    pins: Dict[str, Dict[str, str]] = {}
    for row in rows or []:
        info = {
            "name": str(row.get("pin_name") or "").strip(),
            "description": str(row.get("pin_description") or "").strip(),
        }
        number = str(row.get("pin_number") or "").strip()
        index = str(row.get("pin_index") or "").strip()
        if number:
            pins[number] = info
        if index and index not in pins:
            pins[index] = info
    _PIN_DB_CACHE[supplier_part] = pins
    return pins


def _element_key(dev: Dict[str, Any], in_pin: str, out_pin: str) -> str:
    dtype = dev.get("type") or "OTHER"
    props = dev.get("props") or {}
    if dtype in {"R", "C", "L"}:
        return dtype
    if dtype == "D_OR_LED":
        direction = _direction_key(in_pin, out_pin)
        return f"D@{direction}"
    if dtype == "Q":
        return "Q"
    if dtype == "SW":
        return "SW"
    if dtype == "XTAL":
        return f"XTAL@{_value_key(props, dtype)}"
    if dtype == "AUDIO":
        return f"AUDIO@{_device_identity(props, dtype)}"
    return f"{dtype}@{_device_identity(props, dtype)}"


def _value_key(props: Dict[str, Any], kind: str) -> str:
    raw_candidates = [
        str(props.get("value") or ""),
        str(props.get("deviceName") or ""),
        str(props.get("deviceNotes") or ""),
    ]
    for raw in raw_candidates:
        value = _parse_value_anywhere(raw, kind)
        if value is not None and value > 0:
            return _eng_value(value, kind)
    return "?"


def _parse_value_anywhere(text: str, kind: str) -> Optional[float]:
    if not text:
        return None
    direct = parse_value(text, kind)
    if direct is not None:
        return direct
    unit_re = {
        "R": r"(?:ohm|r|k|kohm|m|mohm|meg|mega|Ω)",
        "C": r"(?:pf|nf|uf|u|µf|μf|mf|f)",
        "L": r"(?:nh|uh|u|µh|μh|mh|h)",
        "XTAL": r"(?:hz|khz|mhz)",
    }.get(kind, r"[a-zA-ZΩµμ]+")
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(" + unit_re + r")", text, re.I):
        token = "".join(match.groups())
        value = parse_value(
            token.replace("µ", "u").replace("μ", "u"),
            "L" if kind == "XTAL" else kind,
        )
        if value is not None:
            return value
    return None


def _eng_value(value: float, kind: str) -> str:
    if kind == "R":
        if value >= 1e6:
            return f"{value / 1e6:.4g}M"
        if value >= 1e3:
            return f"{value / 1e3:.4g}k"
        return f"{value:.4g}R"
    if kind == "C":
        if value >= 1e-6:
            return f"{value / 1e-6:.4g}uF"
        if value >= 1e-9:
            return f"{value / 1e-9:.4g}nF"
        return f"{value / 1e-12:.4g}pF"
    if kind == "L":
        if value >= 1e-3:
            return f"{value / 1e-3:.4g}mH"
        if value >= 1e-6:
            return f"{value / 1e-6:.4g}uH"
        return f"{value / 1e-9:.4g}nH"
    return f"{value:.4g}"


def _direction_key(in_pin: str, out_pin: str) -> str:
    a = _token_key(in_pin)
    b = _token_key(out_pin)
    if {a, b} & {"A", "ANODE"} and {a, b} & {"K", "C", "CATHODE"}:
        return f"{a}->{b}"
    return "BIDIR"


def _dedupe_terms(terms: List[Term]) -> List[Term]:
    seen = set()
    out = []
    for term in terms:
        key = (term.node, term.pin, term.net)
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


def classify_net(net: str) -> Dict[str, str]:
    raw = _canonical_net_name(str(net or ""))
    upper = raw.upper()
    compact = re.sub(r"[^A-Z0-9]+", "_", upper).strip("_")
    if not raw or upper == "NC":
        return {"code": "NC", "class": "none", "label": "NC"}
    if _is_ground_code(compact):
        return {"code": "GND", "class": "gnd", "label": "GND"}
    for code, klass, needles in _high_priority_signal_roles():
        if any(needle in compact for needle in needles):
            return {"code": code, "class": klass, "label": code}
    if _local_function_net_code(compact):
        code = _local_function_net_code(compact)
        return {"code": code, "class": "signal", "label": code.removeprefix("SIG:")}
    power = _power_code(compact)
    if power:
        return {"code": power, "class": "power", "label": power.replace("PWR_", "")}
    usb = _usb_code(upper, compact)
    if usb:
        return {"code": usb, "class": "usb", "label": usb}
    # Keyboard scanner control nets contain generic words such as KEY/SCL, but
    # electrically they are 74HC165 scan controls.  Classify these before the
    # generic I2C/KEY rules so reference and generated connect keys align.
    if compact in {"KEY_PL", "KEY_LOAD", "KEY_SH_LD", "KEY_SHLD", "SCAN_PL", "SCAN_LOAD"}:
        return {"code": "SIG:SCAN_PL", "class": "key", "label": "SCAN_PL"}
    if compact in {"KEY_CE", "KEY_EN", "KEY_ENABLE", "SCAN_CE", "SCAN_EN"}:
        return {"code": "SIG:SCAN_CE", "class": "key", "label": "SCAN_CE"}
    if compact in {"KEY_SCL", "KEY_CLK", "KEY_CP", "SCAN_CLK", "SCAN_CP"}:
        return {"code": "SIG:CLK", "class": "key", "label": "SCAN_CLK"}
    for code, klass, needles in (
        ("USB_DP", "usb", ("USB_DP", "USB_D_P", "USB_P", "DPLUS", "DP")),
        ("USB_DM", "usb", ("USB_DM", "USB_D_N", "USB_N", "DMINUS", "DM")),
        ("I2C_SCL", "i2c", ("SCL",)),
        ("I2C_SDA", "i2c", ("SDA",)),
        ("SD_CLK", "sd", ("SD_CLK", "SDIO_CLK", "SDMMC_CLK", "SDMMC1_CLK")),
        ("SD_CMD", "sd", ("SD_CMD", "SDIO_CMD", "SDMMC_CMD", "SDMMC1_CMD")),
        ("SD_D0", "sd", ("SD_D0", "SDIO_D0", "SDMMC_D0", "SDMMC1_D0", "SD_DAT0", "EMMC_D0", "FLASH_D0", "DAT0", "DATA0")),
        ("SD_D1", "sd", ("SD_D1", "SDIO_D1", "SDMMC_D1", "SDMMC1_D1", "SD_DAT1", "EMMC_D1", "FLASH_D1", "DAT1", "DATA1")),
        ("SD_D2", "sd", ("SD_D2", "SDIO_D2", "SDMMC_D2", "SDMMC1_D2", "SD_DAT2", "EMMC_D2", "FLASH_D2", "DAT2", "DATA2")),
        ("SD_D3", "sd", ("SD_D3", "SDIO_D3", "SDMMC_D3", "SDMMC1_D3", "SD_DAT3", "EMMC_D3", "FLASH_D3", "DAT3", "DATA3")),
        ("SD_STROBE", "sd", ("RCLK", "DATA_STROBE", "EMMC_STROBE", "FLASH_CLE")),
        ("I2S_MCLK", "i2s", ("I2S_MCLK", "I2S_MCK", "MCLK", "MCK")),
        ("I2S_BCLK", "i2s", ("BCLK", "SCLK")),
        ("I2S_LRCK", "i2s", ("LRCK", "WS")),
        ("I2S_BCLK", "i2s", ("I2S_BCK",)),
        ("I2S_DIN", "i2s", ("DIN", "SDIN", "I2S_DI", "I2S1_SDI", "SDI0")),
        ("I2S_DOUT", "i2s", ("DOUT", "SDOUT", "I2S_DO", "I2S1_SDO", "SDO0")),
        ("SPI_MOSI", "spi", ("MOSI",)),
        ("SPI_MISO", "spi", ("MISO",)),
        ("SPI_SCK", "spi", ("SCK", "SPI_CLK")),
        ("SPI_CS", "spi", ("CS", "NSS")),
        ("UART_TX", "uart", ("UART_TX", "_TX", "TXD")),
        ("UART_RX", "uart", ("UART_RX", "_RX", "RXD")),
        ("SWDIO", "debug", ("SWDIO",)),
        ("SWCLK", "debug", ("SWCLK",)),
        ("RESET", "reset", ("NRST", "RESET", "RST", "ESP_EN", "CHIP_EN")),
        ("BOOT", "boot", ("BOOT", "GPIO0_BOOT", "IO0_BOOT")),
        ("XTAL", "clock", ("XTAL", "OSC", "XIN24M", "XOUT24M", "CLK24M", "CLK_24M", "24M_XIN", "24M_XOUT", "CRYSTAL")),
        ("PWM", "pwm", ("PWM",)),
        ("ADC", "analog", ("ADC", "AIN")),
        ("KEY", "key", ("BUTTON", "KEY")),
    ):
        if any(needle in compact for needle in needles):
            return {"code": code, "class": klass, "label": code}
    gpio = _gpio_code(compact)
    if gpio:
        return {"code": gpio, "class": "gpio", "label": gpio}
    if re.search(r"(^|_)N?\d+($|_)", compact) or compact.startswith("$"):
        return {"code": f"LOCAL:{_token_key(compact)[:40]}", "class": "local", "label": "local"}
    return {"code": f"SIG:{_token_key(compact)[:48]}", "class": "signal", "label": raw[:48]}


def _is_ground_code(compact: str) -> bool:
    if compact in {
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
        "0V",
        "VSS",
        "VSSA",
        "VSSD",
    }:
        return True
    if compact.startswith(("GND_", "AGND_", "DGND_", "PGND_", "SGND_")):
        return True
    if re.fullmatch(r"(?:AUDIO|ANALOG|DIGITAL|POWER|USB|LCD|CAM|ADC|DAC|MIC|SPK)_GND(?:_\d+)?", compact):
        return True
    return False


def _high_priority_signal_roles() -> Tuple[Tuple[str, str, Tuple[str, ...]], ...]:
    return (
        ("RESET", "reset", ("NRST", "RESET", "RST", "ESP_EN", "CHIP_EN")),
        ("BOOT", "boot", ("GPIO0_BOOT", "IO0_BOOT", "BOOT")),
    )


def _local_function_net_code(compact: str) -> str:
    """Classify regulator/control local nets before voltage-token power rules.

    Nets such as ``FB_1V2`` or ``PWR_EN`` are not rails just because their
    names contain a voltage or ``PWR`` token.  They are feedback, enable, switch
    or protection nodes that must remain attached to the IC pin endpoint for
    formal value comparison.
    """
    if not compact:
        return ""
    tokens = [token for token in compact.split("_") if token]
    if not tokens:
        return ""
    gate_aliases = {
        "GATE_AH": "SIG:HO_A",
        "GATE_AL": "SIG:LO_A",
        "GATE_BH": "SIG:HO_B",
        "GATE_BL": "SIG:LO_B",
        "GATE_CH": "SIG:HO_C",
        "GATE_CL": "SIG:LO_C",
        "GATE_B_H": "SIG:HO_B",
        "GATE_B_L": "SIG:LO_B",
        "GATE_C_H": "SIG:HO_C",
        "GATE_C_L": "SIG:LO_C",
        "GATE_HA": "SIG:HO_A",
        "GATE_HB": "SIG:HO_B",
        "GATE_HC": "SIG:HO_C",
        "GATE_LA": "SIG:LO_A",
        "GATE_LB": "SIG:LO_B",
        "GATE_LC": "SIG:LO_C",
        "GATE_UH": "SIG:HO_A",
        "GATE_VH": "SIG:HO_B",
        "GATE_WH": "SIG:HO_C",
        "GATE_UL": "SIG:LO_A",
        "GATE_VL": "SIG:LO_B",
        "GATE_WL": "SIG:LO_C",
    }
    if compact in gate_aliases:
        return gate_aliases[compact]
    first = tokens[0]
    local_prefixes = {
        "FB",
        "FBA",
        "FBB",
        "EN",
        "ENA",
        "ENB",
        "CE",
        "BS",
        "BOOT",
        "LX",
        "SW",
        "PG",
        "PGOOD",
        "ISET",
        "ILIM",
        "OVP",
        "COMP",
        "SS",
        "RT",
        "SYNC",
    }
    if first in local_prefixes:
        return f"SIG:{_token_key(compact)[:48]}"
    if compact in {"PWR_EN", "POWER_EN", "PWR_ENABLE", "POWER_ENABLE"}:
        return "SIG:PWR_EN"
    if compact.endswith(("_EN", "_ENABLE", "_FB", "_LX", "_BS", "_BOOT", "_ISET", "_OVP")):
        return f"SIG:{_token_key(compact)[:48]}"
    return ""


def _usb_code(upper: str, compact: str) -> str:
    normalized = upper.replace("＋", "+").replace("－", "-").replace("−", "-")
    if re.search(r"(?:^|[^A-Z0-9])(?:UD|D|USB_D)\+(?:[^A-Z0-9]|$)", normalized):
        return "USB_DP"
    if re.search(r"(?:^|[^A-Z0-9])(?:UD|D|USB_D)-(?:[^A-Z0-9]|$)", normalized):
        return "USB_DM"
    if re.search(r"(?:^|_)DPLUS(?:_|$)", compact):
        return "USB_DP"
    if re.search(r"(?:^|_)DMINUS(?:_|$)", compact):
        return "USB_DM"
    return ""


def _canonical_net_name(net: str) -> str:
    raw = str(net or "").strip()
    if not raw:
        return raw
    # Generated nets frequently carry module prefixes such as M4.3__PA_EN or
    # MOD_AUDIO__I2C_SCL.  These prefixes are placement/context identifiers,
    # not electrical semantics, so strip them before role classification.
    if "__" in raw:
        raw = raw.rsplit("__", 1)[-1]
    raw = re.sub(r"^M[0-9][A-Za-z0-9_.-]*[_-]+", "", raw, flags=re.I)
    raw = re.sub(r"^(?:MOD|MODULE)[A-Za-z0-9_.-]*[_-]+", "", raw, flags=re.I)
    raw = re.sub(r"^[A-Z]+[0-9]+(?:\.[0-9]+)?[_-]+", "", raw, flags=re.I)
    return raw


def _power_code(compact: str) -> str:
    if compact in {"PWR", "POWER", "VPWR", "VPOWER"} or compact.startswith("PWR_") or compact.endswith("_PWR"):
        return "PWR"
    if "3V3" in compact or "3_3V" in compact or compact in {"V33", "V3V3"} or compact.endswith("_V33"):
        return "PWR_3V3"
    if "1V8" in compact or "1_8V" in compact:
        return "PWR_1V8"
    if "2V8" in compact or "2_8V" in compact or compact in {"V2V8", "2V8"}:
        return "PWR_2V8"
    if "1V2" in compact or "1_2V" in compact:
        return "PWR_1V2"
    if "5V" in compact:
        return "PWR_5V"
    if "12V" in compact:
        return "PWR_12V"
    if "VBUS" in compact:
        return "PWR_VBUS"
    if "VBAT" in compact or "BAT" in compact:
        return "PWR_BAT"
    if compact in {"VIN", "VOUT", "VSYS", "SYS", "VCCSYS", "VDD_SYS"}:
        return "PWR"
    if compact.startswith(("VIN_", "VOUT_", "VSYS_", "SYS_")):
        return "PWR"
    if "VCC" in compact or "VDD" in compact or compact.startswith("AVDD") or compact.startswith("DVDD"):
        return "PWR"
    return ""


def _gpio_code(text: str) -> str:
    token = _token_key(text)
    match = re.search(r"(?:^|_)(?:GPIO|IO)(\d{1,2})(?:_|$)", token)
    if match:
        return f"IO_{int(match.group(1))}"
    match = re.search(r"\bP([A-K])(\d{1,2})\b", token)
    if match:
        return f"P{match.group(1)}{int(match.group(2))}"
    return ""


def _counter_jaccard(a: Counter, b: Counter) -> float:
    keys = set(a) | set(b)
    if not keys:
        return 1.0
    inter = sum(min(a.get(k, 0), b.get(k, 0)) for k in keys)
    union = sum(max(a.get(k, 0), b.get(k, 0)) for k in keys)
    return inter / union if union else 1.0


def _token_key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(text or "").upper()).strip("_")

"""跨 module 网表合并（union-find）。

生产路径只按 top_port 的显式端口连接合并局部网络；旧调用者可保留全局网络
与启发式同名合并。网络与器件先按 tuple 身份处理，再分配无碰撞的展示名称。
产物是一份扁平 ``whole_netlist`` dict (gge_id → device)。
"""
from __future__ import annotations
import re
from copy import deepcopy
from collections.abc import Mapping
from typing import Any, Dict, List, Tuple


# Global net 名（不加 module 前缀，合并时跨 module 共享）
_GLOBAL_NETS = {
    "GND", "GND_1", "GND_2", "GNDDETECT", "AGND", "DGND",
    "VDD", "VCC", "3V3", "V33", "VBUS", "VBUS_EXT", "VBAT",
    "5V", "+5V", "1V8", "VCCIO",
}


def _is_global_net(net: str) -> bool:
    return net.upper() in _GLOBAL_NETS


def _is_no_connect(net: Any) -> bool:
    """NC is a per-pin marker, never a shared electrical network."""
    return isinstance(net, str) and net.upper() == "NC"


def _is_explicit_module_net(net: str) -> bool:
    """True for already-qualified nets such as ``M04.1__USB_DM``.

    These names are intentionally emitted by deterministic profiles or repair
    logic to bind a small passive to another module's canonical namespace.  The
    merge step must not turn them into ``M02__M04.1__USB_DM``.
    """
    return bool(re.match(r"^(?:M|MOD|MODULE)[A-Za-z0-9_.-]+__.+", str(net or "")))


def _extract_index(name: str) -> tuple:
    """从 net 名抽取末尾数字索引（如 KIN_05 → ('KIN_', '05')；KEY_NO_A_0 → ('KEY_NO_A_', '0')）。
    返回 (前缀去数字, 数字字符串)。无数字返回 (name, None)。"""
    import re as _re
    m = _re.match(r"^(.+?)_?(\d+)$", name or "")
    if m:
        return (m.group(1), m.group(2))
    return (name or "", None)


def _heuristic_cross_module_net_unions(per_module_netlists: List[Dict[str, Any]],
                                       allowed_pairs: set = None) -> List[tuple]:
    """启发式：跨 module 同物理 net 推断（保守版）。

    规则（必须是明确不会冲突的）：
    1) **KIN_X ↔ KEY_NO_A_X 1:1 配对**（只 NO_A 端，NO_B 默认接 GND）
    2) SPI 总线同名 net 跨 module union（仅明确的 SPI_MOSI/MISO/CLK/CS 命名）
    3) I2C 总线同名 union（SCL/SDA）
    4) UART 总线同名 union（TX/RX）

    **不再**做"同名 net 跨 module 全部 union"——多 MCU 的 BOOT0/NRST/EN/FB 不应被合。
    """
    module_nets: Dict[str, set] = {}
    for entry in per_module_netlists:
        mid = entry.get("module_id") or "UNK"
        nl = entry.get("netlist") or {}
        nets = set()
        for dev in nl.values():
            for pin, net in (dev.get("pins") or {}).items():
                if net and not _is_no_connect(net) and not _is_global_net(net):
                    nets.add(net)
        module_nets[mid] = nets

    unions: List[tuple] = []

    # 规则 1: KEY_NO_A_<n> ↔ KIN_<n % 8>_<n // 8>（键盘扫描矩阵）
    # 74HC165 每颗 8bit，KEY_NO_A 是轴座序号，KIN 是 bit + chip 实例。
    kin_by_key_index: Dict[str, List[tuple]] = {}
    keyA_by_idx: Dict[str, List[tuple]] = {}
    keyB_to_gnd: List[tuple] = []
    import re as _re
    for mid, nets in module_nets.items():
        for n in nets:
            m1 = _re.match(r"^KIN_(\d+)_(\d+)$", n.upper())
            if m1:
                bit = int(m1.group(1))
                chip = int(m1.group(2))
                if 0 <= bit <= 7:
                    kin_by_key_index.setdefault(str(chip * 8 + bit), []).append((mid, n))
                continue
            m2 = _re.match(r"^KEY_NO_A[_]?(\d+)$", n.upper())
            if m2:
                keyA_by_idx.setdefault(m2.group(1), []).append((mid, n))
                continue
            m3 = _re.match(r"^KEY_NO_B[_]?(\d+)$", n.upper())
            if m3:
                keyB_to_gnd.append((mid, n))
    for idx in set(kin_by_key_index) & set(keyA_by_idx):
        for k in kin_by_key_index[idx]:
            for s in keyA_by_idx[idx]:
                # 只在 fixture 声明 dep 的 module 对之间 union
                if allowed_pairs is not None:
                    pair_key = tuple(sorted([k[0], s[0]]))
                    if pair_key not in allowed_pairs:
                        continue
                unions.append((k, s))
    for endpoint in keyB_to_gnd:
        unions.append((endpoint, (endpoint[0], "GND")))

    # 规则 1b: MCU PWM_UH/PWM_UL/... ↔ FD6288Q HIN1/LIN1/...（三相 BLDC）
    pwm_to_driver = {
        "PWM_UH": "HIN1",
        "PWM_UL": "LIN1",
        "PWM_VH": "HIN2",
        "PWM_VL": "LIN2",
        "PWM_WH": "HIN3",
        "PWM_WL": "LIN3",
        "PWM_H1": "HIN1",
        "PWM_L1": "LIN1",
        "PWM_H2": "HIN2",
        "PWM_L2": "LIN2",
        "PWM_H3": "HIN3",
        "PWM_L3": "LIN3",
    }
    net_locations: Dict[str, List[tuple]] = {}
    for mid, nets in module_nets.items():
        for n in nets:
            net_locations.setdefault(n.upper(), []).append((mid, n))
    for pwm, drv in pwm_to_driver.items():
        for a in net_locations.get(pwm, []):
            for b in net_locations.get(drv, []):
                if allowed_pairs is not None:
                    pair_key = tuple(sorted([a[0], b[0]]))
                    if pair_key not in allowed_pairs:
                        continue
                unions.append((a, b))

    # 规则 2-4: 协议总线同名 union（仅明确的协议 net）
    PROTOCOL_NET_PATTERNS = (
        _re.compile(r"^SPI\d*_(MOSI|MISO|SCK|CLK|CS|NSS)(_.*)?$", _re.IGNORECASE),
        _re.compile(r"^I2C\d*_(SCL|SDA)(_.*)?$", _re.IGNORECASE),
        _re.compile(r"^UART\d*_(TX|RX|TXD|RXD)(_.*)?$", _re.IGNORECASE),
        _re.compile(r"^USB_(DP|DM|D[+\-])(_.*)?$", _re.IGNORECASE),
    )
    name_to_modules: Dict[str, List[str]] = {}
    for mid, nets in module_nets.items():
        for n in nets:
            if any(p.match(n) for p in PROTOCOL_NET_PATTERNS):
                name_to_modules.setdefault(n, []).append(mid)
    for net, mods in name_to_modules.items():
        if len(mods) >= 2:
            anchor = (mods[0], net)
            for m in mods[1:]:
                unions.append((anchor, (m, net)))

    return unions


def _connection_pairs(connections, *, strict: bool) -> List[tuple]:
    """Decode current flat endpoints and the historical start/end shape."""
    pairs = []
    for index, connection in enumerate(connections or []):
        def invalid(message):
            if strict:
                raise ValueError(f"internal_connections[{index}]: {message}")
        if not isinstance(connection, Mapping):
            invalid("connection must be an object")
            continue
        flat_keys = {"source_module", "source_net", "target_module", "target_net"}
        if flat_keys.intersection(connection):
            if not flat_keys <= set(connection) or (strict and {"start", "end"}.intersection(connection)):
                invalid("flat connection requires source_module/source_net/target_module/target_net only")
                continue
            raw_pairs = [((connection["source_module"], connection["source_net"]),
                          (connection["target_module"], connection["target_net"]))]
        else:
            start, ends = connection.get("start"), connection.get("end")
            if isinstance(ends, Mapping):
                ends = [ends]
            if not isinstance(start, Mapping) or not isinstance(ends, list) or not ends:
                invalid("connection requires a start endpoint and nonempty end endpoints")
                continue
            raw_pairs = []
            for end in ends:
                if not isinstance(end, Mapping):
                    invalid("end endpoint must be an object")
                    continue
                raw_pairs.append(((start.get("module"), start.get("name")),
                                  (end.get("module"), end.get("name"))))
        for source, target in raw_pairs:
            if not all(isinstance(part, str) and part.strip() for part in (*source, *target)):
                invalid("module and net endpoint names must be nonempty strings")
                continue
            if _is_no_connect(source[1]) or _is_no_connect(target[1]):
                invalid("NC is a no-connect pin marker, not a top-port endpoint")
                continue
            if strict and source[0] == target[0]:
                invalid("top-level endpoints must belong to different modules")
                continue
            pairs.append((source, target))
    return pairs


def _unique_names(proposals: Dict[tuple, str]) -> Dict[tuple, str]:
    """Allocate readable names without conflating distinct tuple identities."""
    reserved, used, result = set(proposals.values()), set(), {}
    for identity in sorted(proposals):
        base = proposals[identity]
        name, suffix = base, 2
        while name in used or (name != base and name in reserved):
            name = f"{base}__{suffix}"
            suffix += 1
        used.add(name)
        result[identity] = name
    return result


def merge_netlists(per_module_netlists: List[Dict[str, Any]],
                   internal_connections: List[Dict[str, Any]] = None,
                   use_heuristic: bool = True,
                   fixture_dep_pairs: set = None,
                   *, merge_global_nets: bool = True,
                   strict_connections: bool = False) -> Dict[str, Any]:
    """Merge module fragments without changing device identities or pin numbers.

    Historical callers retain optional global/heuristic unions. Production uses
    ``use_heuristic=False, merge_global_nets=False, strict_connections=True``:
    only explicit top-port edges join modules, including their GND/power nets.
    Strict mode rejects missing/invalid endpoints and disables heuristic unions.
    Qualified-looking names are ordinary local names when globals are disabled.
    """
    modules: Dict[str, Mapping] = {}
    endpoints: set[Tuple[str, str]] = set()
    devices: Dict[tuple, Mapping] = {}
    for index, entry in enumerate(per_module_netlists):
        if not isinstance(entry, Mapping):
            raise ValueError(f"module entry {index} must be an object")
        mid = entry.get("module_id") or (None if strict_connections else "UNK")
        if not isinstance(mid, str) or not mid.strip():
            raise ValueError(f"module entry {index} requires a nonempty module_id")
        if mid in modules:
            raise ValueError(f"duplicate module_id: {mid}")
        nl = entry.get("netlist") or {}
        if not isinstance(nl, Mapping):
            raise ValueError(f"module {mid}: netlist must be an object")
        modules[mid] = nl
        for gge, device in nl.items():
            if not isinstance(gge, str) or not isinstance(device, Mapping):
                raise ValueError(f"module {mid}: device keys/entries must be strings/objects")
            pins = device.get("pins") or {}
            if not isinstance(pins, Mapping):
                raise ValueError(f"module {mid}, device {gge}: pins must be a mapping")
            devices[(mid, gge)] = device
            for net in pins.values():
                if net is None or net == "" or _is_no_connect(net):
                    continue
                if not isinstance(net, str):
                    raise ValueError(f"module {mid}, device {gge}: net must be text or null")
                endpoints.add((mid, net))

    parent: Dict[Tuple[str, str], Tuple[str, str]] = {key: key for key in endpoints}
    def find(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key
    def union(source, target):
        a, b = find(source), find(target)
        if a != b:
            parent[a] = b

    pairs = _connection_pairs(internal_connections, strict=strict_connections)
    for source, target in pairs:
        if strict_connections:
            for endpoint in (source, target):
                if endpoint not in endpoints:
                    raise ValueError(f"top-port endpoint does not exist: module={endpoint[0]!r}, net={endpoint[1]!r}")
        union(source, target)

    if merge_global_nets:
        shared: Dict[str, tuple] = {}
        for endpoint in sorted(endpoints):
            name = endpoint[1]
            if _is_global_net(name) or _is_explicit_module_net(name):
                if name in shared:
                    union(shared[name], endpoint)
                else:
                    shared[name] = endpoint
    if use_heuristic and not strict_connections:
        allowed_pairs = set(fixture_dep_pairs or [])
        allowed_pairs.update(tuple(sorted((source[0], target[0]))) for source, target in pairs)
        for source, target in _heuristic_cross_module_net_unions(
                per_module_netlists, allowed_pairs=allowed_pairs or None):
            union(source, target)

    groups: Dict[tuple, list] = {}
    for endpoint in sorted(endpoints):
        groups.setdefault(find(endpoint), []).append(endpoint)
    net_proposals = {}
    for root, members in groups.items():
        canonical = min(members) if strict_connections else root
        globals_in_group = [endpoint[1] for endpoint in members if _is_global_net(endpoint[1])]
        qualified = [endpoint[1] for endpoint in members if _is_explicit_module_net(endpoint[1])]
        if merge_global_nets and globals_in_group:
            name = min(globals_in_group)
        elif merge_global_nets and qualified:
            name = min(qualified)
        else:
            name = f"{canonical[0].replace('-', '_')}__{canonical[1]}"
        net_proposals[root] = name
    net_names = _unique_names(net_proposals)
    device_names = _unique_names({key: f"{key[0].replace('-', '_')}__{key[1]}" for key in devices})
    result = {}
    for key, device in devices.items():
        copied = deepcopy(dict(device))
        copied["pins"] = {
            pin: net_names[find((key[0], net))] if net is not None and net != "" and not _is_no_connect(net) else net
            for pin, net in (device.get("pins") or {}).items()
        }
        result[device_names[key]] = copied
    return result


def collect_gen_netlists(workflow_raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 raw 的 netlist_output 抽出每个 module 的 netlist + module_id。"""
    out = []
    for entry in workflow_raw.get("netlist_output", []) or []:
        nli = entry.get("netlist_info")
        if not isinstance(nli, dict):
            continue
        netlist = nli.get("netlist")
        if not isinstance(netlist, dict) or not netlist:
            continue
        mi = entry.get("module_info") or {}
        citations = entry.get("cite_info")
        citation_rows = citations if isinstance(citations, list) else [citations]
        out.append({
            "module_id": mi.get("id"),
            "supplier_part": mi.get("supplier_part") or "",
            "device_name": mi.get("device_name") or "",
            "netlist": netlist,
            "cite_info": deepcopy(citations),
            "is_virtualized": any(bool(row.get("is_virtualized"))
                                  for row in citation_rows if isinstance(row, Mapping)),
            "quantity": mi.get("quantity", 1),
        })
    return out

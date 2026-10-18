"""Lossless component/pin/net incidence graph for netlist formats 1.2 and 1.3.

Ownership edges are structural, never assertions that a device conducts.
Identifiers are scoped by document; null pins have no net node. No device,
including a zero-ohm resistor, is removed or electrically collapsed.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

ComponentKey = tuple[str, str]
PinKey = tuple[str, str, str]
NetKey = tuple[str, str]
CORE_TYPES = frozenset({"ic", "module", "connector", "crystal", "oscillator"})
PHYSICAL_DEVICE_TYPES = frozenset({"mosfet", "transistor", "microphone", "antenna"})
COMPONENT_FIELDS = frozenset({"name", "designator", "type", "value", "description", "id", "pins"})
ABSTRACT_COMPONENT_FIELDS = COMPONENT_FIELDS | {"is_abstract"}
PIN_FIELDS = frozenset({"id", "name", "net"})


class NetlistFormatError(ValueError):
    """An input cannot be represented without inventing or losing evidence."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NetlistFormatError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def read_netlist_json(path: str | Path) -> Any:
    """Read JSON without silently accepting duplicate component/field keys."""
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)


def is_cleaned_netlist(data: Any) -> bool:
    """Recognize even malformed cleaned records so they fail strict validation."""
    return isinstance(data, dict) and any(
        isinstance(item, dict) and ("designator" in item or isinstance(item.get("pins"), list))
        for item in data.values()
    )


def _string(value: Any, where: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip():
        raise NetlistFormatError(f"{where}: expected nonempty string{' or null' if nullable else ''}")


@dataclass(frozen=True)
class ComponentNode:
    key: ComponentKey
    designator: str
    type: str
    name: str | None
    value: str | None
    description: str | None
    supplier_id: str | None
    pins: tuple[PinKey, ...]
    is_abstract: bool | None = None
    identity_anchor: str | None = None

    @property
    def anchor_id(self) -> str | None:
        """Resolved evaluation metadata, separate from the exported source ID."""
        return self.identity_anchor if self.identity_anchor is not None else self.supplier_id

    @property
    def is_core(self) -> bool:
        """Use explicit 1.3 abstraction, retaining historical 1.2 boundaries."""
        if self.identity_anchor is not None:
            return True
        if self.is_abstract is None:
            return self.type in CORE_TYPES
        return self.type == "ic" and self.is_abstract is False


@dataclass(frozen=True)
class PinNode:
    key: PinKey
    component: ComponentKey
    id: str
    name: str | None
    net: NetKey | None


@dataclass(frozen=True)
class NetNode:
    key: NetKey
    pins: frozenset[PinKey]


@dataclass(frozen=True)
class PeripheralCircuit:
    """A whole common-device region with core pins as terminal boundaries.

Shared supply/ground nets can produce large regions. They are deliberately
not cut by net-name heuristics. Reachability here is structural, not DC/AC
conduction through every peripheral device (which also includes diodes/switches).
"""
    components: frozenset[ComponentKey]
    nets: frozenset[NetKey]
    boundary: frozenset[PinKey]


@dataclass(frozen=True)
class NetlistGraph:
    source: str
    components: Mapping[ComponentKey, ComponentNode]
    pins: Mapping[PinKey, PinNode]
    nets: Mapping[NetKey, NetNode]

    def pin_map(self, component: ComponentKey) -> dict[str, PinNode]:
        return {self.pins[key].id: self.pins[key] for key in self.components[component].pins}

    def connected_pins(self, pin: PinKey) -> frozenset[PinKey]:
        """Other pins wired directly to this pin, without traversing devices."""
        net = self.pins[pin].net
        return self.nets[net].pins - {pin} if net is not None else frozenset()

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "components": len(self.components), "pins": len(self.pins), "nets": len(self.nets),
            "unconnected_pins": sum(pin.net is None for pin in self.pins.values()),
            "core_components": sum(c.is_core for c in self.components.values()),
        }

    def peripheral_circuits(self) -> tuple[PeripheralCircuit, ...]:
        remaining = {key for key, c in self.components.items() if not c.is_core}
        circuits = []
        while remaining:
            todo = [min(remaining)]
            components: set[ComponentKey] = set()
            nets: set[NetKey] = set()
            boundary: set[PinKey] = set()
            while todo:
                key = todo.pop()
                if key in components:
                    continue
                components.add(key)
                remaining.discard(key)
                for pin_key in self.components[key].pins:
                    net = self.pins[pin_key].net
                    if net is None or net in nets:
                        continue
                    nets.add(net)
                    for neighbor in self.nets[net].pins:
                        owner = self.pins[neighbor].component
                        if self.components[owner].is_core:
                            boundary.add(neighbor)
                        elif owner not in components:
                            todo.append(owner)
            circuits.append(PeripheralCircuit(frozenset(components), frozenset(nets), frozenset(boundary)))
        return tuple(circuits)


def parse_netlist_graph(data: Mapping[str, Any], *, source: str = "<memory>",
                        allow_duplicate_designators: bool = False) -> NetlistGraph:
    """Validate uniform 1.2/1.3 records, with opt-in preserved multi-board designators."""
    if not isinstance(allow_duplicate_designators, bool):
        raise TypeError("allow_duplicate_designators must be a boolean")
    if not isinstance(data, dict):
        raise NetlistFormatError(f"{source}: netlist root must be an object keyed by gge IDs")
    components: dict[ComponentKey, ComponentNode] = {}
    pins: dict[PinKey, PinNode] = {}
    members: dict[NetKey, set[PinKey]] = defaultdict(set)
    designators: set[str] = set()
    has_abstraction: bool | None = None
    for component_id, item in data.items():
        where = f"{source}/{component_id}"
        if not isinstance(component_id, str) or not re.fullmatch(r"gge[0-9]+", component_id):
            raise NetlistFormatError(f"{where}: expected gge followed by digits")
        if not isinstance(item, dict) or set(item) not in (COMPONENT_FIELDS, ABSTRACT_COMPONENT_FIELDS):
            raise NetlistFormatError(
                f"{where}: expected exactly {sorted(COMPONENT_FIELDS)} (1.2) "
                "or those fields plus is_abstract (1.3)"
            )
        explicit_abstraction = "is_abstract" in item
        if has_abstraction is not None and explicit_abstraction != has_abstraction:
            raise NetlistFormatError(f"{where}: cannot mix 1.2 and 1.3 component records in one netlist")
        has_abstraction = explicit_abstraction
        for field in COMPONENT_FIELDS - {"pins"}:
            _string(item[field], f"{where}.{field}", nullable=field not in {"designator", "type"})
        if explicit_abstraction:
            if not isinstance(item["is_abstract"], bool):
                raise NetlistFormatError(f"{where}.is_abstract: expected boolean")
            if not re.fullmatch(r"[a-z][a-z0-9_]*", item["type"]) or item["type"] == "common_device":
                raise NetlistFormatError(f"{where}.type: expected a concrete lower_snake_case device type")
            if item["is_abstract"] != (item["type"] != "ic"):
                raise NetlistFormatError(f"{where}: only ic may have is_abstract=false; other types must be abstract")
            if item["is_abstract"] and item["id"] is not None:
                raise NetlistFormatError(f"{where}: abstract components must have null id")
        elif item["type"] not in CORE_TYPES | {"common_device"}:
            raise NetlistFormatError(f"{where}.type: unsupported cleaned type {item['type']!r}")
        if item["designator"] in designators and not allow_duplicate_designators:
            raise NetlistFormatError(f"{where}: duplicate designator {item['designator']!r}")
        designators.add(item["designator"])
        if not isinstance(item["pins"], list):
            raise NetlistFormatError(f"{where}.pins: expected array")
        key = (source, component_id)
        pin_keys = []
        for pin in item["pins"]:
            if not isinstance(pin, dict) or set(pin) != PIN_FIELDS:
                raise NetlistFormatError(f"{where}.pins: expected exactly {sorted(PIN_FIELDS)}")
            for field in PIN_FIELDS:
                _string(pin[field], f"{where}.pins.{field}", nullable=field != "id")
            pin_key = (*key, pin["id"])
            if pin_key in pins:
                raise NetlistFormatError(f"{where}: duplicate pin id {pin['id']!r}")
            net = (source, pin["net"]) if pin["net"] is not None else None
            pins[pin_key] = PinNode(pin_key, key, pin["id"], pin["name"], net)
            pin_keys.append(pin_key)
            if net is not None:
                members[net].add(pin_key)
        components[key] = ComponentNode(key, item["designator"], item["type"], item["name"],
                                        item["value"], item["description"], item["id"], tuple(pin_keys),
                                        is_abstract=item.get("is_abstract"))
    nets = {key: NetNode(key, frozenset(value)) for key, value in members.items()}
    return NetlistGraph(source, MappingProxyType(components), MappingProxyType(pins), MappingProxyType(nets))


def with_fixed_identity_anchors(graph: NetlistGraph, identities: Mapping[str, Mapping[str, Any]]) -> NetlistGraph:
    """Attach authenticated, typed non-IC identities without changing source data.

    Callers must verify their reference provenance or frozen compiler/catalog
    manifest before supplying this mapping. This function never infers identity
    from a model name, pin count, topology or an underlying IC. The exported 1.3
    abstraction and null ID remain intact; the typed overlay is evaluation data.
    """
    if not isinstance(identities, Mapping):
        raise NetlistFormatError("fixed identity anchors require an explicit typed mapping")
    components = dict(graph.components)
    for cid, identity in identities.items():
        if not isinstance(identity, Mapping):
            raise NetlistFormatError("fixed identity anchor must name its part, inventory kind and component type")
        part, kind, component_type = (identity.get(field) for field in ("supplier_part", "inventory_kind", "component_type"))
        key = (graph.source, cid)
        if key not in components or not isinstance(part, str) or not re.fullmatch(r"[CS][0-9]+", part):
            raise NetlistFormatError("fixed identity anchor has an unknown component or invalid C/S identity")
        component = components[key]
        supported = (kind == "functional_module" and component_type == "module" or
                     kind == "physical_device" and isinstance(component_type, str) and component_type in PHYSICAL_DEVICE_TYPES)
        if not supported or component.type != component_type or component.is_abstract is not True or component.supplier_id is not None:
            raise NetlistFormatError("fixed identity anchors require a matching supported 1.3 non-IC type with null source ID")
        if component.identity_anchor not in (None, part):
            raise NetlistFormatError("fixed identity anchor cannot replace an existing identity")
        components[key] = replace(component, identity_anchor=part)
    return replace(graph, components=MappingProxyType(components))


def with_functional_identity_anchors(graph: NetlistGraph, identities: Mapping[str, str]) -> NetlistGraph:
    """Keep the original module-only wrapper; physical devices need typed input."""
    if not isinstance(identities, Mapping):
        raise NetlistFormatError("functional identity anchors require an explicit mapping")
    return with_fixed_identity_anchors(graph, {cid: {"supplier_part": part,
        "inventory_kind": "functional_module", "component_type": "module"} for cid, part in identities.items()})


def load_netlist_graph(path: str | Path) -> NetlistGraph:
    source = Path(path).resolve()
    return parse_netlist_graph(read_netlist_json(source), source=str(source))


def load_case_graphs(path: str | Path) -> dict[str, NetlistGraph]:
    """Load cleaned documents from a case/dataset, excluding source artifacts."""
    source = Path(path).resolve()
    if source.is_file():
        return {source.name: load_netlist_graph(source)}
    if not source.is_dir():
        raise FileNotFoundError(source)
    excluded = {"__analysis__", "netlist", "eval", "device_report"}
    files = sorted(p for p in source.rglob("*.json")
                   if p.name != "core_devices.json"
                   and not excluded.intersection(p.relative_to(source).parts[:-1]))
    if not files:
        raise NetlistFormatError(f"{source}: no cleaned netlists")
    return {str(p.relative_to(source)): load_netlist_graph(p) for p in files}


def extract_pin_subgraph(graph: NetlistGraph, terminals: tuple[PinKey, ...]) -> NetlistGraph:
    """Extract complete peripheral branches rooted at selected core pins.

    Traverse nets and common devices, stopping at other core pins. Keep those
    pins as external ports, without traversing the core's body. This preserves
    shunt branches as well as series/parallel paths. A shared ground can make
    the result large; no arbitrary depth cutoff silently drops branches.
    Disconnected terminals retain their separate regions (and null pins).
    """
    if not terminals:
        raise ValueError("at least one core terminal is required")
    selected: set[PinKey] = set()
    nets: set[NetKey] = set()
    expanded: set[ComponentKey] = set()
    todo = list(terminals)
    for key in terminals:
        if key not in graph.pins or not graph.components[graph.pins[key].component].is_core:
            raise ValueError(f"not a core pin: {key!r}")
    while todo:
        key = todo.pop()
        if key in selected:
            continue
        selected.add(key)
        pin = graph.pins[key]
        if pin.net is not None and pin.net not in nets:
            nets.add(pin.net)
            todo.extend(graph.nets[pin.net].pins)
        if not graph.components[pin.component].is_core and pin.component not in expanded:
            expanded.add(pin.component)
            todo.extend(graph.components[pin.component].pins)
    components = {}
    for key in sorted({graph.pins[p].component for p in selected}):
        component = graph.components[key]
        components[key] = replace(component, pins=tuple(p for p in component.pins if p in selected))
    return NetlistGraph(graph.source, MappingProxyType(components),
                        MappingProxyType({p: graph.pins[p] for p in sorted(selected)}),
                        MappingProxyType({n: graph.nets[n] for n in sorted(nets)}))

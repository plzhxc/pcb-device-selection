"""Unique peripheral correspondences for partial connection diagnostics only."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
import re
from typing import Any

from .netlist_graph import ComponentKey, NetlistGraph


def extend_connection_correspondence(reference: NetlistGraph, generated: NetlistGraph,
                                     structural_mapping: Mapping[ComponentKey, ComponentKey]
                                     ) -> tuple[dict, list[dict[str, Any]]]:
    """Pair unique peripheral inventories adjacent to the same proven core.

    This does not prove circuit equivalence or a component's value position.
    Callers must keep the structural map separate for values and certificates.
    No added pair becomes an anchor for another addition. Physical pin numbers
    remain unchanged, and subsequent checks must compare complete peer sets.
    """
    mapping = dict(structural_mapping)
    if len(set(mapping.values())) != len(mapping) or any(
            left not in reference.components or right not in generated.components
            for left, right in mapping.items()):
        raise ValueError("structural correspondence must be an existing injective map")
    reverse = {right: left for left, right in mapping.items()}
    cores = {left: right for left, right in mapping.items()
             if reference.components[left].is_core and generated.components[right].is_core
             and re.fullmatch(r"[CS][0-9]+", reference.components[left].anchor_id or "")
             and reference.components[left].anchor_id == generated.components[right].anchor_id}
    reverse_cores = {right: left for left, right in cores.items()}
    groups = [defaultdict(list), defaultdict(list)]
    adjacent = [{}, {}]
    for side, graph in enumerate((reference, generated)):
        paired = mapping if side == 0 else reverse
        for key, component in graph.components.items():
            if key in paired or component.is_core:
                continue
            signature = (component.type, None if component.is_abstract is True else component.name,
                         tuple(sorted(graph.pin_map(key))))
            groups[side][signature].append(key)
            owners = {graph.pins[peer].component for pin in component.pins
                      for peer in graph.connected_pins(pin)}
            adjacent[side][key] = (owners & cores.keys() if side == 0 else
                                   {reverse_cores[owner] for owner in owners if owner in reverse_cores})
    evidence = []
    for signature, lefts in groups[0].items():
        rights = groups[1].get(signature, [])
        if len(lefts) != 1 or len(rights) != 1:
            continue
        left, right = lefts[0], rights[0]
        anchors = adjacent[0][left] & adjacent[1][right]
        if not anchors:
            continue
        mapping[left] = right
        evidence.append({"reference": left[1], "generated": right[1],
                         "kind": "unique_anchored_inventory_connection_correspondence",
                         "scope": "connection_only", "component_type": signature[0],
                         "pin_numbers": list(signature[2]),
                         "strict_core_neighbors": sorted(key[1] for key in anchors),
                         "proves_value_position": False, "proves_subcircuit_equivalence": False})
    return mapping, evidence


__all__ = ["extend_connection_correspondence"]

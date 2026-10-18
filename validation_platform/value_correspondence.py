"""Partial passive-value witnesses at fully anchored physical terminal positions."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
import hashlib
import json
import re
from typing import Any

from .electrical_values import normalized_value
from .netlist_graph import ComponentKey, NetlistGraph


def extend_value_correspondence(reference: NetlistGraph, generated: NetlistGraph,
                                structural_mapping: Mapping[ComponentKey, ComponentKey]
                                ) -> tuple[dict, list[dict[str, Any]]]:
    """Prove terminal positions before selecting values within those positions.

    Every terminal must directly share a net with an identical, nonempty set of
    strictly paired C/S core pins. Unpaired cores, reversed terminals, floating
    terminals and paths through components do not establish a position. Peripheral
    neighbors may differ: this proves a value position, not complete connectivity.
    Matching all equal values first maximizes equality within a position group;
    extra components remain unmatched and must remain in metric denominators.
    """
    mapping = dict(structural_mapping)
    if len(set(mapping.values())) != len(mapping) or any(
            left not in reference.components or right not in generated.components
            for left, right in mapping.items()):
        raise ValueError("structural correspondence must be an existing injective map")
    cores = {left: right for left, right in mapping.items()
             if reference.components[left].is_core and generated.components[right].is_core
             and re.fullmatch(r"[CS][0-9]+", reference.components[left].anchor_id or "")
             and reference.components[left].anchor_id == generated.components[right].anchor_id}
    reverse = {right: left for left, right in mapping.items()}
    reverse_cores = {right: left for left, right in cores.items()}
    groups = [defaultdict(list), defaultdict(list)]
    for side, graph in enumerate((reference, generated)):
        paired = mapping if side == 0 else reverse
        anchors = cores if side == 0 else reverse_cores
        for key, component in graph.components.items():
            if key in paired or component.is_core or component.type not in {"resistor", "capacitor", "inductor"}:
                continue
            position = []
            for pid, pin in sorted(graph.pin_map(key).items()):
                neighbors = graph.connected_pins(pin.key)
                core_pins = [graph.pins[peer] for peer in neighbors
                             if graph.components[graph.pins[peer].component].is_core]
                if not core_pins or any(peer.component not in anchors for peer in core_pins):
                    break
                endpoints = tuple(sorted(((peer.component if side == 0 else reverse_cores[peer.component])[1], peer.id)
                                         for peer in core_pins))
                position.append((pid, endpoints))
            else:
                if position:
                    signature = (component.type, None if component.is_abstract is True else component.name,
                                 tuple(position))
                    groups[side][signature].append(key)
    evidence = []
    for signature, lefts in sorted(groups[0].items(), key=lambda row: repr(row[0])):
        rights = groups[1].get(signature, [])
        available = set(rights)
        additions = []
        unmatched = []
        for left in sorted(lefts):
            value = normalized_value(reference.components[left].value)
            equal = [right for right in sorted(available)
                     if value == normalized_value(generated.components[right].value)]
            if equal:
                right = equal[0]
                available.remove(right)
                additions.append((left, right))
            else:
                unmatched.append(left)
        additions.extend(zip(unmatched, sorted(available)))
        witness = {"component_type": signature[0], "model": signature[1],
                   "terminal_core_pins": [[pid, [list(endpoint) for endpoint in endpoints]]
                                          for pid, endpoints in signature[2]]}
        encoded = json.dumps(witness, ensure_ascii=False, sort_keys=True).encode()
        for left, right in additions:
            mapping[left] = right
            evidence.append({"reference": left[1], "generated": right[1],
                             "kind": "complete_core_anchored_terminal_position",
                             "scope": "value_position", "witness": witness,
                             "witness_sha256": hashlib.sha256(encoded).hexdigest(),
                             "position_group_sizes": {"reference": len(lefts), "generated": len(rights)},
                             "value_equal": normalized_value(reference.components[left].value) ==
                                            normalized_value(generated.components[right].value),
                             "proves_subcircuit_equivalence": False})
    return mapping, evidence


__all__ = ["extend_value_correspondence"]

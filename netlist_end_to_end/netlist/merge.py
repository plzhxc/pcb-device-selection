"""Combine cleaned boards without changing designators or electrical topology."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any, Mapping

from validation_platform.netlist_graph import parse_netlist_graph


@dataclass(frozen=True)
class MergedNetlist:
    netlist: dict[str, dict[str, Any]]
    components: dict[str, dict[str, str]]
    nets: dict[str, dict[str, str]]


def merge_cleaned_netlists(
    boards: Mapping[str, Mapping[str, Any]],
) -> MergedNetlist:
    """Return a deterministic disjoint union of explicitly named boards.

    Board names must be unique ASCII identifiers. Original designators, pin
    numbers/names, null connections and all device properties are preserved.
    Only gge keys and non-null net labels are rewritten. The returned maps
    make both rewrites reversible; they are separate from the eight-field
    netlist. No same-name networks or connector pins are joined across boards.
    Each source board must satisfy the ordinary single-board input contract.
    """
    if not isinstance(boards, Mapping) or not boards:
        raise ValueError("at least one named board is required")
    for board, source in boards.items():
        if not isinstance(board, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", board):
            raise ValueError(f"invalid board namespace: {board!r}")
        parse_netlist_graph(source, source=board)
        if any("is_abstract" not in item for item in source.values()):
            raise ValueError(f"{board}: merging requires format 1.3")

    merged: dict[str, dict[str, Any]] = {}
    components: dict[str, dict[str, str]] = {}
    nets: dict[str, dict[str, str]] = {}
    for board in sorted(boards):
        source = boards[board]
        for old_key in sorted(source, key=lambda key: (int(key[3:]), key)):
            new_key = f"gge{len(merged) + 1}"
            item = deepcopy(source[old_key])
            for pin in item["pins"]:
                original = pin["net"]
                if original is not None:
                    scoped = f"{board}::{original}"
                    nets[scoped] = {"board": board, "net": original}
                    pin["net"] = scoped
            merged[new_key] = item
            components[new_key] = {
                "board": board,
                "component": old_key,
                "designator": item["designator"],
            }
    return MergedNetlist(merged, components, nets)

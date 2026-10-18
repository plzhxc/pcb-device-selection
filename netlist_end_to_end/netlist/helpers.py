"""Pure normalization helpers shared by netlist and top-port agents."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any


def as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def nonphysical_connection_device(props: Mapping[str, Any]) -> bool:
    """Recognize an admitted synthetic wire entry, not a real jumper/net tie.

    A name containing 'wire' or a zero-ohm value is insufficient. Only an
    untyped ``other`` entry that explicitly claims to be virtual or to exist
    solely as a connection placeholder is rejected here.
    """

    if str(props.get("deviceType") or "").strip().lower() != "other":
        return False
    if props.get("is_virtual") is True or props.get("physical") is False:
        return True
    name = str(props.get("deviceName") or "")
    notes = str(props.get("deviceNotes") or "")
    connection = re.search(r"\b(?:wire|connection|tie|net)\b|连线|连接|直连", name + " " + notes, re.I)
    virtual = re.search(
        r"(?:逻辑|连接|直连|连线|网络).{0,6}占位|(?:占位符|虚构器件)|"
        r"(?:无|无需|不需要)(?:独立|额外|实际|实体)?(?:器件|物料)|"
        r"(?:logical|virtual|synthetic|connection|wire)[ -]+placeholder|"
        r"no\s+(?:physical|actual|additional)\s+(?:component|part)|"
        r"only\s+to\s+satisfy\s+(?:the\s+)?schema",
        notes, re.I,
    )
    return bool(connection and virtual)


def cross_page(value: Any) -> dict[str, list[str]]:
    raw = as_mapping(value)

    def names(key: str) -> list[str]:
        rows = raw.get(key) or []
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            return []
        return list(dict.fromkeys(str(item) for item in rows if str(item)))

    return {"inputs": names("inputs"), "outputs": names("outputs")}


def netlist_info(value: Any) -> dict[str, Any]:
    payload = as_mapping(value)
    nested = payload.get("netlist_info")
    if isinstance(nested, Mapping):
        payload = dict(nested)
    netlist = payload.get("netlist")
    if not isinstance(netlist, Mapping):
        netlist = {}
    cross = payload.get("crossPageNet") or payload.get("cross_page_net") or {}
    return {
        **payload,
        "netlist": dict(netlist),
        "crossPageNet": cross_page(cross),
        "notes": str(payload.get("notes") or ""),
    }


__all__ = ["as_mapping", "cross_page", "netlist_info", "nonphysical_connection_device"]

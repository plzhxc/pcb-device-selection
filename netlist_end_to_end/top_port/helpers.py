"""Pure helpers for top-port result aggregation."""

from collections.abc import Mapping, Sequence
from typing import Any


def dedupe_dicts(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    seen: set[tuple[tuple[str, str], ...]] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        normalized = dict(row)
        key = tuple(
            sorted((str(name), repr(value)) for name, value in normalized.items())
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def dedupe_by_identity(rows: Sequence[Mapping[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Merge repeated claims, retaining distinct reasons and conflicting identities."""
    indexed: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = tuple(repr(row.get(field)) for field in fields)
        if key not in indexed:
            indexed[key] = dict(row)
            continue
        existing = indexed[key]
        reasons = list(existing.get("evidence_reasons") or [])
        for reason in (existing.get("reason"), row.get("reason")):
            if reason and reason not in reasons:
                reasons.append(reason)
        if len(reasons) > 1:
            existing["evidence_reasons"] = reasons
    return list(indexed.values())


def dependency_pairs(decomposition: Mapping[str, Any], available: set[str]) -> set[tuple[str, str]]:
    """Only caller-declared relationships; this never infers a connection."""
    pairs = set()
    for module in decomposition.get("modules") or []:
        if not isinstance(module, Mapping) or module.get("id") not in available:
            continue
        for dependency in module.get("dependencies") or []:
            target = dependency if isinstance(dependency, str) else None
            if target in available and target != module["id"]:
                pairs.add(tuple(sorted((module["id"], target))))
    for link in decomposition.get("links") or []:
        if not isinstance(link, Mapping):
            continue
        source, target = link.get("source"), link.get("target")
        if isinstance(source, str) and isinstance(target, str) and source in available and target in available and source != target:
            pairs.add(tuple(sorted((source, target))))
    return pairs


__all__ = ["dedupe_dicts", "dedupe_by_identity", "dependency_pairs"]

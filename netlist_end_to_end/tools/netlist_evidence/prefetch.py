"""Orchestration for bounded catalog and topology prefetch."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..milvus import DatasheetKnowledgeProvider
from ..sql import DeviceCatalogProvider
from .catalog import candidate_inputs, resolve_catalog_candidate
from .common import (
    MAX_OUTPUT_CHARS,
    PARAMETER_BUDGET,
    PIN_BUDGET,
    TOPOLOGY_BUDGET,
    as_mapping,
    bounded_text,
    compact_summary,
    issue,
    serialized_size,
)
from .topology import fetch_topology


class CatalogMilvusNetlistKnowledgePrefetcher:
    """Resolve up to three exact catalog candidates and their topology evidence."""

    name = "catalog_milvus_netlist_knowledge"
    max_output_chars = MAX_OUTPUT_CHARS

    def __init__(
        self,
        catalog: DeviceCatalogProvider,
        datasheets: DatasheetKnowledgeProvider,
    ) -> None:
        if not isinstance(catalog, DeviceCatalogProvider):
            raise TypeError("catalog must implement DeviceCatalogProvider")
        if not isinstance(datasheets, DatasheetKnowledgeProvider):
            raise TypeError("datasheets must implement DatasheetKnowledgeProvider")
        self.catalog = catalog
        self.datasheets = datasheets

    @staticmethod
    def compact_summary(evidence: Mapping[str, Any]) -> dict[str, Any]:
        return compact_summary(evidence)

    def prefetch(
        self,
        module: Mapping[str, Any],
        selection: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(module, Mapping) or not isinstance(selection, Mapping):
            raise TypeError("module and selection must be mappings")
        inputs = candidate_inputs(module, selection)
        if not inputs:
            return self._result(
                "not_applicable",
                {
                    "status": "no_candidates",
                    "candidate_results": [],
                    "resolved_candidates": [],
                },
                {"status": "not_checked", "items": [], "candidate_statuses": []},
                [],
            )

        issues: list[dict[str, str]] = []
        resolved: list[dict[str, Any]] = []
        candidate_results: list[dict[str, Any]] = []
        pin_budget = max(2_400, PIN_BUDGET // len(inputs))
        parameter_budget = max(700, PARAMETER_BUDGET // len(inputs))
        for candidate in inputs:
            result, candidate_issues = self._resolve_catalog(
                candidate,
                pin_budget=pin_budget,
                parameter_budget=parameter_budget,
            )
            issues.extend(candidate_issues)
            candidate_results.append(
                {key: value for key, value in result.items() if key != "device"}
            )
            if result.get("status") == "exact_ready":
                resolved.append(result)

        required_result = next(
            (
                row
                for row in [*resolved, *candidate_results]
                if bool(row.get("confirmed_c_number"))
            ),
            None,
        )
        if (
            required_result is not None
            and required_result.get("status") != "exact_ready"
        ):
            issues.append(
                issue(
                    "confirmed_catalog_identity_unavailable",
                    "catalog",
                    "module.confirmed_device could not be resolved exactly; alternatives cannot replace it.",
                )
            )
            return self._result(
                "failed",
                {
                    "status": "failed",
                    "candidate_results": candidate_results,
                    "resolved_candidates": resolved,
                    "required_supplier_part": None,
                    "evidence_source": bounded_text(self.catalog.name, 96),
                },
                {"status": "not_checked", "items": [], "candidate_statuses": []},
                issues,
            )

        if not resolved:
            return self._result(
                "failed",
                {
                    "status": "failed",
                    "candidate_results": candidate_results,
                    "resolved_candidates": [],
                    "required_supplier_part": None,
                    "evidence_source": bounded_text(self.catalog.name, 96),
                },
                {"status": "not_checked", "items": [], "candidate_statuses": []},
                issues,
            )

        topology_items: list[dict[str, Any]] = []
        topology_statuses: list[dict[str, Any]] = []
        topology_budget = max(1_000, TOPOLOGY_BUDGET // len(resolved))
        for candidate in resolved:
            device = as_mapping(candidate.get("device"))
            c_number = str(device.get("supplier_part") or "")
            topology, topology_issue = self._fetch_topology(
                c_number,
                budget=topology_budget,
            )
            topology_items.extend(topology.pop("items"))
            topology_statuses.append(topology)
            if topology_issue:
                issues.append(topology_issue)

        found = sum(row.get("status") == "found" for row in topology_statuses)
        if found == len(topology_statuses):
            topology_status = "found"
        elif found:
            topology_status = "partial"
        elif any(row.get("status") == "error" for row in topology_statuses):
            topology_status = "error"
        else:
            topology_status = "not_found"
        catalog_status = "matched" if len(resolved) == len(inputs) else "partial"
        required_supplier_part = next(
            (
                str(as_mapping(row.get("device")).get("supplier_part") or "")
                for row in resolved
                if bool(row.get("confirmed_c_number"))
            ),
            None,
        )
        topology_degraded = any(bool(row.get("degraded")) for row in topology_statuses)
        overall = (
            "ok"
            if catalog_status == "matched"
            and topology_status == "found"
            and not topology_degraded
            else "degraded"
        )
        result = self._result(
            overall,
            {
                "status": catalog_status,
                "candidate_results": candidate_results,
                "resolved_candidates": resolved,
                "required_supplier_part": required_supplier_part,
                "evidence_source": bounded_text(self.catalog.name, 96),
            },
            {
                "status": topology_status,
                "items": topology_items,
                "candidate_statuses": topology_statuses,
                "evidence_source": bounded_text(self.datasheets.name, 96),
            },
            issues,
        )
        return self._force_bound(result)

    def _resolve_catalog(
        self,
        candidate: Mapping[str, Any],
        *,
        pin_budget: int,
        parameter_budget: int,
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        return resolve_catalog_candidate(
            self.catalog,
            candidate,
            pin_budget=pin_budget,
            parameter_budget=parameter_budget,
        )

    def _fetch_topology(
        self,
        c_number: str,
        *,
        budget: int,
    ) -> tuple[dict[str, Any], dict[str, str] | None]:
        return fetch_topology(self.datasheets, c_number, budget=budget)

    @staticmethod
    def _result(
        status: str,
        catalog: Mapping[str, Any],
        topology: Mapping[str, Any],
        issues: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": status,
            "catalog": dict(catalog),
            "topology": dict(topology),
            "issues": [dict(row) for row in issues],
            "summary": {},
            "read_only": True,
        }
        result["summary"] = compact_summary(result)
        return result

    @staticmethod
    def _force_bound(result: dict[str, Any]) -> dict[str, Any]:
        if serialized_size(result) <= MAX_OUTPUT_CHARS:
            return result
        topology = as_mapping(result.get("topology"))
        for item in topology.get("items") or []:
            if isinstance(item, dict):
                item["excerpt"] = bounded_text(item.get("excerpt"), 160)
        topology["truncated"] = True
        for candidate in (
            as_mapping(result.get("catalog")).get("resolved_candidates") or []
        ):
            device = as_mapping(as_mapping(candidate).get("device"))
            if isinstance(device, dict):
                device["parameters"] = {}
                device["parameters_truncated"] = True
        result["issues"].append(
            issue(
                "evidence_payload_truncated",
                "prefetch",
                "The aggregate evidence payload was reduced to the public size bound.",
            )
        )
        result["status"] = "degraded"
        result["summary"] = compact_summary(result)
        if serialized_size(result) > MAX_OUTPUT_CHARS:
            resolved = as_mapping(result.get("catalog")).get("resolved_candidates")
            if isinstance(resolved, list):
                while len(resolved) > 1 and serialized_size(result) > MAX_OUTPUT_CHARS:
                    resolved.pop()
            result["summary"] = compact_summary(result)
        if serialized_size(result) > MAX_OUTPUT_CHARS:
            raise ValueError(
                "bounded netlist evidence unexpectedly exceeds 24000 characters"
            )
        return result


__all__ = ["CatalogMilvusNetlistKnowledgePrefetcher"]

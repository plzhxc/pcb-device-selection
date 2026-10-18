"""Service wiring for isolated semantic-generation experiments."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_framework.llm import AsyncLLMClient, OpenAICompatibleProvider
from datasheet_automation.knowledge.preparation import PreparationService
from datasheet_automation.knowledge.evidence import primary_document_sha256
from validation_platform.fixed_inventory import iter_fixed_devices

from ..config import agent_configs, describe, load_model_config
from ..netlist.semantic import generate_semantic_board
from .inputs import digest, validate_input
from .records import PinContractStore, RecordStore
from .roles import extract_device_roles


def model_runtime(config_path: str | None = None) -> tuple[Any, Any, dict, dict]:
    config = load_model_config(config_path)
    base, roles = agent_configs(config)
    key = os.environ.get("QWEN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("QWEN_API_KEY or DASHSCOPE_API_KEY is required")
    provider = OpenAICompatibleProvider(api_key=key,
        base_url=os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        name="dashscope", reasoning_style="dashscope")
    return AsyncLLMClient(provider, base.params), base, {"default": base, **roles}, describe(config)


def _rank_original_evidence(hits: list[dict], canonical: list[dict], *,
                            part: str, primary_sha: str, limit: int) -> tuple[list[dict], dict]:
    """Honor RAG rank using only authenticated bodies, never RAG text."""
    by_ref = {row["source_ref"]: row for row in canonical}
    ordered, seen, matched, unmatched = [], set(), [], []
    for rank, hit in enumerate(hits, 1):
        page, sha, ref = hit.get("page_idx"), hit.get("document_sha256"), hit.get("source_ref")
        # Legacy primary chunks are page locators, not sources of facts.
        if ref is None and sha == primary_sha and type(page) is int and page >= 0:
            ref = f"pdf:{sha}:page:{page + 1}"
        row = by_ref.get(ref) if isinstance(ref, str) else None
        if (row is None or hit.get("lichuang_id") != part or row.get("lichuang_id") != part
                or sha != row.get("document_sha256")
                or type(page) is not type(row.get("page_idx")) or page != row.get("page_idx")):
            unmatched.append({"rank": rank, "source_ref": ref,
                              "reason": "no exact authenticated owner/document/page/ref body"})
            continue
        matched.append({"rank": rank, "source_ref": ref})
        if ref not in seen:
            ordered.append(row)
            seen.add(ref)
    for row in canonical:
        if row["source_ref"] not in seen:
            ordered.append(row)
            seen.add(row["source_ref"])
    return ordered[:limit], {"matched_hits": matched, "unmatched_hits": unmatched}


def retrieve_primary_evidence(service: PreparationService, part: str, query: str, *, limit: int = 20) -> dict:
    """Use RAG to locate pages, then authenticate their content against SQL PDF bytes."""
    retrieved = service.read_evidence(part, query, limit=limit)
    verified = service.read_primary_source_evidence(part, query=query, limit=limit)
    if verified.get("pdf_status") != "verified_original_pdf":
        verified["retrieval_provenance"] = {"query": query, "rag_chunk_ids": [row["id"] for row in retrieved["evidence"]],
            "authenticator": "SQL original source bytes, SHA256 and reviewed transcription scope"}
        return verified
    pages = sorted({row["page_idx"] + 1 for row in retrieved["evidence"]
                    if type(row.get("page_idx")) is int and row["page_idx"] >= 0
                    and row.get("document_sha256") == primary_document_sha256(verified)})
    located = {"evidence": []}
    if pages:
        try:
            located = service.read_pdf_page_evidence(part, pages=pages, limit=limit)
        except ValueError:
            # Legacy page metadata can be wrong; query-ranked original pages remain available.
            located = {"evidence": []}
    chunks = {row["source_ref"]: row for row in [*located["evidence"], *verified["evidence"]]}
    verified["evidence"], ranking = _rank_original_evidence(retrieved["evidence"], list(chunks.values()),
        part=part, primary_sha=primary_document_sha256(verified), limit=limit)
    verified["retrieval_provenance"] = {"query": query, "rag_chunk_ids": [row["id"] for row in retrieved["evidence"]],
        "authenticator": "SQL original PDF bytes and recorded SHA256",
        "ordering": "exact authenticated RAG rank, then canonical source order", **ranking}
    return verified


def _read_cited_originals(service: PreparationService, source_reader: Any, part: str,
                          record: Mapping, missing: set[str], *, selected_revision: bool) -> dict:
    """Route exact citations to their own PDF, retaining existing source readers.

    Reviewed secondary bodies come from the canonical aggregate reader without
    passing secondary page numbers to its primary-PDF stage. That reader emits
    full verified review transcriptions independently of its native-page limit;
    collect_knowledge still admits only exact missing refs and fails if absent.
    """
    parsed = {ref: re.fullmatch(r"pdf:([0-9a-f]{64}):page:([0-9]+)(?::review:([0-9a-f]{64}))?", ref)
              for ref in missing}
    if any(match is None for match in parsed.values()):
        raise ValueError("exact original PDF citation required")
    canonical_primary = primary_document_sha256(record)
    selection = record.get("source_version_selection") if selected_revision else None
    primary = selection.get("selected_document_sha256") if isinstance(selection, Mapping) else canonical_primary
    if selected_revision and not isinstance(selection, Mapping):
        raise ValueError("explicit historical revision selection required")
    foreign = {ref: match for ref, match in parsed.items() if match[1] != primary}
    if selected_revision and foreign:
        raise ValueError("cited source is outside the selected historical revision")
    primary_pages = sorted({int(match[2]) for match in parsed.values() if match[1] == primary})
    secondary_review = any(match[3] is not None for match in foreign.values())
    fetched = source_reader(part, **({} if secondary_review else {"pages": primary_pages}), limit=200)
    if (primary_document_sha256(fetched) != canonical_primary
            or selected_revision and fetched.get("source_version_selection") != selection):
        raise ValueError("original document changed during cited-source lookup")
    fetched = {**fetched, "evidence": list(fetched.get("evidence", []))}
    if secondary_review and primary_pages:
        exact_primary = source_reader(part, pages=primary_pages, limit=200)
        if (exact_primary.get("supplier_part", part) != part
                or primary_document_sha256(exact_primary) != canonical_primary
                or selected_revision and exact_primary.get("source_version_selection") != selection):
            raise ValueError("original document changed during cited-source lookup")
        fetched["evidence"].extend(exact_primary.get("evidence", []))
    for sha in sorted({match[1] for match in foreign.values() if match[3] is None}):
        pages = sorted({int(match[2]) for match in foreign.values() if match[1] == sha and match[3] is None})
        fetched["evidence"].extend(service.read_secondary_pdf_evidence(
            part, pdf_sha256=sha, pages=pages, limit=200))
    return fetched


def collect_knowledge(parts: list[str], service: PreparationService,
                      contracts: PinContractStore | None = None, *,
                      source_version_selections: Mapping[str, Mapping] | None = None) -> dict[str, dict]:
    """Collect own sources, then route exact citations to their declared owner.

    Foreign sources remain in that owner's record for independent binder source
    bundles. Owners outside the explicit inventory are rejected rather than
    silently introducing another device or mislabelling its source documents.
    """
    from ..netlist.application_context import application_source_requirements, project_application_context

    result = {}
    selections = dict(source_version_selections or {})
    if set(selections) - set(parts):
        raise ValueError("revision selections cannot add owners outside the requested inventory")
    def source_reader(part: str, **options: Any) -> dict:
        if part in selections:
            return service.read_selected_source_evidence(part, source_selection=selections[part], **options)
        return service.read_primary_source_evidence(part, **options)
    for part in sorted(set(parts)):
        query = "pin description pin configuration application circuit external components decoupling recommended operating conditions unused pins 引脚 管脚 功能 描述 电源 电压 接地 典型 应用 外围 电路 未使用 悬空"
        record = source_reader(part, query=query, limit=200) if part in selections else retrieve_primary_evidence(service, part, query, limit=24)
        if part in selections:
            from datasheet_automation.knowledge.datasheet_revision import authenticate_revision_selection
            authenticate_revision_selection(record, selections[part])
        # These independently authenticated mappings identify CAD contacts only.
        # They cannot grant electrical roles, voltage limits or NC permissions.
        record["pin_identity_mappings"] = [] if part in selections else service.read_pin_identity_mappings(part)
        if contracts and record.get("document"):
            contract = contracts.latest_device(part, document_sha256=primary_document_sha256(record),
                **({"source_selection_sha256": selections[part]["selection_sha256"]} if part in selections else {}))
            if contract and contract.get("provenance", {}).get("source_authentication_version") in {2, 3}:
                record["contracts"] = contract
        # Catalog field receipts and pin-identity proofs are source obligations,
        # just like role contracts. Fetch their exact referenced original pages
        # before building an offline generation/authentication snapshot.
        field_reader = getattr(service, "read_catalog_field_evidence", None)
        record["catalog_field_evidence"] = field_reader(part) if callable(field_reader) else []
        if part in selections:
            record["catalog_field_evidence"] = [row for row in record["catalog_field_evidence"]
                if row.get("evidence", {}).get("source_selection_sha256") == selections[part]["selection_sha256"]]
        result[part] = record

    # All inventory owners must exist before resolving a producer's references
    # to a receiver's datasheet or application note. Request ordering is inert.
    obligations: dict[str, dict[str, int]] = {part: {} for part in result}
    for part, record in result.items():
        dependencies: dict[str, set[str]] = {}
        def cited_sources(value: Any) -> None:
            if isinstance(value, Mapping):
                ref = value.get("source_ref", "")
                match = re.fullmatch(r"pdf:[0-9a-f]{64}:page:([0-9]+)(?::review:[0-9a-f]{64})?", str(ref))
                if match:
                    owner = value.get("source_supplier_part", part)
                    if not isinstance(owner, str) or re.fullmatch(r"(?:C|S)[0-9]+", owner) is None:
                        raise ValueError(f"invalid cited source owner in {part}: {owner!r}")
                    if owner not in result:
                        raise ValueError(f"cited source owner {owner} required by {part} is outside requested knowledge inventory: {ref}")
                    obligations[owner][ref] = int(match[1])
                    if owner != part:
                        dependencies.setdefault(owner, set()).add(ref)
                for key, item in value.items():
                    # A registry inventories sources; only actual claims require them.
                    if key != "source_registry":
                        cited_sources(item)
            elif isinstance(value, list):
                for item in value:
                    cited_sources(item)
        cited_sources({key: record.get(key) for key in ("contracts", "catalog_field_evidence", "pin_identity_mappings")})
        cited_sources(application_source_requirements(record))
        record["source_citation_dependencies"] = [
            {"source_supplier_part": owner, "source_refs": sorted(refs), "adds_component": False}
            for owner, refs in sorted(dependencies.items())]

    for part, record in result.items():
        citations = obligations[part]
        def own_rows(bundle: Mapping) -> list:
            if bundle.get("supplier_part", part) != part:
                raise ValueError(f"source reader returned a different owner for {part}")
            rows = bundle.get("evidence", [])
            for row in rows:
                if row.get("lichuang_id", part) != part or row.get("supplier_part", part) != part:
                    raise ValueError(f"source row belongs to another owner in {part}: {row.get('source_ref')}")
            return rows
        present = {e["source_ref"] for e in own_rows(record)}
        if missing := set(citations) - present:
            fetched = _read_cited_originals(service, source_reader, part, record, missing,
                selected_revision=part in selections)
            merged = {}
            for row in [*record.get("evidence", []), *own_rows(fetched)]:
                previous = merged.get(row["source_ref"])
                if previous and any(previous.get(key) != row.get(key) for key in ("content", "document_sha256", "provenance")):
                    raise ValueError(f"cited source content changed for {part}: {row['source_ref']}")
                merged[row["source_ref"]] = row
            # Page lookups may include other documents with the same page number.
            # Keep initial retrieval intact and add only exact cited sources.
            record["evidence"] = [row for ref, row in merged.items()
                                  if ref in present or ref in missing]
            record["source_assets"] = fetched.get("source_assets", record.get("source_assets", []))
            record["symbol_mappings"] = fetched.get("symbol_mappings", record.get("symbol_mappings", []))
            if unavailable := missing - merged.keys():
                raise ValueError(f"cited original sources unavailable for {part}: {sorted(unavailable)}")
            for ref in missing:
                match = re.fullmatch(r"pdf:([0-9a-f]{64}):page:([0-9]+)(?::review:[0-9a-f]{64})?", ref)
                row = merged[ref]
                if (row.get("document_sha256") != match[1]
                        or type(row.get("page_idx")) is not int or row["page_idx"] != int(match[2]) - 1):
                    raise ValueError(f"cited original source identity mismatch for {part}: {ref}")
        ordered = sorted(record.get("evidence", []), key=lambda row: (row.get("page_idx") is None, row.get("page_idx") or 0, row.get("source_ref", "")))
        def alias(row: Mapping, index: int) -> str:
            source = (f":doc:{row['document_sha256'][:12]}"
                      if row.get("document_sha256") != primary_document_sha256(record) else "")
            if row.get("provenance") == "reviewed_original_pdf_transcription":
                return f"{part}{source}:p{row['page_idx'] + 1}:review:{row['source_quality']['review_receipt_sha256'][:12]}"
            return f"{part}{source}:p{row['page_idx'] + 1}" if type(row.get("page_idx")) is int else f"{part}:s{index + 1}"
        record["evidence"] = [{**row, "original_id": row["id"], "id": alias(row, index)} for index, row in enumerate(ordered)]
        application_context = project_application_context(record, record["evidence"])
        if application_context is not None:
            record["application_source_context"] = application_context
    return result


def generation_implementation_hashes() -> dict[str, str]:
    """Freeze both semantic stages and their source-owned supporting contracts."""
    package = Path(__file__).resolve().parents[1]
    source_root = package.parent
    paths = {Path(__file__).resolve(), package / "experiments" / "inputs.py",
             package / "experiments" / "cli.py"}
    for directory in (package / "netlist", package / "top_port", package / "common_devices", package / "config",
                      source_root / "agent_framework", source_root / "validation_platform"):
        paths.update(p for p in directory.rglob("*") if p.is_file()
                     and p.suffix in {".py", ".json", ".yaml", ".yml"} and "__pycache__" not in p.parts)
    # Source authentication follows helpers for terminal inventories, physical
    # positions and stored review assets. Freeze that package transitively so a
    # helper edit cannot masquerade as an unchanged generation implementation.
    paths.update(p for p in (source_root / "datasheet_automation" / "knowledge").rglob("*.py")
                 if p.is_file() and "__pycache__" not in p.parts)
    return {str(path.relative_to(source_root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


_LOADED_GENERATION_IMPLEMENTATION = generation_implementation_hashes()


async def prepare_roles(inputs: list[Mapping[str, Any]], *, service: PreparationService,
                        contracts: PinContractStore, records: RecordStore,
                        client: Any, config: Any, concurrency: int = 4,
                        refresh: bool = False, only_parts: list[str] | None = None) -> dict[str, Any]:
    if not 1 <= concurrency <= 16:
        raise ValueError("concurrency must be 1..16")
    parts = sorted({device["device_id"] for data in inputs for board in validate_input(data)["boards"]
                    for _, device in iter_fixed_devices(board) if device["device_id"]})
    if only_parts is not None:
        if (not isinstance(only_parts, list) or not only_parts
                or any(not isinstance(part, str) for part in only_parts)
                or len(set(only_parts)) != len(only_parts) or set(only_parts) - set(parts)):
            raise ValueError("only_parts must be a nonempty unique subset of the supplied input inventories")
        parts = sorted(only_parts)
    semaphore = asyncio.Semaphore(concurrency)

    async def one(part: str) -> dict[str, Any]:
        async with semaphore:
            try:
                knowledge = await asyncio.to_thread(service.read_device_knowledge, part, evidence_limit=8)
                document = knowledge.get("document") or {}
                old = await asyncio.to_thread(contracts.latest_device, part, document_sha256=primary_document_sha256(knowledge)) if document else None
                catalog_pins = [str(pin.get("pin_number")) for pin in knowledge.get("pins", [])]
                same_inventory = (bool(catalog_pins) and len(catalog_pins) == len(set(catalog_pins))
                                  and set(catalog_pins) == set((old or {}).get("pins", {}))
                                  and knowledge.get("device", {}).get("pin_count") == len(catalog_pins))
                if old and not refresh and same_inventory and old.get("provenance", {}).get("source_authentication_version") in {2, 3}:
                    return {"supplier_part": part, "status": "cached", "sha256": digest(old),
                            "pin_states": {state: sum(p.get("status") == state for p in old["pins"].values())
                                           for state in ("evidence_grounded", "partial", "unknown")}}
                if old and not refresh and not same_inventory:
                    records.event("pin_role_cache_inventory_changed", {"supplier_part": part,
                        "old_contract_sha256": digest(old), "old_pins": sorted(old.get("pins", {})),
                        "catalog_pins": sorted(catalog_pins),
                        "reason": "A shared PDF digest does not prove the catalog terminal inventory is unchanged."})
                result = await extract_device_roles(knowledge, client=client, config=config,
                    retrieve=lambda part, query, limit: retrieve_primary_evidence(service, part, query, limit=limit),
                    read_primary=lambda part: service.read_primary_source_evidence(part, limit=200),
                    on_event=lambda e: records.event("pin_role_agent", e))
                artifact = records.put("pin-contracts", result)
                if result.get("schema"):
                    await asyncio.to_thread(contracts.save_device, result)
                counts = {state: sum(p.get("status") == state for p in result.get("pins", {}).values())
                          for state in ("evidence_grounded", "partial", "unknown")}
                summary = {"supplier_part": part, "status": result["status"],
                           "pin_states": counts, "artifact": artifact}
            except Exception as exc:
                # Fail one item explicitly; independent devices can still progress.
                summary = {"supplier_part": part, "status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
            records.event("device_roles_completed", summary)
            return summary

    rows = await asyncio.gather(*(one(part) for part in parts))
    result = {"schema": "pcbagent.role_preparation.v1", "devices": rows, "requested_subset": only_parts,
              "extraction_completed": all(row["status"] in {"cached", "extracted"} for row in rows),
              "complete": all(row["status"] in {"cached", "extracted"} and not row.get("pin_states", {}).get("unknown")
                              and not row.get("pin_states", {}).get("partial") for row in rows)}
    records.put("role-preparation", result)
    return result


async def generate_case(data: Mapping[str, Any], *, service: PreparationService,
                        contracts: PinContractStore, records: RecordStore, client: Any,
                        configs: Mapping, model_description: Mapping,
                        concurrency: int = 2,
                        planning_strategy: str = "monolithic", planner_concurrency: int = 3,
                        max_module_core_pins: int = 128,
                        planning_validation_mode: str = "strict",
                        architecture_context_profile: str = "legacy_full_contracts",
                        selected_boards: list[str] | None = None,
                        knowledge_snapshots: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Run two stages with no reference manifest passed into the generator."""
    data = validate_input(data)
    from validation_platform.planning_obligations import PLANNING_VALIDATION_MODES
    if planning_validation_mode not in PLANNING_VALIDATION_MODES:
        raise ValueError("invalid planning validation mode")
    from ..netlist.semantic_presentation import ARCHITECTURE_CONTEXT_PROFILES
    if (architecture_context_profile not in ARCHITECTURE_CONTEXT_PROFILES
            or (architecture_context_profile != "legacy_full_contracts" and planning_strategy != "architecture")):
        raise ValueError("invalid or inapplicable architecture context profile")
    if type(concurrency) is not int or not 1 <= concurrency <= 16:
        raise ValueError("concurrency must be 1..16")
    if (planning_strategy not in {"monolithic", "architecture"} or type(planner_concurrency) is not int
            or not 1 <= planner_concurrency <= 16 or type(max_module_core_pins) is not int
            or not 1 <= max_module_core_pins <= 2000):
        raise ValueError("invalid planning strategy, planner concurrency or module core-pin budget")
    if selected_boards is not None and (not selected_boards or len(set(selected_boards)) != len(selected_boards)
            or set(selected_boards) - {board["board_id"] for board in data["boards"]}):
        raise ValueError("selected_boards must name a nonempty unique subset of this input's boards")
    run_id = "semantic_" + uuid4().hex
    input_record = records.put("inputs", data)
    semaphore = asyncio.Semaphore(concurrency)
    source_hashes = generation_implementation_hashes()
    configuration = {"models": dict(model_description),
        "planning_strategy": planning_strategy, "planner_concurrency": planner_concurrency,
        "planning_validation_mode": planning_validation_mode,
        "architecture_context_profile": architecture_context_profile,
        "max_module_core_pins": max_module_core_pins,
        "selected_boards": sorted(selected_boards) if selected_boards is not None else None,
        "role_parameters": {getattr(role, "value", role): config.params.digest() for role, config in configs.items()},
        "implementation": source_hashes}

    async def one(board: dict) -> dict:
        if selected_boards is not None and board["board_id"] not in selected_boards:
            return {"board_id": board["board_id"], "status": "not_scheduled",
                    "reason": "board diagnostic subset; reference denominators remain included"}
        async with semaphore:
            parts = [device["device_id"] for _, device in iter_fixed_devices(board) if device["device_id"]]
            try:
                if knowledge_snapshots is not None:
                    if board["board_id"] not in knowledge_snapshots:
                        raise ValueError("frozen repeat is missing a board knowledge snapshot")
                    knowledge = deepcopy(dict(knowledge_snapshots[board["board_id"]]))
                else:
                    knowledge = await asyncio.to_thread(collect_knowledge, parts, service, contracts)
                knowledge_record = records.put("knowledge-snapshots", knowledge)
                output = await generate_semantic_board(board, request=data["request"], requirements=data["requirements"],
                    knowledge=knowledge, client=client, configs=configs,
                    planning_strategy=planning_strategy, planner_concurrency=planner_concurrency,
                    max_module_core_pins=max_module_core_pins,
                    planning_validation_mode=planning_validation_mode,
                    architecture_context_profile=architecture_context_profile,
                    on_event=lambda e: records.event("semantic_agent", {**e, "experiment_run_id": run_id, "case_id": data["case_id"]}))
                output_record = records.put("generated-boards", output)
                return {"board_id": board["board_id"], "status": output["status"],
                        "planning_pending_count": len(output.get("architecture_obligations", {}).get("pending_planning_issues", [])),
                        "knowledge": knowledge_record, "output": output_record}
            except Exception as exc:
                result = {"board_id": board["board_id"], "status": "failed",
                          "error_type": type(exc).__name__, "error": str(exc)}
                records.event("board_generation_failed", {"run_id": run_id, **result})
                return result

    boards = await asyncio.gather(*(one(deepcopy(board)) for board in data["boards"]))
    result = {"schema": "pcbagent.semantic_case_run.v1", "run_id": run_id,
        "case_id": data["case_id"], "created_at": datetime.now(timezone.utc).isoformat(),
        "input": input_record, "models": dict(model_description), "configuration": configuration,
        "configuration_sha256": digest(configuration), "boards": boards,
        "configuration_stable": _LOADED_GENERATION_IMPLEMENTATION == source_hashes == generation_implementation_hashes(),
        "status": "compiled" if all(board["status"] == "compiled" for board in boards) else "incomplete",
        "planning_pending_count": sum(board.get("planning_pending_count", 0) for board in boards),
        "qualified": False, "qualification_reason": "independent electrical validation and four-metric evaluation still required"}
    result["record"] = records.put("case-runs", result)
    return result


__all__ = ["model_runtime", "collect_knowledge", "retrieve_primary_evidence", "prepare_roles", "generate_case", "generation_implementation_hashes"]

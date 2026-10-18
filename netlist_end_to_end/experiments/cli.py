"""Reproducible limited-input and two-stage experiment command."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Sequence

from datasheet_automation.knowledge.preparation import PreparationService, configure_local_services

from .inputs import build_case_input, digest, validate_input
from .records import PinContractStore, RecordStore
from .runtime import generate_case, model_runtime, prepare_roles

WORKLIB = Path(__file__).resolve().parents[3]
DEFAULT_ROOT = WORKLIB / "07_TEMP" / "case-semantic-validation" / "experiments"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare and run isolated netlist + top_port semantic cases")
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--local-services", action="store_true", help="Use the managed local MySQL deployment credentials in memory")
    commands = parser.add_subparsers(dest="command", required=True)
    inputs = commands.add_parser("inputs", help="Build limited inputs and separate evaluator manifests")
    inputs.add_argument("--case-root", type=Path, default=WORKLIB / "02_CASE" / "02_dataset")
    inputs.add_argument("--request-root", type=Path, default=WORKLIB / "02_CASE" / "02_dataset")
    inputs.add_argument("--cases", nargs="+")
    inputs.add_argument("--identity-map", type=Path)
    inputs.add_argument("--module-identity-map", type=Path,
                        help="Separate functional-module identity map, or its bindings artifact")
    inputs.add_argument("--physical-identity-map", type=Path,
                        help="Connection-free named physical device identity map, or its bindings artifact")
    inputs.add_argument("--functional-inventory", type=Path,
                        help="Optional connection-free functional-module inventory; otherwise enumerate source metadata")
    for name in ("roles", "generate"):
        command = commands.add_parser(name)
        command.add_argument("--input", type=Path, nargs="+", required=True)
        command.add_argument("--concurrency", type=int, default=2)
        command.add_argument("--model-config")
        if name == "roles":
            command.add_argument("--refresh", action="store_true", help="Append a fresh extraction version; retain old facts")
            command.add_argument("--only-parts", nargs="+", help="Unique C/S subset of these inputs' fixed device inventories")
        else:
            command.add_argument("--planning-strategy", choices=("monolithic", "architecture"), default="monolithic")
            command.add_argument("--planning-validation-mode", choices=("strict", "retain_unresolved_candidate"), default="strict",
                                 help="Explicit diagnostic continuation; retained issues still prevent independent qualification")
            command.add_argument("--architecture-context-profile", choices=("legacy_full_contracts", "stage_boundary_v1"),
                                 default="legacy_full_contracts", help="Architecture display only; full frozen facts remain authoritative")
            command.add_argument("--planner-concurrency", type=int, default=3)
            command.add_argument("--max-module-core-pins", type=int, default=128)
            command.add_argument("--boards", nargs="+", help="Diagnostic subset; unscheduled boards remain in case denominators")
            command.add_argument("--knowledge-from-run", type=Path,
                                 help="Reuse verified board knowledge records from one prior case run; requires one --input")
    evaluate = commands.add_parser("evaluate", help="Validate generation, then compute unchanged raw and justified adjusted scores")
    evaluate.add_argument("--run", type=Path, required=True)
    evaluate.add_argument("--reference-manifest", type=Path, required=True)
    evaluate.add_argument("--reference-contracts", type=Path)
    evaluate.add_argument("--permissions", type=Path)
    evaluate.add_argument("--certificates", type=Path)
    evaluate.add_argument("--source-locations", type=Path,
                          help="Evaluator-only JSON map of full original source paths to relocated paths; frozen SHA256 checks remain required")
    qualify = commands.add_parser("qualify", help="Check at least three distinct frozen evaluation runs")
    qualify.add_argument("--evaluation", type=Path, nargs="+", required=True)
    return parser


def _read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _read_identity_map(path: Path | None, *wrappers: str) -> dict:
    if path is None:
        return {}
    value = _read_object(path)
    for wrapper in wrappers:
        if wrapper in value:
            value = value[wrapper]
            break
    if not isinstance(value, dict) or any(not isinstance(row, dict) for row in value.values()):
        raise ValueError(f"identity map must map source locators to identity objects: {path}")
    return value


def _read_record(record: dict, *, relative_to: Path) -> dict:
    if (not isinstance(record, dict) or not isinstance(record.get("path"), str)
            or not isinstance(record.get("sha256"), str)):
        raise ValueError("frozen knowledge requires content-addressed path/sha256 records")
    path = Path(record["path"])
    value = _read_object(path if path.is_absolute() else relative_to / path)
    if digest(value) != record["sha256"]:
        raise ValueError(f"frozen record content digest mismatch: {path}")
    return value


def _frozen_knowledge(path: Path, data: dict, selected_boards: list[str] | None) -> tuple[dict, dict]:
    """Verify the parent records before any database or model operation.

    A changed input is permitted for a controlled input experiment and recorded
    explicitly. Missing board snapshots never fall back to current knowledge.
    """
    run = _read_object(path)
    if run.get("schema") != "pcbagent.semantic_case_run.v1" or run.get("case_id") != data["case_id"]:
        raise ValueError("frozen knowledge run must belong to this input's case")
    source_input = validate_input(_read_record(run.get("input"), relative_to=path.parent))
    if source_input["case_id"] != data["case_id"]:
        raise ValueError("frozen run input belongs to another case")
    required = set(selected_boards if selected_boards is not None else [board["board_id"] for board in data["boards"]])
    available = {board["board_id"] for board in data["boards"]}
    if not required or required - available or (selected_boards is not None and len(required) != len(selected_boards)):
        raise ValueError("selected boards must be a nonempty unique subset of the new input")
    source_boards = {board["board_id"] for board in source_input["boards"]}
    rows = run.get("boards")
    if (not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("board_id"), str) for row in rows)
            or len({row["board_id"] for row in rows}) != len(rows)
            or {row["board_id"] for row in rows} - source_boards):
        raise ValueError("frozen run has invalid or duplicate board records")
    indexed = {row["board_id"]: row for row in rows}
    snapshots, hashes = {}, {}
    for board in sorted(required):
        if board not in indexed or not indexed[board].get("knowledge"):
            raise ValueError(f"frozen repeat is missing knowledge for {board}; select only recorded boards or provide another run")
        snapshots[board] = _read_record(indexed[board]["knowledge"], relative_to=path.parent)
        hashes[board] = indexed[board]["knowledge"]["sha256"]
    provenance = {"source_run_path": str(path.resolve()), "source_run_sha256": digest(run),
        "source_run_id": run.get("run_id"), "case_id": data["case_id"],
        "source_input_sha256": digest(source_input), "input_sha256": digest(data),
        "input_changed": digest(source_input) != digest(data), "board_knowledge_sha256": hashes,
        "knowledge_policy": "frozen_records_only"}
    return snapshots, provenance


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "generate" and args.knowledge_from_run and len(args.input) != 1:
        parser.error("--knowledge-from-run requires exactly one --input")
    records = RecordStore(args.runtime_root)
    if args.local_services:
        configure_local_services()
    if args.command == "qualify":
        from .evaluation import qualify_repeats
        result = qualify_repeats([json.loads(path.read_text(encoding="utf-8")) for path in args.evaluation])
        result["record"] = records.put("qualifications", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["qualified"] else 2
    if args.command == "evaluate":
        from .evaluation import evaluate_case_run
        def read(path):
            return json.loads(path.read_text(encoding="utf-8")) if path else None
        result = evaluate_case_run(read(args.run), read(args.reference_manifest), records=records,
            reference_contracts=read(args.reference_contracts), permissions=read(args.permissions),
            certificates=read(args.certificates), source_locations=read(args.source_locations))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["candidate_pass"] else 2
    if args.command == "inputs":
        identity = _read_identity_map(args.identity_map, "identity_map")
        module_identity = _read_identity_map(args.module_identity_map, "bindings", "module_identity_map")
        physical_identity = _read_identity_map(args.physical_identity_map, "bindings", "physical_identity_map")
        functional_inventory = _read_object(args.functional_inventory) if args.functional_inventory else None
        chosen = set(args.cases or [])
        rows = []
        for directory in sorted(args.case_root.iterdir()):
            if not directory.is_dir() or (chosen and directory.name not in chosen):
                continue
            has_reference = (directory / f"{directory.name}-cleaned.json").is_file()
            if not has_reference:
                continue
            request_path = args.request_root / directory.name / "eval" / "input.txt"
            request = request_path.read_text(encoding="utf-8")
            data, manifest = build_case_input(directory, request, identity_map=identity,
                module_identity_map=module_identity, functional_inventory=functional_inventory,
                physical_identity_map=physical_identity)
            rows.append({"case_id": data["case_id"], "input": records.put("inputs", data),
                         "reference": records.put("reference-manifests", manifest)})
        missing = chosen - {row["case_id"] for row in rows}
        if missing:
            raise ValueError("requested cases not found: " + ", ".join(sorted(missing)))
        output = {"schema": "pcbagent.case_input_index.v1", "cases": rows}
        output["record"] = records.put("input-indexes", output)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    data = [validate_input(json.loads(path.read_text(encoding="utf-8"))) for path in args.input]
    snapshots = repeat_provenance = None
    if args.command == "generate" and args.knowledge_from_run:
        snapshots, repeat_provenance = _frozen_knowledge(args.knowledge_from_run, data[0], args.boards)
    service = PreparationService()
    contracts = PinContractStore(service.open_catalog_connection)
    if snapshots is None:
        contracts.ensure_schema()
    client, base, configs, models = model_runtime(args.model_config)
    if args.command == "roles":
        result = asyncio.run(prepare_roles(data, service=service, contracts=contracts, records=records,
            client=client, config=base, concurrency=args.concurrency, refresh=args.refresh, only_parts=args.only_parts))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["complete"] else 2

    async def run() -> list[dict]:
        outputs = []
        for payload in data:
            result = await generate_case(payload, service=service, contracts=contracts, records=records,
                client=client, configs=configs, model_description=models, concurrency=args.concurrency,
                planning_strategy=args.planning_strategy, planner_concurrency=args.planner_concurrency,
                planning_validation_mode=args.planning_validation_mode,
                architecture_context_profile=args.architecture_context_profile,
                max_module_core_pins=args.max_module_core_pins, selected_boards=args.boards,
                knowledge_snapshots=snapshots)
            if repeat_provenance is not None:
                records.event("frozen_knowledge_repeat", {**repeat_provenance,
                    "result_run_id": result["run_id"], "result_record": result["record"]})
            outputs.append(result)
        return outputs

    outputs = asyncio.run(run())
    print(json.dumps(outputs, ensure_ascii=False, indent=2))
    return 0 if all(row["status"] == "compiled" and row.get("planning_pending_count", 0) == 0 for row in outputs) else 2


if __name__ == "__main__":
    raise SystemExit(main())

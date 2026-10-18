"""Build the knowledge base for every core device of one evaluation case.

For each identified non-abstract IC in the cleaned case's core_devices.json, run the full
datasheet_automation chain (LCSC search → PDF → MinerU → LLM/VLM optimisation)
and load the result into the v2 store.  Devices whose run directory already
holds an optimised content list are ingested without re-running the chain.

    python -m datasheet_automation.main ingest-case --case case1-keyboard [--only C8304 ...] [--limit N] [--reuse-only] [--workers 3]

Devices run ``workers`` at a time (each MinerU parse caps itself at 16 CPU
threads; each optimisation run fans its model calls out on its own pool), and
store writes are serialised with a lock.

The report is printed as JSON and written next to the runs under
``07_TEMP/datasheet_automation/knowledge/<case>.report.json``.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from datasheet_automation.core.app_logging import get_logger
from datasheet_automation.core.config import default_config
from datasheet_automation.core.io import DATASHEET_RUNTIME_ROOT

from .store import KnowledgeStore

log = get_logger()
WORKLIB = Path(__file__).resolve().parents[3]
CASE_ROOT = WORKLIB / "02_CASE" / "02_dataset"
_DEVICE_ID = re.compile(r"[CS][0-9]+")


def case_core_devices(case: str) -> list[dict[str, Any]]:
    """Confirmed C/S identities from the cleaned IC inventory, with instance counts."""

    if not case or Path(case).name != case or case in {".", ".."}:
        raise ValueError("case must be one directory name under cleaned case")
    path = CASE_ROOT / case / "core_devices.json"
    inventory = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(inventory, list):
        raise ValueError(f"core inventory must be a list: {path}")
    found: dict[str, dict[str, Any]] = {}
    for component in inventory:
        if not isinstance(component, dict):
            raise ValueError(f"invalid core inventory entry: {path}")
        if component.get("type") != "ic" or component.get("is_abstract") is not False:
            continue
        identity = str(component.get("id") or "").strip().upper()
        if not _DEVICE_ID.fullmatch(identity):
            raise ValueError(f"core IC has missing/invalid C/S identity: {component.get('name')!r}")
        instances = component.get("instances")
        quantity = component.get("quantity")
        if not isinstance(instances, list) or type(quantity) is not int or quantity < 1 or quantity != len(instances):
            raise ValueError(f"core IC quantity/instances mismatch: {identity}")
        name = str(component.get("name") or "").strip()
        row = found.setdefault(identity, {"lichuang_id": identity, "name": name, "instances": 0})
        if row["name"] and name and row["name"] != name:
            raise ValueError(f"conflicting models for core identity {identity}")
        row["name"] = row["name"] or name
        row["instances"] += quantity
    if not found:
        raise ValueError(f"no identified non-abstract ICs in {path}")
    return sorted(found.values(), key=lambda r: (-r["instances"], r["lichuang_id"]))


def _existing_run(c_number: str) -> Path | None:
    root = DATASHEET_RUNTIME_ROOT / "runs"
    for candidate in (root / c_number, DATASHEET_RUNTIME_ROOT / c_number):
        if any(candidate.rglob(f"opti_{c_number}_content_list.json")) if candidate.is_dir() else False:
            return candidate
    hits = sorted(DATASHEET_RUNTIME_ROOT.rglob(f"opti_{c_number}_content_list.json")) if DATASHEET_RUNTIME_ROOT.exists() else []
    return hits[0].parents[1] if hits else None


def ingest_case(
    case: str,
    *,
    only: Iterable[str] = (),
    limit: int | None = None,
    reuse_only: bool = False,
    workers: int = 3,
    store: KnowledgeStore | None = None,
) -> dict[str, Any]:
    from datasheet_automation.core.application import run as run_request

    devices = case_core_devices(case)
    wanted = {c.strip().upper() for c in only if c.strip()}
    if wanted:
        unknown = wanted - {device["lichuang_id"] for device in devices}
        if unknown:
            raise ValueError(f"--only IDs not in the cleaned core inventory: {', '.join(sorted(unknown))}")
        devices = [d for d in devices if d["lichuang_id"] in wanted]
    if limit is not None:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        devices = devices[:limit]
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be a positive integer")
    store = store or KnowledgeStore()
    schema = store.ensure_schema()

    lock = threading.Lock()

    def process(device: dict[str, Any]) -> dict[str, Any]:
        c_number = device["lichuang_id"]
        started = time.time()
        entry: dict[str, Any] = {**device, "status": "pending"}
        try:
            run_dir = _existing_run(c_number)
            if run_dir is None:
                if reuse_only:
                    entry.update(status="skipped", reason="no existing run and --reuse-only")
                    return entry
                if c_number.startswith("S"):
                    raise ValueError(f"{c_number} is a custom identity; parse/optimize its confirmed source PDF with this ID before ingesting")
                request = default_config("full", query=c_number)
                request["options"]["reuse"] = True
                response = run_request(request)
                if response.get("status") not in {"ok", "degraded"}:
                    raise RuntimeError(f"pipeline did not succeed: {response.get('status')}")
                pipeline = response.get("data") or {}
                raw_run_dir = (pipeline.get("artifacts") or {}).get("run_dir")
                if not raw_run_dir or not Path(str(raw_run_dir)).is_dir():
                    raise RuntimeError("pipeline did not return a readable run directory")
                run_dir = Path(str(raw_run_dir))
                entry["pipeline_status"] = response.get("status")
                entry["pipeline_warnings"] = response.get("warnings") or []
                selected = (pipeline.get("collection") or {}).get("selected_product") or {}
                if str(selected.get("product_code") or "").strip().upper() != c_number:
                    raise ValueError(f"pipeline selected a different device than {c_number}")
                visual_failures = [str(w) for w in entry["pipeline_warnings"] if any(mark in str(w) for mark in ("image_unresolved:", "visual_agent_failed:", "table_agent_failed:"))]
                if visual_failures:
                    raise ValueError(f"pipeline visual processing incomplete: {visual_failures}")
                device["name"] = device["name"] or str(selected.get("product_model") or selected.get("name") or "")
            with lock:
                report = store.ingest_run(run_dir, c_number, product_name=device["name"])
            if report.get("milvus_rows", 0) < 1:
                raise RuntimeError("ingestion returned no Milvus chunks")
            entry.update(status="ok", ingest=report, seconds=round(time.time() - started, 1))
        except Exception as exc:  # one device must not stop the batch
            log.error("ingest-case | %s failed | %s: %s", c_number, type(exc).__name__, exc)
            entry.update(status="failed", error=f"{type(exc).__name__}: {exc}", seconds=round(time.time() - started, 1))
        return entry

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        results = list(pool.map(process, devices))

    summary = {
        "case": case,
        "store": {"milvus": f"{store.config.milvus_url}/{store.config.milvus_database}.{store.config.milvus_collection}", "mysql": f"{store.config.mysql_host}:{store.config.mysql_port}/{store.config.mysql_database}", "schema_created": schema},
        "workers": int(workers),
        "devices_total": len(devices),
        "instances_total": sum(device["instances"] for device in devices),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "milvus_rows": sum(r.get("ingest", {}).get("milvus_rows", 0) for r in results),
        "images_stored": sum(r.get("ingest", {}).get("images_stored", 0) for r in results),
        "results": results,
    }
    out_dir = DATASHEET_RUNTIME_ROOT / "knowledge"
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (out_dir / f"{case}.report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return summary


__all__ = ["case_core_devices", "ingest_case"]

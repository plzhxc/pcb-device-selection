"""Application facade for one configured datasheet workflow request."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .config import PlatformConfig, load_config
from datasheet_automation.delivery.manifest import prune_intermediate, write_manifest
from .collection import CollectionConfig, DatasheetCollector
from .app_logging import configure, get_logger
from datasheet_automation.parsing.protocol import DEFAULT_MINERU_SOURCE, MinerURequest
from datasheet_automation.parsing.transaction import parse_mineru
from .pipeline import OptimizationConfig, run_full_pipeline, run_optimization
from .runtime import RuntimeServices


RESPONSE_SCHEMA = "datasheet-automation.response.v1"


def run(
    config: str | Path | Mapping[str, Any] | PlatformConfig,
    *,
    runtime: RuntimeServices | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one YAML or mapping request and return a versioned result."""

    cfg = config if isinstance(config, PlatformConfig) else load_config(config)
    configure(cfg.logging)
    log = get_logger()
    services = RuntimeServices.from_value(runtime)

    log.info("request start | operation=%s", cfg.operation)
    services.trace("datasheet_automation.request.start", operation=cfg.operation, trace=cfg.trace)
    try:
        data = _dispatch(cfg, services)
    except Exception as exc:
        log.error(
            "request failed | operation=%s | %s: %s",
            cfg.operation,
            type(exc).__name__,
            exc,
        )
        services.trace(
            "datasheet_automation.request.error",
            operation=cfg.operation,
            error_type=type(exc).__name__,
            message=str(exc),
            trace=cfg.trace,
        )
        raise

    warnings = _warnings_from(data)
    status = "degraded" if warnings else "ok"
    log.info("request end | operation=%s | status=%s", cfg.operation, status)
    services.trace(
        "datasheet_automation.request.end",
        operation=cfg.operation,
        status=status,
        trace=cfg.trace,
    )
    return {
        "schema_version": RESPONSE_SCHEMA,
        "operation": cfg.operation,
        "status": status,
        "data": data,
        "warnings": warnings,
        "trace": cfg.trace,
    }


def _dispatch(cfg: PlatformConfig, services: RuntimeServices) -> dict[str, Any]:
    if cfg.operation == "search":
        query = _required_text(cfg.payload, "query")
        limit = int(cfg.options.get("candidate_limit", cfg.options.get("limit", 10)))
        products = DatasheetCollector(
            CollectionConfig(candidate_limit=limit), runtime=services
        ).search(query)
        return {"query": query, "candidates": [product.asdict() for product in products]}

    if cfg.operation == "collect_and_parse":
        query = _required_text(cfg.payload, "query")
        return DatasheetCollector(
            _collection_config(cfg.options), runtime=services
        ).collect_and_parse(query).asdict()

    if cfg.operation == "full":
        query = _required_text(cfg.payload, "query")
        return run_full_pipeline(
            query,
            collection_config=_collection_config(cfg.options),
            optimization_config=_optimization_config(cfg.options),
            runtime=services,
        )

    if cfg.operation == "parse":
        request = _parse_request(cfg)
        result = parse_mineru(request, services.mineru_backend)
        response = result.asdict()
        if not cfg.options.get("raw_dump"):
            response["pruned"] = prune_intermediate(result)
            response["manifest"] = write_manifest(result)
        return response

    if cfg.operation == "optimize":
        mineru = _required_text(cfg.payload, "mineru")
        optimization = _optimization_config(cfg.options, payload=cfg.payload)
        return run_optimization(
            mineru=mineru,
            config=optimization,
            runtime=services,
            lichuang_id=_optional_text(cfg.payload, "lichuang_id"),
        )

    raise AssertionError(cfg.operation)


def _parse_request(cfg: PlatformConfig) -> MinerURequest:
    mineru = cfg.options.get("mineru") or {}
    options = cfg.options
    return MinerURequest(
        pdf_path=Path(_required_text(cfg.payload, "pdf")),
        output_dir=Path(_required_text(cfg.payload, "output_dir")),
        command=mineru.get("command"),
        source_root=Path(str(mineru.get("source") or DEFAULT_MINERU_SOURCE)),
        backend=str(mineru.get("backend") or "pipeline"),
        method=str(mineru.get("method") or "auto"),
        lang=str(mineru.get("lang") or "ch"),
        formula=bool(mineru.get("formula", False)),
        table=bool(mineru.get("table", False)),
        api_url=mineru.get("api_url"),
        server_url=mineru.get("server_url"),
        timeout_s=(float(mineru["timeout_s"]) if mineru.get("timeout_s") else None),
        overwrite=bool(options.get("overwrite", False)),
        reuse_existing=bool(options.get("reuse", True)),
        execute=bool(options.get("execute", True)),
    )


def _collection_config(options: Mapping[str, Any]) -> CollectionConfig:
    mineru = options.get("mineru") or {}
    values: dict[str, Any] = {}
    top = {
        "output_root": ("output_root", Path),
        "overwrite": ("overwrite", bool),
        "candidate_limit": ("candidate_limit", int),
        "run_mineru": ("run_mineru", bool),
        "reuse": ("reuse_mineru_output", bool),
        "price_ladder": ("fetch_price_ladder", bool),
    }
    for key, (attribute, cast) in top.items():
        if options.get(key) is not None:
            values[attribute] = cast(options[key])
    mineru_map = {
        "command": "mineru_command",
        "backend": "mineru_backend",
        "method": "mineru_method",
        "lang": "mineru_lang",
        "formula": "mineru_formula",
        "table": "mineru_table",
        "api_url": "mineru_api_url",
        "server_url": "mineru_server_url",
        "timeout_s": "mineru_timeout_s",
    }
    for key, attribute in mineru_map.items():
        if mineru.get(key) is not None:
            values[attribute] = mineru[key]
    if mineru.get("source") is not None:
        values["mineru_source"] = Path(mineru["source"])
    return CollectionConfig(**values)


def _optimization_config(
    options: Mapping[str, Any], *, payload: Mapping[str, Any] | None = None
) -> OptimizationConfig:
    raw = options.get("optimization") or {}
    if not isinstance(raw, Mapping):
        raise TypeError("config.options.optimization must be a mapping")
    output_dir = (payload or {}).get("output_dir", raw.get("output_dir"))
    return OptimizationConfig(
        output_dir=Path(str(output_dir)) if output_dir else None,
        output_subdir=str(raw.get("output_subdir") or "optimized"),
        chunk_size=int(raw.get("chunk_size", 5000)),
        agent_mode=str(raw.get("agent_mode") or "required"),
        use_vlm=bool(raw.get("use_vlm", True)),
        llm_model=_optional_value(raw, "llm_model", "qwen-plus"),
        vlm_model=_optional_value(raw, "vlm_model", "qwen-vl-plus"),
        reasoning=raw.get("reasoning"),
        concurrency=int(raw.get("concurrency", 6)),
    )


def _warnings_from(data: Any) -> list[str]:
    if not isinstance(data, Mapping) or not isinstance(data.get("warnings"), list):
        return []
    return [str(item) for item in data["warnings"]]


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = str(payload.get(name) or "").strip()
    if not value:
        raise ValueError(f"payload.{name} is required")
    return value


def _optional_text(payload: Mapping[str, Any], name: str) -> str | None:
    value = str(payload.get(name) or "").strip()
    return value or None


def _optional_value(mapping: Mapping[str, Any], name: str, default: str) -> str | None:
    value = mapping.get(name, default)
    return None if value is None else str(value)

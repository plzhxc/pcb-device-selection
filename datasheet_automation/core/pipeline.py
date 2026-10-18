"""Complete LCSC-to-optimized-datasheet workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .collection import CollectionConfig, DatasheetCollector
from .io import runtime_output_path
from .runtime import RuntimeServices


@dataclass(frozen=True)
class OptimizationConfig:
    """Options owned by the MinerU-to-optimized-document stage."""

    output_dir: Path | None = None
    output_subdir: str = "optimized"
    chunk_size: int = 5000
    agent_mode: str = "required"
    use_vlm: bool = True
    llm_model: str | None = "qwen-plus"
    vlm_model: str | None = "qwen-vl-plus"
    reasoning: bool | None = None
    concurrency: int = 6

    def __post_init__(self) -> None:
        if self.output_dir is not None:
            object.__setattr__(
                self,
                "output_dir",
                runtime_output_path(self.output_dir),
            )

    def resolve_output_dir(self, run_dir: Path) -> Path:
        if self.output_dir is not None:
            return runtime_output_path(self.output_dir)
        subdir = Path(self.output_subdir)
        if subdir.is_absolute() or not subdir.parts or ".." in subdir.parts:
            raise ValueError("optimization.output_subdir must be a safe relative path")
        return runtime_output_path(run_dir / subdir)


def run_optimization(
    *,
    mineru: str | Path,
    config: OptimizationConfig,
    runtime: RuntimeServices | Mapping[str, Any] | None = None,
    lichuang_id: str | None = None,
) -> dict[str, Any]:
    """Run only the optimization stage against an existing MinerU result."""

    services = RuntimeServices.from_value(runtime)
    if config.output_dir is None:
        raise ValueError("payload.output_dir is required for operation='optimize'")
    from datasheet_automation.optimization.application import run_datasheet_optimization

    return run_datasheet_optimization(
        mineru=mineru,
        output_dir=config.output_dir,
        lichuang_id=lichuang_id,
        chunk_size=config.chunk_size,
        agent_mode=config.agent_mode,
        use_vlm=config.use_vlm,
        llm_model=config.llm_model,
        vlm_model=config.vlm_model,
        reasoning=config.reasoning,
        concurrency=config.concurrency,
        agent_factory=services.optimization_agent_factory,
    )


def run_full_pipeline(
    query: str,
    *,
    collection_config: CollectionConfig | None = None,
    optimization_config: OptimizationConfig | None = None,
    runtime: RuntimeServices | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Acquire, parse and optimize one datasheet in a single execution chain."""

    services = RuntimeServices.from_value(runtime)
    optimization = optimization_config or OptimizationConfig()
    collection_report = DatasheetCollector(
        collection_config or CollectionConfig(), runtime=services
    ).collect_and_parse(query).asdict()

    artifacts = collection_report.get("artifacts") or {}
    raw_content_list = Path(str(artifacts.get("raw_content_list_json") or ""))
    run_dir = Path(str(artifacts.get("run_dir") or ""))
    if not raw_content_list.is_file():
        raise RuntimeError(
            "collection completed without a readable MinerU content list: "
            f"{raw_content_list}"
        )
    if not run_dir.is_dir():
        raise RuntimeError(
            f"collection completed without a readable run directory: {run_dir}"
        )

    selected = collection_report.get("selected_product") or {}
    product_code = str(selected.get("product_code") or query).strip()
    output_dir = optimization.resolve_output_dir(run_dir)

    from datasheet_automation.optimization.application import run_datasheet_optimization

    optimization_report = run_datasheet_optimization(
        mineru=raw_content_list,
        output_dir=output_dir,
        lichuang_id=product_code,
        chunk_size=optimization.chunk_size,
        agent_mode=optimization.agent_mode,
        use_vlm=optimization.use_vlm,
        llm_model=optimization.llm_model,
        vlm_model=optimization.vlm_model,
        reasoning=optimization.reasoning,
        concurrency=optimization.concurrency,
        agent_factory=services.optimization_agent_factory,
    )
    warnings = [
        *(str(item) for item in collection_report.get("warnings") or []),
        *(str(item) for item in optimization_report.get("warnings") or []),
    ]
    return {
        "pipeline": "datasheet_automation.full.v1",
        "query": query,
        "product_code": product_code,
        "collection": collection_report,
        "optimization": optimization_report,
        "artifacts": {
            "run_dir": str(run_dir),
            "raw_content_list": str(raw_content_list),
            "optimized_dir": str(output_dir),
            "optimized_markdown": optimization_report["outputs"]["markdown"],
            "optimized_content_list": optimization_report["outputs"]["content_list"],
        },
        "warnings": warnings,
    }


__all__ = [
    "CollectionConfig",
    "DatasheetCollector",
    "OptimizationConfig",
    "run_full_pipeline",
    "run_optimization",
]

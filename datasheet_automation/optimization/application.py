"""Composition root for the datasheet optimization application."""

from __future__ import annotations

from pathlib import Path

from .agents.factory import DefaultOptimizationAgentFactory
from .datasheet_optimization_pipeline import DatasheetOptimizationPipeline
from .protocols import OptimizationAgentFactory


def run_datasheet_optimization(
    *,
    mineru: str | Path,
    output_dir: str | Path,
    lichuang_id: str | None = None,
    chunk_size: int = 5000,
    agent_mode: str = "required",
    use_vlm: bool = True,
    llm_model: str | None = None,
    vlm_model: str | None = None,
    reasoning: bool | None = None,
    agent_factory: OptimizationAgentFactory | None = None,
    concurrency: int = 6,
) -> dict:
    """Wire concrete Agents to the business pipeline and run one document."""

    resolved_factory = agent_factory
    if resolved_factory is None and agent_mode != "off":
        resolved_factory = DefaultOptimizationAgentFactory()
    return DatasheetOptimizationPipeline(
        chunk_size=chunk_size,
        agent_mode=agent_mode,
        use_vlm=use_vlm,
        llm_model=llm_model,
        vlm_model=vlm_model,
        reasoning=reasoning,
        agent_factory=resolved_factory,
        concurrency=concurrency,
    ).run(mineru=mineru, output_dir=output_dir, lichuang_id=lichuang_id)


__all__ = ["run_datasheet_optimization"]

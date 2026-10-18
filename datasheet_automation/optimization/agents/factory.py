"""Concrete bridge from optimization ports to ``agent_framework`` Agents."""

from __future__ import annotations

from agent_framework import AgentServices

from ..protocols import (
    BlockCoherenceOptimizer,
    LayoutOptimizer,
    SemanticSegmenter,
    TableOptimizer,
    VisualOptimizer,
)
from ..settings import build_optimization_agent_services
from .text import (
    BlockCoherenceAgent,
    LayoutOptimizationAgent,
    SemanticSegmentationAgent,
)
from .visual import TableOptimizationAgent, VisualUnderstandingAgent


class DefaultOptimizationAgentFactory:
    """Create concrete Agents while keeping their services outside the pipeline."""

    def __init__(self, services: AgentServices | None = None) -> None:
        self.services = services or build_optimization_agent_services()

    def layout(
        self, *, model: str | None, reasoning: bool | None
    ) -> LayoutOptimizer:
        return LayoutOptimizationAgent(
            model=model, reasoning=reasoning, services=self.services
        )

    def segmentation(
        self, *, model: str | None, reasoning: bool | None
    ) -> SemanticSegmenter:
        return SemanticSegmentationAgent(
            model=model, reasoning=reasoning, services=self.services
        )

    def coherence(
        self, *, model: str | None, reasoning: bool | None
    ) -> BlockCoherenceOptimizer:
        return BlockCoherenceAgent(
            model=model, reasoning=reasoning, services=self.services
        )

    def visual(
        self, *, model: str | None, reasoning: bool | None
    ) -> VisualOptimizer:
        return VisualUnderstandingAgent(
            model=model, reasoning=reasoning, services=self.services
        )

    def table(
        self, *, model: str | None, reasoning: bool | None
    ) -> TableOptimizer:
        return TableOptimizationAgent(
            model=model, reasoning=reasoning, services=self.services
        )


__all__ = ["DefaultOptimizationAgentFactory"]

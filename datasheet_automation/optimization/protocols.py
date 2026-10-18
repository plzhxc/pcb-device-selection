"""Optimization-owned ports consumed by the pipeline.

The pipeline knows these behavioral contracts only. It does not import
``BaseAgent`` or any concrete Agent implementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class LayoutOptimizer(Protocol):
    def optimize(self, markdown: str) -> tuple[str, str]: ...


class SemanticSegmenter(Protocol):
    def segment(self, markdown: str) -> list[dict[str, str]]: ...


class BlockCoherenceOptimizer(Protocol):
    def merge(self, blocks: list[dict[str, str]]) -> list[dict[str, str]]: ...


class VisualOptimizer(Protocol):
    def analyze(
        self,
        *,
        image_path: str | Path,
        img_path: str,
        caption: str = "",
        context_before: str = "",
        context_after: str = "",
    ) -> dict[str, str]: ...


class TableOptimizer(Protocol):
    def optimize(self, **values: Any) -> dict[str, str]: ...


class OptimizationAgentFactory(Protocol):
    """Construction port so each pipeline call receives fresh Agent state."""

    def layout(
        self, *, model: str | None, reasoning: bool | None
    ) -> LayoutOptimizer: ...

    def segmentation(
        self, *, model: str | None, reasoning: bool | None
    ) -> SemanticSegmenter: ...

    def coherence(
        self, *, model: str | None, reasoning: bool | None
    ) -> BlockCoherenceOptimizer: ...

    def visual(
        self, *, model: str | None, reasoning: bool | None
    ) -> VisualOptimizer: ...

    def table(
        self, *, model: str | None, reasoning: bool | None
    ) -> TableOptimizer: ...


__all__ = [
    "BlockCoherenceOptimizer",
    "LayoutOptimizer",
    "OptimizationAgentFactory",
    "SemanticSegmenter",
    "TableOptimizer",
    "VisualOptimizer",
]

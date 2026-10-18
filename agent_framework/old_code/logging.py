"""Minimal standard-library logger used outside PCBAgent applications."""

from __future__ import annotations

import json
import logging
from typing import Any


class StructuredLogger:
    """Adapt :mod:`logging` to the framework's structured logger contract."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    @staticmethod
    def _render(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, default=str)

    def debug(self, msg: Any, *, immediate: bool = True) -> None:
        self._logger.debug(self._render(msg))

    def info(self, msg: Any, *, immediate: bool = True) -> None:
        self._logger.info(self._render(msg))

    def warning(self, msg: Any, *, immediate: bool = True) -> None:
        self._logger.warning(self._render(msg))

    def error(self, msg: Any, *, immediate: bool = True) -> None:
        self._logger.error(self._render(msg))


__all__ = ["StructuredLogger"]

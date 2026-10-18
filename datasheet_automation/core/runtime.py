"""Request-scoped outbound integrations for the unified pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from datasheet_automation.lcsc.pricing import PriceSigner
from datasheet_automation.lcsc.transport import Fetcher
from datasheet_automation.parsing.protocol import MinerUBackend


@dataclass(frozen=True)
class RuntimeServices:
    """Inject external effects without coupling business stages together."""

    mineru_backend: MinerUBackend | None = None
    fetcher: Fetcher | None = None
    price_signer: PriceSigner | None = None
    optimization_agent_factory: Any = None
    trace_sink: Any = None

    @classmethod
    def from_value(cls, value: RuntimeServices | Mapping[str, Any] | None) -> RuntimeServices:
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        allowed = cls.__dataclass_fields__
        unknown = sorted(set(value) - set(allowed))
        if unknown:
            raise ValueError(f"unknown datasheet runtime service(s): {', '.join(unknown)}")
        return cls(**dict(value))

    def trace(self, event: str, **payload: Any) -> None:
        sink = self.trace_sink
        if sink is None:
            return
        try:
            if callable(sink):
                sink(event, payload)
                return
            emit = getattr(sink, "emit", None)
            if callable(emit):
                emit(event, payload)
        except Exception:
            # Observability must never change datasheet processing behaviour.
            return

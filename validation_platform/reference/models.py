"""Typed results shared by reference-netlist format adapters.

Adapters normalize source files into the historical flat netlist schema while
keeping source capability separate from the data.  In particular, an empty
``pins`` mapping in a BOM is *not* evidence that connectivity was checked.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


PinIdentity = Literal["number", "source_token", "unavailable"]
UnifiedNetlist = dict[str, dict[str, Any]]


class ReferenceCapabilityError(ValueError):
    """Raised when a caller requests evidence absent from the source format."""


@dataclass(frozen=True)
class ReferenceCapability:
    """Evidence that an adapter can faithfully expose from its source."""

    components: bool
    values: bool
    connectivity: bool
    pin_identity: PinIdentity
    schematic_context: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def require(self, *names: str) -> None:
        """Fail closed when a requested boolean capability is unavailable."""

        missing = [name for name in names if not bool(getattr(self, name, False))]
        if missing:
            raise ReferenceCapabilityError(
                "reference source does not provide: " + ", ".join(sorted(missing))
            )


@dataclass(frozen=True)
class ReferenceSource:
    """Digest-backed provenance for one source file actually consumed."""

    path: str
    size_bytes: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReferenceProvenance:
    """Adapter identity and exact input files used to build a netlist."""

    format: str
    adapter: str
    adapter_version: str
    sources: tuple[ReferenceSource, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "sources": [source.as_dict() for source in self.sources],
        }


@dataclass(frozen=True)
class ReferenceLoadResult:
    """Normalized reference data plus the evidence needed to interpret it."""

    netlist: UnifiedNetlist
    capability: ReferenceCapability
    provenance: ReferenceProvenance
    warnings: tuple[str, ...] = ()

    def require(self, *capabilities: str) -> "ReferenceLoadResult":
        self.capability.require(*capabilities)
        return self

    def as_dict(self, *, include_netlist: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "capability": self.capability.as_dict(),
            "provenance": self.provenance.as_dict(),
            "warnings": list(self.warnings),
        }
        if include_netlist:
            payload["netlist"] = self.netlist
        return payload

"""Reference adapter registry and fail-closed format detection."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from validation_platform.reference.bom import Utf16BomAdapter
from validation_platform.reference.easyeda_pcb import EasyEdaPcbExtractAdapter
from validation_platform.reference.enet import EasyEdaEnetAdapter
from validation_platform.reference.models import ReferenceLoadResult


class ReferenceAdapter(Protocol):
    format_name: str
    aliases: tuple[str, ...]

    def can_load(self, path: Path) -> bool: ...

    def load(self, path: Path) -> ReferenceLoadResult: ...


class ReferenceFormatError(ValueError):
    """Raised when a source format is unsupported or ambiguous."""


_ADAPTERS: tuple[ReferenceAdapter, ...] = (
    EasyEdaEnetAdapter(),
    EasyEdaPcbExtractAdapter(),
    Utf16BomAdapter(),
)


def available_reference_formats() -> tuple[str, ...]:
    return tuple(adapter.format_name for adapter in _ADAPTERS)


def _adapter_for_name(name: str) -> ReferenceAdapter:
    normalized = str(name or "").strip().lower()
    for adapter in _ADAPTERS:
        names = {adapter.format_name.lower(), *(alias.lower() for alias in adapter.aliases)}
        if normalized in names:
            return adapter
    supported = ", ".join(available_reference_formats())
    raise ReferenceFormatError(f"unsupported reference format {name!r}; supported: {supported}")


def load_reference(
    path: str | Path,
    *,
    reference_format: str | None = None,
) -> ReferenceLoadResult:
    """Load one reference asset or an unambiguous reference directory.

    Directories may contain README, evidence manifests, prompts, and core-part
    CSV files.  Only files positively recognized by adapters are consumed.  If
    more than one *format* is present, callers must select one explicitly.
    """

    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"reference source does not exist: {source}")
    if reference_format:
        adapter = _adapter_for_name(reference_format)
        if not adapter.can_load(source):
            raise ReferenceFormatError(
                f"{source} does not contain a valid {adapter.format_name} asset"
            )
        return adapter.load(source)

    matches = [adapter for adapter in _ADAPTERS if adapter.can_load(source)]
    if not matches:
        supported = ", ".join(available_reference_formats())
        raise ReferenceFormatError(
            f"could not detect a reference format at {source}; supported: {supported}"
        )
    if len(matches) > 1:
        formats = ", ".join(adapter.format_name for adapter in matches)
        raise ReferenceFormatError(
            f"ambiguous reference directory {source}; detected {formats}; "
            "pass reference_format explicitly"
        )
    return matches[0].load(source)


def load_reference_dir(
    path: str | Path,
    *,
    reference_format: str | None = None,
) -> ReferenceLoadResult:
    """Directory-oriented spelling retained for case scaffolding code."""

    return load_reference(path, reference_format=reference_format)

"""Small shared helpers for reference adapters."""

from __future__ import annotations

import hashlib
from pathlib import Path

from validation_platform.reference.models import ReferenceSource


def source_provenance(path: Path) -> ReferenceSource:
    content = path.read_bytes()
    return ReferenceSource(
        path=str(path.resolve()),
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def direct_files(path: Path, pattern: str) -> list[Path]:
    if path.is_file():
        return [path] if path.match(pattern) else []
    if not path.is_dir():
        return []
    return sorted(candidate for candidate in path.glob(pattern) if candidate.is_file())

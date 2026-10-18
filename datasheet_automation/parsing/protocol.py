"""Versioned value objects for the injectable MinerU boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol


MINERU_REQUEST_SCHEMA = "datasheet-automation.mineru.request.v1"
MINERU_RESULT_SCHEMA = "datasheet-automation.mineru.result.v1"

# MinerU ships as a self-contained portable bundle at the Worklib top level
# (Worklib/mineru-portable): its ``env/`` carries a Python with mineru
# installed in site-packages plus all deps, and ``mineru-models/`` carries the
# weights. The CLI backend runs the bundle's own ``mineru-run`` wrapper, which
# owns all runtime env setup (LD_LIBRARY_PATH / model config / threads), so
# nothing here needs an external environment. ``source_root`` points at the
# bundle root; overridable via config. A bare MinerU checkout (no env/) still
# works via the engine's legacy PYTHONPATH path.
DEFAULT_MINERU_SOURCE = Path(__file__).resolve().parents[3] / "mineru-portable"


@dataclass(frozen=True)
class MinerURequest:
    """One validated PDF parse request passed to a MinerU backend."""

    pdf_path: Path
    output_dir: Path
    schema_version: str = MINERU_REQUEST_SCHEMA
    command: Optional[str] = None
    source_root: Path = DEFAULT_MINERU_SOURCE
    backend: str = "pipeline"
    method: str = "auto"
    lang: str = "ch"
    # Formula and table parsing default OFF: the delivered Markdown renders
    # formulas poorly, so both are kept as image screenshots instead of being
    # converted to LaTeX / HTML. Callers may re-enable per request.
    formula: bool = False
    table: bool = False
    api_url: Optional[str] = None
    server_url: Optional[str] = None
    timeout_s: Optional[float] = None
    overwrite: bool = False
    reuse_existing: bool = True
    execute: bool = True


@dataclass
class MinerUResult:
    """Local, validated MinerU assets returned by a backend."""

    output_dir: Path
    content_list_json: Path
    schema_version: str = MINERU_RESULT_SCHEMA
    status: str = "completed"
    source: str = "mineru_cli"
    markdown_file: Optional[Path] = None
    raw_files: list[Path] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    stdout_tail: str = ""
    stderr_tail: str = ""

    def asdict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "source": self.source,
            "output_dir": str(self.output_dir),
            "content_list_json": str(self.content_list_json),
            "markdown_file": str(self.markdown_file) if self.markdown_file else None,
            "raw_files": [str(path) for path in self.raw_files],
            "command": self.command,
            "duration_s": self.duration_s,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


class MinerUBackend(Protocol):
    """Parse one request without owning reuse, overwrite, or promotion."""

    def parse(self, request: MinerURequest) -> MinerUResult:
        ...

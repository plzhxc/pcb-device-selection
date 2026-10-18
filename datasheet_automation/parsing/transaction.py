"""MinerU run-or-reuse transaction: stage, validate, promote, roll back.

Fresh executions are staged into an isolated hidden dir and promoted only after
validation, so a timeout or failed overwrite can never corrupt the last
known-good output. This orchestration is the platform's own asset; MinerU has
no notion of it.
"""

from __future__ import annotations

from dataclasses import replace
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4

from datasheet_automation.core.app_logging import get_logger
from datasheet_automation.core.io import ensure_dir, runtime_output_path
from .protocol import (
    MINERU_REQUEST_SCHEMA,
    MINERU_RESULT_SCHEMA,
    MinerUBackend,
    MinerURequest,
    MinerUResult,
)


log = get_logger()
_CONTENT_LIST_SUFFIX = "_content_list.json"

SUPPORTED_BACKENDS = {
    "pipeline",
    "vlm-http-client",
    "hybrid-http-client",
    "vlm-auto-engine",
    "hybrid-auto-engine",
}
SUPPORTED_METHODS = {"auto", "txt", "ocr"}
SUPPORTED_LANGUAGES = {
    "ch", "ch_server", "ch_lite", "en", "korean", "japan", "chinese_cht",
    "ta", "te", "ka", "th", "el", "latin", "arabic", "east_slavic",
    "cyrillic", "devanagari",
}


def parse_mineru(
    request: MinerURequest,
    backend: MinerUBackend | None = None,
) -> MinerUResult:
    """Execute or reuse one MinerU parse through the shared protocol."""

    normalized = _normalize_request(request)
    ensure_dir(normalized.output_dir)
    existing = find_mineru_outputs(
        normalized.output_dir,
        normalized.pdf_path.stem,
        normalized.method,
    )
    if existing is not None and normalized.reuse_existing and not normalized.overwrite:
        log.info("mineru reuse hit | %s | %s", normalized.pdf_path.stem, existing.content_list_json)
        return replace(existing, status="reused", source="existing_output")
    if not normalized.execute:
        raise RuntimeError(
            "MinerU request is reuse-only, but no matching "
            f"{normalized.pdf_path.stem}_content_list.json exists under "
            f"{normalized.output_dir}"
        )

    if backend is None:
        from .engine import MinerUCliBackend

        backend = MinerUCliBackend()
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{normalized.pdf_path.stem}-mineru-",
            dir=str(normalized.output_dir),
        )
    ).resolve()
    # The default backend never reads overwrite/reuse_existing/execute (both are
    # resolved above, before staging); these are pinned only to give a custom
    # backend unambiguous "fresh parse into staging" semantics.
    staged_request = replace(
        normalized,
        output_dir=staging,
        overwrite=False,
        reuse_existing=False,
    )
    log.debug("mineru fresh parse -> staging %s", staging)
    started = time.monotonic()
    try:
        result = _validate_result(backend.parse(staged_request), staged_request)
        promoted = _promote_staged_result(result, staging, normalized.output_dir)
        if promoted.duration_s <= 0:
            promoted.duration_s = round(time.monotonic() - started, 3)
        return promoted
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _is_hidden(path: Path, base: Path) -> bool:
    """True if any path segment *below* ``base`` starts with a dot.

    Fresh parses stage into ``.{stem}-mineru-XXXX/`` under ``output_dir``; if that
    staging leaks (process hard-killed), its half-written ``_content_list.json``
    must not be mistaken for a reusable product. Judged relative to ``base`` only,
    since ``output_dir`` itself may legitimately sit under a dot directory.
    """

    try:
        rel = path.relative_to(base)
    except ValueError:
        return False
    return any(part.startswith(".") for part in rel.parts)


def find_mineru_outputs(
    output_dir: str | Path,
    pdf_stem: Optional[str] = None,
    method: Optional[str] = None,
) -> Optional[MinerUResult]:
    output_dir = Path(output_dir).resolve()
    if not output_dir.exists():
        return None

    content_lists = sorted(
        path
        for path in output_dir.rglob("*_content_list.json")
        if not path.name.endswith("_content_list_v2.json")
        and not _is_hidden(path, output_dir)
    )
    if pdf_stem:
        content_lists = [
            path for path in content_lists
            if path.name == f"{pdf_stem}_content_list.json"
        ]
    if method:
        content_lists = [path for path in content_lists if path.parent.name == method]
    if not content_lists:
        return None

    content_list = content_lists[0]
    parse_dir = content_list.parent
    markdowns = sorted(parse_dir.glob("*.md"))
    if pdf_stem:
        exact_markdown = [path for path in markdowns if path.stem == pdf_stem]
        markdowns = exact_markdown or markdowns
    return MinerUResult(
        output_dir=parse_dir,
        content_list_json=content_list,
        markdown_file=markdowns[0] if markdowns else None,
        raw_files=sorted(parse_dir.iterdir()),
    )


def _normalize_request(request: MinerURequest) -> MinerURequest:
    if not isinstance(request, MinerURequest):
        raise TypeError("MinerU backend request must be MinerURequest")
    if request.schema_version != MINERU_REQUEST_SCHEMA:
        raise ValueError(
            f"MinerU request schema must be {MINERU_REQUEST_SCHEMA!r}; "
            f"observed {request.schema_version!r}"
        )
    pdf_path = Path(request.pdf_path).expanduser().resolve()
    output_dir = runtime_output_path(request.output_dir)
    source_root = Path(request.source_root).expanduser().resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"MinerU request input must be a PDF: {pdf_path}")
    if request.backend not in SUPPORTED_BACKENDS:
        raise ValueError(f"unsupported MinerU backend: {request.backend!r}")
    if request.method not in SUPPORTED_METHODS:
        raise ValueError(f"unsupported MinerU method: {request.method!r}")
    if request.lang not in SUPPORTED_LANGUAGES:
        raise ValueError(f"unsupported MinerU language: {request.lang!r}")
    if request.timeout_s is not None and request.timeout_s <= 0:
        raise ValueError("MinerU timeout_s must be greater than zero")
    return replace(request, pdf_path=pdf_path, output_dir=output_dir, source_root=source_root)


def _validate_result(result: MinerUResult, request: MinerURequest) -> MinerUResult:
    if not isinstance(result, MinerUResult):
        raise TypeError("MinerU backend must return MinerUResult")
    if result.schema_version != MINERU_RESULT_SCHEMA:
        raise ValueError(
            f"MinerU result schema must be {MINERU_RESULT_SCHEMA!r}; "
            f"observed {result.schema_version!r}"
        )
    output_dir = Path(result.output_dir).resolve()
    content_list = Path(result.content_list_json).resolve()
    try:
        output_dir.relative_to(request.output_dir)
        content_list.relative_to(output_dir)
    except ValueError as exc:
        raise ValueError("MinerU backend returned assets outside its output directory") from exc
    if not output_dir.is_dir():
        raise FileNotFoundError(output_dir)
    if not content_list.is_file():
        raise FileNotFoundError(content_list)
    expected_name = f"{request.pdf_path.stem}_content_list.json"
    if content_list.name != expected_name:
        raise ValueError(
            f"MinerU backend returned {content_list.name!r}; expected {expected_name!r}"
        )
    markdown = Path(result.markdown_file).resolve() if result.markdown_file else None
    if markdown is not None and not markdown.is_file():
        raise FileNotFoundError(markdown)
    raw_files = [Path(path).resolve() for path in result.raw_files]
    for path in ([markdown] if markdown else []) + raw_files:
        try:
            path.relative_to(output_dir)
        except ValueError as exc:
            raise ValueError(
                "MinerU backend returned assets outside its parse directory"
            ) from exc
    return replace(
        result,
        output_dir=output_dir,
        content_list_json=content_list,
        markdown_file=markdown,
        raw_files=raw_files,
    )


def _promote_staged_result(
    result: MinerUResult,
    staging: Path,
    destination: Path,
) -> MinerUResult:
    output_relative = result.output_dir.relative_to(staging)
    content_relative = result.content_list_json.relative_to(staging)
    markdown_relative = (
        result.markdown_file.relative_to(staging) if result.markdown_file else None
    )
    raw_relative = [path.relative_to(staging) for path in result.raw_files]

    promoted_output = destination / output_relative
    if result.output_dir == staging:
        # The default CLI backend always nests its dump under
        # staging/{stem}/{method}/, so output_dir never equals staging here; this
        # branch fires only if a custom backend writes straight into the staging
        # root, promoting each child individually rather than the parse subtree.
        replacements = [
            (child, destination / child.name) for child in list(staging.iterdir())
        ]
    else:
        replacements = [(result.output_dir, promoted_output)]
    _replace_paths_with_rollback(replacements)

    promoted_content = destination / content_relative
    promoted_markdown = destination / markdown_relative if markdown_relative else None
    promoted_raw = [destination / path for path in raw_relative]
    if not promoted_content.is_file():
        raise RuntimeError(f"promoted MinerU content list is missing: {promoted_content}")
    return replace(
        result,
        output_dir=promoted_output,
        content_list_json=promoted_content,
        markdown_file=promoted_markdown,
        raw_files=promoted_raw,
        status="completed",
    )


def _remove_path(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _replace_paths_with_rollback(replacements: list[tuple[Path, Path]]) -> None:
    """Promote staged paths and restore every old target if promotion fails."""

    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for source, target in replacements:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                backup = target.with_name(f".{target.name}.mineru-backup-{uuid4().hex}")
                target.rename(backup)
                backups.append((target, backup))
            shutil.move(str(source), str(target))
            installed.append(target)
    except Exception:
        for target in reversed(installed):
            _remove_path(target)
        for target, backup in reversed(backups):
            if backup.exists() or backup.is_symlink():
                backup.rename(target)
        raise
    for _target, backup in backups:
        _remove_path(backup)

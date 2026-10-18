"""Prune MinerU's debug dumps and write a self-describing manifest beside the assets.

``parse`` hands one directory to the downstream Markdown formatter, so we name the
four assets it needs -- origin PDF, Markdown, content list, images -- in a
``manifest.json`` beside them instead of making the consumer reverse-engineer our
filenames or wade through the debug dumps. Nothing is copied: the dump stays the
single source of truth, so writing the manifest is deterministic and idempotent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from datasheet_automation.parsing.images import referenced_images
from datasheet_automation.parsing.protocol import MinerUResult


MANIFEST_SCHEMA = "datasheet-automation.manifest.v1"
MANIFEST_NAME = "manifest.json"
_CONTENT_LIST_SUFFIX = "_content_list.json"

# MinerU debug/intermediate dumps the downstream formatter never consumes and the
# platform's own prepare/reuse paths never read either. Dropped before handoff so
# the product directory carries only the four named assets.
_INTERMEDIATE_SUFFIXES = (
    "_middle.json",
    "_model.json",
    "_content_list_v2.json",
    "_layout.pdf",
    "_span.pdf",
)


def prune_intermediate(result: MinerUResult) -> list[str]:
    """Drop MinerU's regenerable debug dumps from the parse directory.

    Removes only the ``<stem>_{middle,model,content_list_v2}.json`` and
    ``<stem>_{layout,span}.pdf`` files. The four named assets and ``images/`` are
    untouched. Returns the names removed (for the trace).
    """

    parse_dir = result.output_dir
    stem = result.content_list_json.name[: -len(_CONTENT_LIST_SUFFIX)]
    removed: list[str] = []
    for suffix in _INTERMEDIATE_SUFFIXES:
        target = parse_dir / f"{stem}{suffix}"
        if target.is_file():
            target.unlink()
            removed.append(target.name)
    return sorted(removed)


def write_manifest(
    result: MinerUResult,
    *,
    pdf_name: str | None = None,
    method: str | None = None,
) -> dict[str, str]:
    """Validate the parse dump in place and drop a self-describing manifest.

    ``prepare`` flattens its parse into the delivery dir and ships the official
    download instead of MinerU's ``_origin.pdf``, so it overrides ``pdf`` (the
    download filename) and ``method`` (captured before the nested parse dir is
    removed). Defaults reproduce parse's original manifest byte-for-byte.
    """

    parse_dir = result.output_dir
    stem = result.content_list_json.name[: -len(_CONTENT_LIST_SUFFIX)]
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "stem": stem,
        "method": method if method is not None else parse_dir.name,
        "pdf": pdf_name if pdf_name is not None else f"{stem}_origin.pdf",
        "markdown": f"{stem}.md",
        "content_list": result.content_list_json.name,
        "images_dir": "images",
    }
    missing = [manifest[key] for key in ("pdf", "markdown", "content_list")
               if not (parse_dir / manifest[key]).is_file()]
    if missing:
        raise FileNotFoundError(f"parse dump missing manifest assets: {', '.join(missing)}")
    # MinerU omits images/ for a figureless PDF; guarantee the directory exists so
    # the manifest's images_dir always resolves to a real (possibly empty) path.
    images_dir = parse_dir / manifest["images_dir"]
    images_dir.mkdir(exist_ok=True)
    _validate_manifest_assets(parse_dir, manifest)

    # Surface images present on disk but cited by neither the content list nor the
    # Markdown. The known-benign case is a cross-page table merge: the merged-away
    # page's screenshot is redundant (its rows live in the owner table's HTML). We
    # keep the file -- deleting could mask a genuine MinerU drop -- and record it.
    referenced = {ref.split("/", 1)[-1] for ref in _referenced(parse_dir, manifest)}
    unreferenced = sorted(
        p.name for p in images_dir.iterdir() if p.is_file() and p.name not in referenced
    )
    if unreferenced:
        manifest["unreferenced_images"] = unreferenced

    manifest_path = parse_dir / MANIFEST_NAME
    tmp = manifest_path.with_name(f".{MANIFEST_NAME}.{uuid4().hex}")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, manifest_path)
    result_map = {"manifest": str(manifest_path), **{
        key: str(parse_dir / manifest[key])
        for key in ("pdf", "markdown", "content_list", "images_dir")
    }}
    if unreferenced:
        result_map["unreferenced_images"] = unreferenced
    return result_map


def _referenced(parse_dir: Path, manifest: dict) -> set[str]:
    return referenced_images(parse_dir, manifest["content_list"], manifest["markdown"])


def _validate_manifest_assets(parse_dir: Path, manifest: dict) -> None:
    dangling = sorted(
        ref for ref in _referenced(parse_dir, manifest)
        if not (parse_dir / ref).is_file()
    )
    if dangling:
        raise ValueError(f"manifest image references dangle: {', '.join(dangling)}")

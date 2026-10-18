"""Flatten one nested MinerU parse into the flat delivery dir with stable names.

``prepare`` ships ``run_dir`` (output_root/<code>/) to the downstream agent, but
MinerU nests its dump under ``<mineru_root>/<stem>/<method>/`` and names images by
sha256. We prune the debug dumps, relabel images as reading-order
``{img,table}-p<page>-<seq>`` names via the ImageRefs model, hoist Markdown /
content list / images up to ``run_dir``, and drop the temporary parse root -- the
official download already in ``run_dir`` replaces MinerU's ``_origin.pdf``.
"""

from __future__ import annotations

from dataclasses import replace
import json
import shutil
from pathlib import Path

from datasheet_automation.parsing import images as imagerefs
from datasheet_automation.parsing.protocol import MinerUResult
from .manifest import prune_intermediate, write_manifest


_CONTENT_LIST_SUFFIX = "_content_list.json"


def finalize_prepare_dir(
    result: MinerUResult,
    run_dir: Path,
    mineru_root: Path,
    pdf_name: str,
) -> tuple[MinerUResult, list[str], dict[str, str]]:
    """Flatten one nested parse into ``run_dir``. Returns (flat result, pruned, manifest)."""

    stem = result.content_list_json.name[: -len(_CONTENT_LIST_SUFFIX)]
    method = result.output_dir.name  # capture before the parse root is removed
    # Read middle.json before prune deletes it -- its pre-merge preproc_blocks are
    # the authoritative page+kind for every cropped image, orphans included.
    middle_images = imagerefs.images_from_middle(result.output_dir / f"{stem}_middle.json")
    removed = prune_intermediate(result)

    # Build old->new canonical image map (covers orphans via middle.json), then
    # rename on disk in two collision-safe passes, then rewrite the references.
    entries = json.loads(result.content_list_json.read_text(encoding="utf-8"))
    rename = imagerefs.build_rename_map(middle_images, entries)
    imagerefs.apply_disk_rename(result.output_dir, rename)

    imagerefs.rewrite_entries(entries, rename)
    result.content_list_json.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if result.markdown_file and result.markdown_file.is_file():
        text = result.markdown_file.read_text(encoding="utf-8")
        result.markdown_file.write_text(imagerefs.relabel(text, rename), encoding="utf-8")

    # Hoist the three delivered assets up to run_dir (overwrite stale copies), then
    # drop the whole parse root -- _origin.pdf goes with it.
    for name in (f"{stem}.md", result.content_list_json.name, "images"):
        src = result.output_dir / name
        if not src.exists():
            continue
        dest = run_dir / name
        if dest.exists():
            _remove_path(dest)
        shutil.move(str(src), str(dest))
    shutil.rmtree(mineru_root)

    flat = replace(
        result,
        output_dir=run_dir,
        content_list_json=run_dir / result.content_list_json.name,
        markdown_file=(run_dir / f"{stem}.md") if result.markdown_file else None,
        raw_files=sorted(run_dir.iterdir()),
    )
    manifest = write_manifest(flat, pdf_name=pdf_name, method=method)
    return flat, removed, manifest


def _remove_path(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)

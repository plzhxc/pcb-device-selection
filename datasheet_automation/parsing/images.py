"""ImageRefs: the one place that knows how MinerU images are cited and renamed.

MinerU cites images two ways that never fully overlap: the content list's
``img_path`` field and inline ``<img src>`` in string fields (e.g. table_body),
versus Markdown ``![]()`` / ``<img>``. Table screenshots appear only in the
content list; table HTML only inline in Markdown. So the reference universe is
always their *union* across both syntaxes.

This module owns: the two capture regexes, the md/html union, the pre-merge
``middle.json`` page+kind ordering (the authoritative source that still lists
images orphaned by cross-page table merge), the canonical rename map, the
two-pass on-disk rename, and reference rewriting. Both the parsing transaction
and the delivery flatten cross this interface instead of re-deriving it.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from uuid import uuid4


# MinerU embeds figures both as Markdown ``![](images/..)`` and as HTML
# ``<img src="images/..">`` inside table_body cells; capture both.
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*(images/[^)\s]+)")
_HTML_IMAGE_RE = re.compile(r"""<img[^>]+src=["'](images/[^"']+)["']""")


def refs_in_text(text: str) -> set[str]:
    """All ``images/`` references in ``text`` across Markdown and HTML syntax."""

    return set(_MD_IMAGE_RE.findall(text)) | set(_HTML_IMAGE_RE.findall(text))


def relabel(text: str, rename: dict[str, str]) -> str:
    """Rewrite every ``images/`` reference in ``text`` via ``rename`` (md + html)."""

    for pattern in (_MD_IMAGE_RE, _HTML_IMAGE_RE):
        text = pattern.sub(
            lambda m: m.group(0).replace(m.group(1), rename.get(m.group(1), m.group(1))),
            text,
        )
    return text


def images_from_middle(middle_path: Path) -> list[tuple[str, int, str]]:
    """Ordered ``(images/<name>, page_idx, prefix)`` for every cropped image span.

    Read from MinerU's *pre-merge* ``_middle.json`` (``pdf_info[*].preproc_blocks``),
    the authoritative source of each image's page and kind (``table`` screenshot
    vs ``img`` figure/chart/seal/equation). Crucially it still lists images that
    become orphans after cross-page table merge, so those get a canonical name
    too. Returns ``[]`` if middle.json is absent, so callers can fall back.
    """

    try:
        mid = json.loads(Path(middle_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    order: list[tuple[str, int, str]] = []
    seen: set[str] = set()

    def walk(node: object, page: int) -> None:
        if isinstance(node, dict):
            ip = node.get("image_path")
            if isinstance(ip, str) and ip and ip not in seen:
                seen.add(ip)
                prefix = "table" if node.get("type") == "table" else "img"
                order.append((f"images/{ip}", page, prefix))
            for value in node.values():
                walk(value, page)
        elif isinstance(node, list):
            for value in node:
                walk(value, page)

    pdf_info = mid.get("pdf_info") if isinstance(mid, dict) else None
    if isinstance(pdf_info, list):
        for page_idx, page in enumerate(pdf_info):
            if isinstance(page, dict):
                walk(page.get("preproc_blocks", []), page_idx)
    return order


def referenced_images(parse_dir: Path, content_list_name: str, markdown_name: str | None) -> set[str]:
    """Every ``images/...`` path cited by the content list or the Markdown.

    Union of: content list ``img_path`` fields + any ``<img src>`` embedded in
    content list string values (e.g. ``table_body``) + Markdown ``![]()`` and
    ``<img>`` references. The two documents don't cite the same set, so the
    reference universe must be their union.
    """

    entries = json.loads((parse_dir / content_list_name).read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError(f"content list must be a JSON array: {content_list_name}")
    referenced: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        img_path = str(entry.get("img_path") or "").strip()
        if img_path:
            referenced.add(img_path)
        for value in entry.values():
            if isinstance(value, str) and "images/" in value:
                referenced.update(_HTML_IMAGE_RE.findall(value))
    if markdown_name:
        md = parse_dir / markdown_name
        if md.is_file():
            referenced.update(refs_in_text(md.read_text(encoding="utf-8")))
    return referenced


def build_rename_map(
    middle_images: list[tuple[str, int, str]],
    entries: list[dict],
) -> dict[str, str]:
    """Map each old ``images/<sha>`` to a canonical ``images/{img,table}-p<page>-<seq>``.

    ``seq`` restarts per ``(prefix, page)`` so names read img-p1-01, img-p1-02,
    ... Prefer middle.json (covers every image, orphans included, in page reading
    order); fall back to the content list when middle.json is absent (then only
    referenced images are renamed, orphans keep sha256 names). Base64 images
    embedded in table HTML aren't separate middle spans, so pick those up from
    the content list too.
    """

    rename: dict[str, str] = {}
    seq: dict[tuple[str, int], int] = {}

    def assign(old: str, prefix: str, page: int) -> None:
        old = old.strip()
        if not old or old in rename:
            return
        seq[(prefix, page)] = seq.get((prefix, page), 0) + 1
        ext = Path(old).suffix or ".jpg"
        rename[old] = f"images/{prefix}-p{page + 1}-{seq[(prefix, page)]:02d}{ext}"

    for old, page, prefix in middle_images:
        assign(old, prefix, page)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        page = int(entry.get("page_idx") or 0)
        if not middle_images:
            assign(
                str(entry.get("img_path") or ""),
                "table" if entry.get("type") == "table" else "img",
                page,
            )
        for value in entry.values():
            if isinstance(value, str) and "images/" in value:
                for ref in _HTML_IMAGE_RE.findall(value):
                    assign(ref, "img", page)
    return rename


def apply_disk_rename(output_dir: Path, rename: dict[str, str]) -> None:
    """Rename image files on disk in two passes (all -> temp, temp -> final).

    Two passes so a new name colliding with a not-yet-renamed old name cannot
    clobber it. Any file outside the map (no middle.json) keeps its sha256 name.
    """

    images_dir = output_dir / "images"
    staged: list[tuple[Path, str]] = []
    for old, new in rename.items():
        src = output_dir / old
        if not src.is_file():
            continue
        tmp = images_dir / f".finalize-{uuid4().hex}"
        src.rename(tmp)
        staged.append((tmp, Path(new).name))
    for tmp, new_name in staged:
        tmp.rename(images_dir / new_name)


def rewrite_entries(entries: list[dict], rename: dict[str, str]) -> None:
    """Rewrite content-list references to canonical names in place.

    Both the bare ``img_path`` field and any ``images/`` paths embedded in HTML
    string fields (e.g. an ``<img>`` inside table_body).
    """

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        old = str(entry.get("img_path") or "").strip()
        if old in rename:
            entry["img_path"] = rename[old]
        for key, value in entry.items():
            if key != "img_path" and isinstance(value, str) and "images/" in value:
                entry[key] = relabel(value, rename)

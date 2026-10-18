"""The single input-to-output datasheet optimization pipeline.

Flow:
MinerU content list -> source Markdown -> text agents -> image materialization
-> visual agents -> image-comment enrichment -> optimized Markdown and JSON.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import partial

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .parsing import guess_title
from .protocols import OptimizationAgentFactory


VISUAL_TYPES = {"image", "chart", "table"}
SKIP_TYPES = {"header", "footer", "page_number", "aside_text"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)]+?)\s*\)")
HEADING_RE = re.compile(r"(?m)(?=^#{1,6}\s+\S)")
FORBIDDEN_INFERENCE_RE = re.compile(
    r"(?:根据(?:常见|标准|经验)|推断|推测|猜测|假设|"
    r"based on (?:common|standard|typical|context)|infer(?:red|ence)?|assum(?:e|ed|ption))",
    re.IGNORECASE,
)


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        parts = [_flatten_text(item) for item in value.values()]
        return " ".join(part for part in parts if part).strip()
    if isinstance(value, (list, tuple)):
        parts = [_flatten_text(item) for item in value]
        return " ".join(part for part in parts if part).strip()
    return str(value).strip()


def _caption(entry: dict[str, Any]) -> str:
    for key in ("caption", "image_caption", "table_caption", "title"):
        text = _flatten_text(entry.get(key))
        if text:
            return text
    return ""


def _footnote(entry: dict[str, Any]) -> str:
    return _flatten_text(entry.get("footnote") or entry.get("table_footnote"))


def _table_body(entry: dict[str, Any]) -> str:
    for key in ("table_body", "table", "html", "text", "content"):
        text = _flatten_text(entry.get(key))
        if text:
            return text
    return ""


def _clean_ref(raw: str) -> str:
    ref = str(raw or "").strip()
    if ref.startswith("<") and ref.endswith(">"):
        ref = ref[1:-1]
    if " \"" in ref or " '" in ref:
        ref = ref.split(maxsplit=1)[0]
    return ref.strip("<>").replace("\\", "/")


def _ref_key(ref: str) -> str:
    return Path(_clean_ref(ref)).name.lower()


def _markdown_refs(markdown: str) -> list[str]:
    refs: list[str] = []
    for raw in IMAGE_REF_RE.findall(str(markdown or "")):
        ref = _clean_ref(raw)
        if ref:
            refs.append(ref)
    return refs


def _repair_soft_breaks(markdown: str) -> str:
    text = str(markdown or "")
    text = re.sub(
        r"(?P<left>[<>=+*/(])\s*\n\s*\n\s*(?!#{1,6}\s|!\[|\|)(?P<right>\S)",
        r"\g<left>\g<right>",
        text,
    )
    text = re.sub(
        r"(?P<left>[A-Za-z])-\s*\n\s*\n\s*(?P<right>[a-z])",
        r"\g<left>-\g<right>",
        text,
    )
    return text.strip()


def _entry_markdown(entry: dict[str, Any]) -> str:
    entry_type = str(entry.get("type") or "")
    if entry_type in SKIP_TYPES:
        return ""
    if entry_type == "text":
        content = str(entry.get("content") or "").strip()
        title = str(entry.get("title") or "").strip()
        if content:
            return f"## {title}\n\n{content}".strip() if title else content
        text = str(entry.get("text") or "").strip()
        try:
            level = int(entry.get("text_level") or 0)
        except (TypeError, ValueError):
            level = 0
        return f"{'#' * min(max(level, 1), 6)} {text}" if text and level > 0 else text
    if entry_type == "equation":
        return str(entry.get("text") or entry.get("content") or "").strip()
    if entry_type == "code":
        caption = _flatten_text(entry.get("code_caption"))
        body = str(entry.get("code_body") or entry.get("content") or "").strip()
        return "\n\n".join(part for part in (caption, body) if part)
    if entry_type == "list":
        items = entry.get("list_items") or entry.get("text") or []
        if isinstance(items, list):
            return "\n".join(f"- {_flatten_text(item)}" for item in items if _flatten_text(item))
        return str(items).strip()
    if entry_type in VISUAL_TYPES:
        parts = [_caption(entry)]
        img_path = str(entry.get("img_path") or "").strip()
        if img_path:
            parts.append(f"![]({img_path})")
        if entry_type == "table":
            parts.append(_table_body(entry))
            parts.append(_footnote(entry))
        return "\n\n".join(part for part in parts if part).strip()
    return str(entry.get("text") or entry.get("content") or "").strip()


def _content_list_markdown(entries: list[dict[str, Any]]) -> str:
    return _repair_soft_breaks(
        "\n\n".join(
            text for entry in entries if isinstance(entry, dict) and (text := _entry_markdown(entry))
        )
    )


def _source_markdown(content_list: Path, mineru_dir: Path, entries: list[dict[str, Any]]) -> str:
    stem = re.sub(r"_content_list(?:_v2)?\.json$", "", content_list.name)
    candidates = [content_list.with_name(f"{stem}.md"), mineru_dir / f"{stem}.md"]
    candidates.extend(path for path in sorted(mineru_dir.glob("*.md")) if not path.name.startswith("opti_"))
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.is_file():
            return _repair_soft_breaks(candidate.read_text(encoding="utf-8", errors="ignore"))
    return _content_list_markdown(entries)


def _split_long_unit(unit: str, limit: int) -> list[str]:
    remaining = unit.strip()
    parts: list[str] = []
    while len(remaining) > limit:
        window = remaining[: limit + 1]
        cuts = [window.rfind(token) for token in ("\n\n", "\n", "。", ". ", "; ", " ")]
        cut = max(cuts)
        if cut < max(64, limit // 3):
            cut = limit
        elif window[cut : cut + 2] in {". ", "; ", "\n\n"}:
            cut += 1
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def _split_markdown(markdown: str, limit: int) -> list[str]:
    text = str(markdown or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    sections = [section.strip() for section in HEADING_RE.split(text) if section.strip()]
    units: list[str] = []
    for section in sections or [text]:
        if len(section) <= limit:
            units.append(section)
            continue
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", section) if part.strip()]
        for paragraph in paragraphs:
            units.extend(_split_long_unit(paragraph, limit))

    blocks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}\n\n{unit}".strip() if current else unit
        if current and len(candidate) > limit:
            blocks.append(current)
            current = unit
        else:
            current = candidate
    if current:
        blocks.append(current)
    return blocks


def _introduced_inference(source: str, optimized: str) -> bool:
    source_markers = {match.group(0).lower() for match in FORBIDDEN_INFERENCE_RE.finditer(source)}
    output_markers = {match.group(0).lower() for match in FORBIDDEN_INFERENCE_RE.finditer(optimized)}
    return bool(output_markers - source_markers)


def _safe_agent_output(source: str, output: str, warnings: list[str], stage: str) -> str:
    if _introduced_inference(source, output):
        warnings.append(f"{stage}_rejected:introduced_inference")
        return source
    return output.strip() or source


def _restore_and_filter_refs(
    source_markdown: str,
    blocks: list[dict[str, str]],
    warnings: list[str],
) -> list[dict[str, str]]:
    known_refs = _markdown_refs(source_markdown)
    known_keys = {_ref_key(ref) for ref in known_refs}

    def remove_unknown(match: re.Match[str]) -> str:
        ref = _clean_ref(match.group(1))
        if _ref_key(ref) in known_keys:
            return match.group(0)
        warnings.append(f"unknown_image_ref_removed:{ref}")
        return ""

    for block in blocks:
        block["content"] = IMAGE_REF_RE.sub(remove_unknown, str(block.get("content") or "")).strip()

    present_keys = {
        _ref_key(ref)
        for block in blocks
        for ref in _markdown_refs(str(block.get("content") or ""))
    }
    missing = [ref for ref in known_refs if _ref_key(ref) not in present_keys]
    if missing:
        if not blocks:
            blocks.append({"type": "text", "title": "datasheet_visuals", "content": ""})
        refs = "\n\n".join(f"![]({ref})" for ref in missing)
        blocks[-1]["content"] = f"{blocks[-1].get('content', '')}\n\n{refs}".strip()
        warnings.extend(f"image_ref_restored:{ref}" for ref in missing)
    return blocks


def _resolve_content_list(mineru: str | Path, lichuang_id: str | None) -> tuple[Path, Path, str]:
    source = Path(mineru).expanduser().resolve()
    if source.is_file():
        content_list = source
        mineru_dir = source.parent
    elif source.is_dir():
        candidates = sorted(
            path
            for path in source.rglob("*_content_list.json")
            if not path.name.startswith("opti_")
        )
        if lichuang_id:
            matches = [path for path in candidates if path.name.startswith(f"{lichuang_id}_")]
            if matches:
                candidates = matches
        if not candidates:
            raise FileNotFoundError(f"no *_content_list.json found under {source}")
        if len(candidates) > 1:
            joined = ", ".join(str(path) for path in candidates[:5])
            raise ValueError(f"multiple MinerU content lists found; specify a file or --lichuang-id: {joined}")
        content_list = candidates[0]
        mineru_dir = content_list.parent
    else:
        raise FileNotFoundError(f"MinerU input does not exist: {source}")

    inferred = re.sub(r"_content_list(?:_v2)?\.json$", "", content_list.name)
    device_id = str(lichuang_id or inferred).strip()
    if not device_id or device_id in {".", ".."} or any(char in device_id for char in "/\\"):
        raise ValueError(f"invalid lichuang_id: {device_id!r}")
    return content_list, mineru_dir, device_id


def _page_index(entry: dict[str, Any]) -> int | None:
    value = entry.get("page_idx")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _resolve_source_image(
    ref: str,
    entry: dict[str, Any],
    content_list: Path,
    mineru_dir: Path,
) -> Path | None:
    for key in ("absolute_img_path", "image_path", "path"):
        raw = entry.get(key)
        if raw and Path(str(raw)).expanduser().is_file():
            return Path(str(raw)).expanduser().resolve()

    ref_path = Path(_clean_ref(ref))
    candidates = [ref_path] if ref_path.is_absolute() else [
        content_list.parent / ref_path,
        mineru_dir / ref_path,
        mineru_dir / "images" / ref_path.name,
        mineru_dir.parent / ref_path,
        mineru_dir.parent / "images" / ref_path.name,
        mineru_dir.parent.parent / "images" / ref_path.name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    if ref_path.name and mineru_dir.is_dir():
        match = next((path for path in mineru_dir.rglob(ref_path.name) if path.is_file()), None)
        if match:
            return match.resolve()
    return None


@dataclass
class _ImagePlan:
    ordered_refs: list[str]
    renamed_by_key: dict[str, str]
    source_by_key: dict[str, Path]
    report: dict[str, Any]

    def renamed(self, ref: str) -> str:
        return self.renamed_by_key.get(_ref_key(ref), _clean_ref(ref))

    def source(self, ref: str) -> Path | None:
        return self.source_by_key.get(_ref_key(ref))


def _materialize_images(
    *,
    markdown: str,
    entries: list[dict[str, Any]],
    content_list: Path,
    mineru_dir: Path,
    output_dir: Path,
    warnings: list[str],
) -> _ImagePlan:
    entry_by_key: dict[str, dict[str, Any]] = {}
    page_by_key: dict[str, int] = {}
    ordered_refs: list[str] = []
    seen: set[str] = set()

    def add(ref: str, entry: dict[str, Any] | None = None) -> None:
        clean = _clean_ref(ref)
        key = _ref_key(clean)
        if not key or Path(key).suffix.lower() not in IMAGE_SUFFIXES:
            return
        if entry is not None:
            entry_by_key[key] = entry
            page = _page_index(entry)
            if page is not None:
                page_by_key[key] = page
        if key not in seen:
            seen.add(key)
            ordered_refs.append(clean)

    for entry in entries:
        if isinstance(entry, dict) and entry.get("type") in VISUAL_TYPES and entry.get("img_path"):
            add(str(entry["img_path"]), entry)
    for ref in _markdown_refs(markdown):
        add(ref)

    renamed_by_key: dict[str, str] = {}
    per_page: dict[int, int] = {}
    for ref in ordered_refs:
        key = _ref_key(ref)
        page_no = page_by_key[key] + 1 if key in page_by_key else 0
        per_page[page_no] = per_page.get(page_no, 0) + 1
        suffix = Path(key).suffix.lower() or ".jpg"
        renamed_by_key[key] = f"images/{page_no}_{per_page[page_no]}{suffix}"

    source_by_key: dict[str, Path] = {}
    copied = existing = missing = 0
    for ref in ordered_refs:
        key = _ref_key(ref)
        source = _resolve_source_image(ref, entry_by_key.get(key, {}), content_list, mineru_dir)
        destination = output_dir / renamed_by_key[key]
        if source:
            source_by_key[key] = source
        if destination.is_file() and source is None:
            existing += 1
            source_by_key[key] = destination.resolve()
            continue
        if source is None:
            missing += 1
            warnings.append(f"image_unresolved:{ref}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and source == destination.resolve():
            existing += 1
        else:
            shutil.copy2(source, destination)
            copied += 1

    return _ImagePlan(
        ordered_refs=ordered_refs,
        renamed_by_key=renamed_by_key,
        source_by_key=source_by_key,
        report={
            "refs": len(ordered_refs),
            "copied": copied,
            "already_present": existing,
            "unresolved": missing,
            "rename_map": {
                ref: renamed_by_key[_ref_key(ref)] for ref in ordered_refs
            },
        },
    )


def _rewrite_refs(markdown: str, plan: _ImagePlan) -> str:
    def replace(match: re.Match[str]) -> str:
        ref = _clean_ref(match.group(1))
        renamed = plan.renamed(ref)
        return match.group(0) if renamed == ref else match.group(0).replace(match.group(1), renamed)

    return IMAGE_REF_RE.sub(replace, str(markdown or ""))


def _nearby_text(entries: list[dict[str, Any]], index: int, direction: int) -> str:
    cursor = index + direction
    while 0 <= cursor < len(entries):
        entry = entries[cursor]
        if isinstance(entry, dict) and entry.get("type") == "text":
            text = str(entry.get("content") or entry.get("text") or "").strip()
            if text:
                return text[-1200:] if direction < 0 else text[:1200]
        cursor += direction
    return ""


def _formatted_visual_content(title: str, category: str, description: str) -> str:
    return (
        f"title:\n{title}\n\ncategory:\n{category}\n\n"
        f"description:\n{description}"
    ).strip()


def _build_visual_entries(
    *,
    raw_entries: list[dict[str, Any]],
    plan: _ImagePlan,
    agent_mode: str,
    use_vlm: bool,
    llm_model: str | None,
    vlm_model: str | None,
    reasoning: bool | None,
    agent_factory: OptimizationAgentFactory | None,
    warnings: list[str],
    concurrency: int = 1,
) -> tuple[list[dict[str, Any]], int]:
    """Describe every figure / table.

    Agent calls are independent per image, so they run on a thread pool of
    ``concurrency`` workers (each call builds its own Agent instance; nothing is
    shared).  Results are applied in the original entry order on the calling
    thread so ``warnings`` and the ``required`` failure policy behave exactly as
    in the sequential version.
    """

    handled_keys: set[str] = set()
    calls = 0

    def handle_failure(stage: str, ref: str, exc: Exception) -> None:
        if agent_mode == "required":
            raise exc
        warnings.append(f"{stage}_failed:{ref}:{type(exc).__name__}:{exc}")

    def _no_factory() -> dict[str, Any]:
        raise RuntimeError("optimization Agent factory is not configured")

    # job = (visual_entry, stage, ref_label, callable | None)
    jobs: list[tuple[dict[str, Any], str, str, Any]] = []

    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict) or raw.get("type") not in VISUAL_TYPES:
            continue
        source_type = str(raw.get("type") or "image")
        output_type = "table" if source_type == "table" else "image"
        old_ref = str(raw.get("img_path") or "").strip()
        if old_ref:
            handled_keys.add(_ref_key(old_ref))
        img_path = plan.renamed(old_ref) if old_ref else ""
        source = plan.source(old_ref) if old_ref else None
        caption = _caption(raw)
        table_body = _table_body(raw)
        footnote = _footnote(raw)
        before = _nearby_text(raw_entries, index, -1)
        after = _nearby_text(raw_entries, index, 1)

        title = caption or (Path(img_path).name if img_path else "datasheet_table")
        category = "表格" if output_type == "table" else "其他类别"
        description = "\n\n".join(part for part in (caption, table_body, footnote) if part)
        if not description:
            description = "保留原始图片；未执行视觉内容推断。"

        visual: dict[str, Any] = {"type": output_type, "title": title, "category": category, "_description": description}
        if img_path:
            visual["img_path"] = img_path
        if source:
            visual["absolute_img_path"] = str(source)
        page = _page_index(raw)
        if page is not None:
            visual["page_idx"] = page

        call = None
        stage = ""
        if agent_mode != "off" and output_type == "table":
            stage = "table_agent"
            call = _no_factory if agent_factory is None else partial(
                lambda **kw: agent_factory.table(model=llm_model, reasoning=reasoning).optimize(**kw),
                caption=caption, table_body=table_body, footnote=footnote, context_before=before, context_after=after,
            )
        elif agent_mode != "off" and use_vlm and output_type == "image":
            stage = "visual_agent"
            call = _no_factory if agent_factory is None else partial(
                lambda **kw: agent_factory.visual(model=vlm_model, reasoning=reasoning).analyze(**kw),
                image_path=str(source or ""), img_path=img_path or old_ref, caption=caption, context_before=before, context_after=after,
            )
        jobs.append((visual, stage, old_ref or f"{output_type}_{index}", call))

    for ref in plan.ordered_refs:
        key = _ref_key(ref)
        if key in handled_keys:
            continue
        img_path = plan.renamed(ref)
        source = plan.source(ref)
        visual = {
            "type": "image",
            "img_path": img_path,
            "title": Path(img_path).name,
            "category": "其他类别",
            "_description": "保留原始图片；MinerU content_list 未提供对应视觉条目。",
        }
        if source:
            visual["absolute_img_path"] = str(source)
        call = None
        if agent_mode != "off" and use_vlm:
            call = _no_factory if agent_factory is None else partial(
                lambda **kw: agent_factory.visual(model=vlm_model, reasoning=reasoning).analyze(**kw),
                image_path=str(source or ""), img_path=img_path,
            )
        jobs.append((visual, "visual_agent", ref, call))

    results: list[Any] = [None] * len(jobs)
    pending = [(i, job[3]) for i, job in enumerate(jobs) if job[3] is not None]
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
            futures = [(i, pool.submit(call)) for i, call in pending]
            for i, future in futures:
                try:
                    results[i] = future.result()
                except Exception as exc:  # applied below, in order
                    results[i] = exc

    visual_entries: list[dict[str, Any]] = []
    for (visual, stage, ref, call), result in zip(jobs, results):
        description = visual.pop("_description")
        if call is not None:
            if isinstance(result, Exception):
                handle_failure(stage, ref, result)
            elif isinstance(result, dict):
                calls += 1
                visual["title"] = result.get("title") or visual["title"]
                visual["category"] = result.get("category") or visual["category"]
                description = result.get("description") or description
        visual["content"] = _formatted_visual_content(visual["title"], visual["category"], description)
        visual_entries.append(visual)

    return visual_entries, calls


def _visual_index(visual_entries: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for visual in visual_entries:
        ref = str(visual.get("img_path") or "")
        if ref:
            index[_clean_ref(ref)] = visual
            index.setdefault(_ref_key(ref), visual)
    return index


def _attach_img_comments(
    text_entries: list[dict[str, Any]], visual_entries: list[dict[str, Any]]
) -> dict[str, Any]:
    index = _visual_index(visual_entries)
    used: set[str] = set()
    orphan_refs: list[str] = []
    enriched = refs_found = 0
    for entry in text_entries:
        comments: list[list[str]] = []
        seen: set[str] = set()
        for ref in _markdown_refs(str(entry.get("content") or "")):
            refs_found += 1
            visual = index.get(_clean_ref(ref)) or index.get(_ref_key(ref))
            if visual is None:
                orphan_refs.append(ref)
                continue
            visual_ref = str(visual.get("img_path") or ref)
            if visual_ref in seen:
                continue
            seen.add(visual_ref)
            used.add(_clean_ref(visual_ref))
            comments.append(
                [visual_ref, str(visual.get("title") or ""), str(visual.get("content") or "")]
            )
        if comments:
            entry["img_comment"] = comments
            enriched += 1

    unused = [
        str(visual.get("img_path"))
        for visual in visual_entries
        if visual.get("img_path") and _clean_ref(str(visual["img_path"])) not in used
    ]
    return {
        "text_entries": len(text_entries),
        "text_entries_enriched": enriched,
        "image_refs_found": refs_found,
        "orphan_refs": orphan_refs,
        "unused_visuals": unused,
    }


def _description_from_visual(visual: dict[str, Any]) -> str:
    content = str(visual.get("content") or "").strip()
    marker = "description:\n"
    return content.split(marker, 1)[1].strip() if marker in content else content


def _optimized_markdown(
    text_entries: list[dict[str, Any]], visual_entries: list[dict[str, Any]]
) -> str:
    index = _visual_index(visual_entries)
    used: set[str] = set()

    def annotate(match: re.Match[str]) -> str:
        ref = _clean_ref(match.group(1))
        visual = index.get(ref) or index.get(_ref_key(ref))
        if visual is None:
            return match.group(0)
        used.add(_clean_ref(str(visual.get("img_path") or ref)))
        title = str(visual.get("title") or Path(ref).name)
        category = str(visual.get("category") or "")
        description = _description_from_visual(visual)
        heading = f"> **图像说明：{title}**" + (f"（{category}）" if category else "")
        quoted = "\n".join(f"> {line}" if line else ">" for line in description.splitlines())
        return f"{match.group(0)}\n\n{heading}\n>\n{quoted}".strip()

    sections: list[str] = []
    for entry in text_entries:
        title = str(entry.get("title") or "").strip()
        content = IMAGE_REF_RE.sub(annotate, str(entry.get("content") or "").strip())
        if title and not re.match(r"^#{1,6}\s+", content):
            content = f"## {title}\n\n{content}".strip()
        if content:
            sections.append(content)

    extras: list[str] = []
    for visual in visual_entries:
        ref = str(visual.get("img_path") or "")
        if ref and _clean_ref(ref) in used:
            continue
        title = str(visual.get("title") or Path(ref).name or "Visual")
        description = _description_from_visual(visual)
        parts = [f"### {title}"]
        if ref:
            parts.append(f"![]({ref})")
        if description:
            parts.append(description)
        extras.append("\n\n".join(parts))
    if extras:
        sections.append("## Visual assets\n\n" + "\n\n".join(extras))
    return "\n\n".join(sections).strip() + "\n"


def verify_images(markdown_path: str | Path) -> dict[str, Any]:
    """Report broken local image references in one generated Markdown file."""
    path = Path(markdown_path)
    result: dict[str, Any] = {"refs": 0, "resolved": 0, "missing": []}
    if not path.is_file():
        return result
    for ref in _markdown_refs(path.read_text(encoding="utf-8")):
        if ref.startswith(("http://", "https://", "data:")) or Path(ref).is_absolute():
            continue
        result["refs"] += 1
        if (path.parent / ref).is_file():
            result["resolved"] += 1
        else:
            result["missing"].append(ref)
    return result


class DatasheetOptimizationPipeline:
    """Run the complete, linear datasheet optimization workflow."""

    def __init__(
        self,
        *,
        chunk_size: int = 5000,
        agent_mode: str = "required",
        use_vlm: bool = True,
        llm_model: str | None = None,
        vlm_model: str | None = None,
        reasoning: bool | None = None,
        agent_factory: OptimizationAgentFactory | None = None,
        concurrency: int = 6,
    ) -> None:
        if chunk_size < 256:
            raise ValueError("chunk_size must be at least 256 characters")
        if agent_mode not in {"required", "optional", "off"}:
            raise ValueError("agent_mode must be required, optional, or off")
        if agent_mode != "off" and agent_factory is None:
            raise ValueError(
                "agent_factory is required when agent_mode enables Agent calls"
            )
        self.chunk_size = chunk_size
        self.agent_mode = agent_mode
        self.use_vlm = use_vlm
        self.llm_model = llm_model
        self.vlm_model = vlm_model
        self.reasoning = reasoning
        self.agent_factory = agent_factory
        if int(concurrency) < 1:
            raise ValueError("concurrency must be >= 1")
        self.concurrency = int(concurrency)

    def _optimize_text(
        self, source_markdown: str, warnings: list[str]
    ) -> tuple[list[dict[str, str]], int]:
        chunks = _split_markdown(source_markdown, self.chunk_size)
        if self.agent_mode == "off":
            blocks = [
                {"type": "text", "title": guess_title(chunk), "content": chunk}
                for chunk in chunks
            ]
            return _restore_and_filter_refs(source_markdown, blocks, warnings), 0

        # Each chunk runs its own layout → segmentation → coherence chain; chunks are
        # independent, so they run concurrently and are re-joined in order.
        with ThreadPoolExecutor(max_workers=max(1, int(self.concurrency))) as pool:
            outcomes = list(pool.map(lambda item: self._optimize_chunk(*item), enumerate(chunks)))

        blocks: list[dict[str, str]] = []
        calls = 0
        for segmented, chunk_calls, chunk_warnings in outcomes:
            blocks.extend(segmented)
            calls += chunk_calls
            warnings.extend(chunk_warnings)
        return _restore_and_filter_refs(source_markdown, blocks, warnings), calls

    def _optimize_chunk(self, chunk_index: int, chunk: str) -> tuple[list[dict[str, str]], int, list[str]]:
        """Layout → segmentation → coherence for one chunk; returns (blocks, calls, warnings)."""

        warnings: list[str] = []
        calls = 0
        layout = chunk
        try:
            assert self.agent_factory is not None
            layout, uncertainty = self.agent_factory.layout(
                model=self.llm_model, reasoning=self.reasoning
            ).optimize(chunk)
            calls += 1
            layout = _safe_agent_output(chunk, layout, warnings, "layout_agent")
            if uncertainty and uncertainty.lower() != "none":
                warnings.append(f"layout_uncertainty:chunk_{chunk_index + 1}:{uncertainty}")
        except Exception as exc:
            if self.agent_mode == "required":
                raise
            warnings.append(f"layout_agent_failed:chunk_{chunk_index + 1}:{type(exc).__name__}:{exc}")

        segmented = [{"type": "text", "title": guess_title(layout), "content": layout}]
        try:
            assert self.agent_factory is not None
            segmented = self.agent_factory.segmentation(
                model=self.llm_model, reasoning=self.reasoning
            ).segment(layout)
            calls += 1
            joined = "\n\n".join(block["content"] for block in segmented)
            if _introduced_inference(layout, joined):
                warnings.append("segmentation_agent_rejected:introduced_inference")
                segmented = [{"type": "text", "title": guess_title(layout), "content": layout}]
        except Exception as exc:
            if self.agent_mode == "required":
                raise
            warnings.append(
                f"segmentation_agent_failed:chunk_{chunk_index + 1}:{type(exc).__name__}:{exc}"
            )

        try:
            before_merge = segmented
            assert self.agent_factory is not None
            segmented = self.agent_factory.coherence(
                model=self.llm_model, reasoning=self.reasoning
            ).merge(segmented)
            if len(before_merge) > 1:
                calls += 1
            joined = "\n\n".join(block["content"] for block in segmented)
            source_joined = "\n\n".join(block["content"] for block in before_merge)
            if _introduced_inference(source_joined, joined):
                warnings.append("coherence_agent_rejected:introduced_inference")
                segmented = before_merge
        except Exception as exc:
            if self.agent_mode == "required":
                raise
            warnings.append(
                f"coherence_agent_failed:chunk_{chunk_index + 1}:{type(exc).__name__}:{exc}"
            )
        return segmented, calls, warnings

    def run(
        self,
        *,
        mineru: str | Path,
        output_dir: str | Path,
        lichuang_id: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        content_list, mineru_dir, device_id = _resolve_content_list(mineru, lichuang_id)
        target = Path(output_dir).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)

        raw = json.loads(content_list.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not all(isinstance(entry, dict) for entry in raw):
            raise ValueError(f"MinerU content_list must be a JSON list of objects: {content_list}")
        raw_entries: list[dict[str, Any]] = raw
        warnings: list[str] = []

        source = _source_markdown(content_list, mineru_dir, raw_entries)
        text_entries, text_calls = self._optimize_text(source, warnings)
        text_markdown = "\n\n".join(str(entry.get("content") or "") for entry in text_entries)
        image_plan = _materialize_images(
            markdown=text_markdown,
            entries=raw_entries,
            content_list=content_list,
            mineru_dir=mineru_dir,
            output_dir=target,
            warnings=warnings,
        )
        for entry in text_entries:
            entry["content"] = _rewrite_refs(str(entry.get("content") or ""), image_plan)

        visual_entries, visual_calls = _build_visual_entries(
            raw_entries=raw_entries,
            plan=image_plan,
            agent_mode=self.agent_mode,
            use_vlm=self.use_vlm,
            llm_model=self.llm_model,
            vlm_model=self.vlm_model,
            reasoning=self.reasoning,
            agent_factory=self.agent_factory,
            warnings=warnings,
            concurrency=self.concurrency,
        )
        enrichment = _attach_img_comments(text_entries, visual_entries)
        optimized_entries = text_entries + visual_entries
        markdown = _optimized_markdown(text_entries, visual_entries)

        markdown_path = target / f"opti_{device_id}.md"
        content_path = target / f"opti_{device_id}_content_list.json"
        markdown_path.write_text(markdown, encoding="utf-8")
        content_path.write_text(
            json.dumps(optimized_entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        image_check = verify_images(markdown_path)

        return {
            "pipeline": "datasheet_optimization_pipeline",
            "lichuang_id": device_id,
            "input": str(content_list),
            "output_dir": str(target),
            "outputs": {
                "markdown": str(markdown_path),
                "content_list": str(content_path),
            },
            "stages": {
                "input": {"entries": len(raw_entries), "source_markdown_chars": len(source)},
                "text_optimization": {
                    "chunks": len(_split_markdown(source, self.chunk_size)),
                    "entries": len(text_entries),
                    "agent_calls": text_calls,
                },
                "images": image_plan.report,
                "visual_optimization": {
                    "entries": len(visual_entries),
                    "agent_calls": visual_calls,
                },
                "enrichment": enrichment,
            },
            "agents": {
                "mode": self.agent_mode,
                "use_vlm": self.use_vlm,
                "factory_configured": self.agent_factory is not None,
            },
            "image_check": image_check,
            "warnings": warnings,
            "elapsed_s": round(time.perf_counter() - started, 3),
        }


__all__ = [
    "DatasheetOptimizationPipeline",
    "verify_images",
]

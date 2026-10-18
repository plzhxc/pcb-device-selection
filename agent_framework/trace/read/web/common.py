"""Trace Web 模板环境与纯展示格式化函数。

HTML、CSS、JavaScript 都在 ``templates/`` 与 ``static/`` 中。这里不再拼页面标签，
只把数据库值规范化为模板可安全自动转义的普通字符串。
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape


WEB_ROOT = Path(__file__).resolve().parent
TEMPLATE_ROOT = WEB_ROOT / "templates"
STATIC_ROOT = WEB_ROOT / "static"


def path_for(kind: str, identifier: Any) -> str:
    """生成下钻路径；identifier 只进入 URL path segment。"""

    return f"/{quote(str(kind), safe='')}/{quote(str(identifier), safe='')}"


def status_class(status: Any) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"success", "succeeded", "completed", "complete"}:
        return "b-ok"
    if normalized in {
        "error",
        "failed",
        "failure",
        "invalid",
        "output_invalid",
        "validation_failed",
        "plan_invalid",
        "merge_failed",
    }:
        return "b-err"
    if normalized in {
        "running",
        "started",
        "pending",
        "retrying",
        "partial",
        "cancelled",
    }:
        return "b-run"
    return "b-mute"


def shorten(value: Any, length: int = 12) -> str:
    if value is None or value == "":
        return "—"
    text = str(value)
    return text if len(text) <= length else f"{text[:length]}…"


def timestamp(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    text = str(value)
    if "T" in text:
        text = text.replace("T", " ", 1)
    if "." in text:
        text = text.split(".", 1)[0]
    return text[:19]


def duration_ms(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        milliseconds = max(0, int(value))
    except (TypeError, ValueError):
        return str(value)
    if milliseconds < 1000:
        return f"{milliseconds} ms"
    seconds = milliseconds / 1000
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes, seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def number(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return str(value or "—")


def _unwrap_nested_json(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, dict):
        return {
            key: _unwrap_nested_json(item, depth + 1) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_unwrap_nested_json(item, depth + 1) for item in value]
    if isinstance(value, str):
        text = value.strip()
        if (text.startswith("{") and text.endswith("}")) or (
            text.startswith("[") and text.endswith("]")
        ):
            try:
                return _unwrap_nested_json(json.loads(text), depth + 1)
            except (json.JSONDecodeError, ValueError):
                pass
    return value


def pretty_json(value: Any) -> str:
    normalized = _unwrap_nested_json(value)
    try:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    except Exception:
        return repr(normalized)


def preview(value: Any, length: int | None = None) -> str:
    """返回完整的单行预览；详情折叠只改变布局，不丢失字符。"""

    del length
    full = pretty_json(value)
    return " ".join(full.splitlines())


_templates = Environment(
    loader=FileSystemLoader(str(TEMPLATE_ROOT)),
    autoescape=select_autoescape(("html", "xml")),
    enable_async=False,
)
_templates.filters.update(
    {
        "duration_ms": duration_ms,
        "number": number,
        "pretty_json": pretty_json,
        "preview": preview,
        "shorten": shorten,
        "status_class": status_class,
        "timestamp": timestamp,
    }
)
_templates.globals["path_for"] = path_for


def render_page(
    request: Any,
    template_name: str,
    *,
    title: str,
    **context: Any,
) -> str:
    """渲染一个真实模板文件；数据库值由 Jinja2 自动转义。"""

    template = _templates.get_template(template_name)
    return template.render(
        request=request,
        title=title,
        brand=str(getattr(request.app.state, "trace_title", "Agent Tracer")),
        **context,
    )


__all__ = [
    "STATIC_ROOT",
    "TEMPLATE_ROOT",
    "duration_ms",
    "number",
    "path_for",
    "pretty_json",
    "preview",
    "render_page",
    "shorten",
    "status_class",
    "timestamp",
]

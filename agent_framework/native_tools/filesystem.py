"""受工作区约束的原生编码工具。

这里刻意拆成四个小工具，而不是一个带 ``action`` 的万能 Scratchpad。模型拿到的
权限面更清楚，Trace 也能直接区分列举、读取、搜索和精确修改。
"""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
from typing import Any, ClassVar

from ..tool import (
    BaseTool,
    ToolCapability,
    ToolOutput,
    ToolTimeoutError,
)
from .workspace import WorkspaceBoundary


class _WorkspaceTool(BaseTool):
    def __init__(self, root: str | Path) -> None:
        super().__init__()
        self.workspace = WorkspaceBoundary(root)


class ListFilesTool(_WorkspaceTool):
    name = "list_files"
    description = "列出工作区内匹配 glob 的文件和目录；结果有硬上限。"
    capability = ToolCapability.READ
    parallel_safe = False
    detachable_on_timeout = True
    timeout_s = 10.0
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "default": ".",
                "description": "相对工作区的起始目录。",
            },
            "pattern": {
                "type": "string",
                "default": "**/*",
                "description": "相对于 path 的 glob，例如 **/*.py。",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "default": 200,
            },
        },
        "additionalProperties": False,
    }

    def call(self, arguments: dict[str, Any], **kwargs: Any) -> ToolOutput:
        base = self.workspace.resolve(arguments.get("path", "."))
        if not base.is_dir():
            raise NotADirectoryError(self.workspace.relative(base))
        pattern = self.workspace.validate_glob(arguments.get("pattern", "**/*"))
        limit = min(int(arguments.get("max_results", 200)), 500)
        entries = []
        truncated = False
        for candidate in base.glob(pattern):
            try:
                resolved = self.workspace.resolve(candidate)
            except (FileNotFoundError, PermissionError):
                continue
            entries.append(
                {
                    "path": self.workspace.relative(resolved),
                    "type": "directory" if resolved.is_dir() else "file",
                    **({"size": resolved.stat().st_size} if resolved.is_file() else {}),
                }
            )
            if len(entries) > limit:
                truncated = True
                entries = entries[:limit]
                break
        entries.sort(key=lambda item: (item["type"] != "directory", item["path"]))
        return ToolOutput(
            content={"entries": entries, "truncated": truncated},
            metadata={"result_count": len(entries), "truncated": truncated},
        )


class ReadFileTool(_WorkspaceTool):
    name = "read_file"
    description = (
        "按行读取工作区内的 UTF-8 文本文件。支持 offset/limit 分页，并限制总字节数。"
    )
    capability = ToolCapability.READ
    parallel_safe = False
    detachable_on_timeout = True
    timeout_s = 10.0
    max_output_chars = 64_000
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "offset": {
                "type": "integer",
                "minimum": 1,
                "default": 1,
                "description": "起始行号，从 1 开始。",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2_000,
                "default": 400,
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def call(self, arguments: dict[str, Any], **kwargs: Any) -> ToolOutput:
        path = self.workspace.resolve(arguments["path"])
        if not path.is_file():
            raise IsADirectoryError(self.workspace.relative(path))
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("read_file 拒绝读取超过 10MB 的文件")
        offset = int(arguments.get("offset", 1))
        limit = min(int(arguments.get("limit", 400)), 2_000)
        output_budget = 60_000
        selected: list[tuple[int, str]] = []
        truncated_lines: list[int] = []
        rendered_chars = 0
        lines_scanned = 0
        has_more = False
        next_offset: int | None = None
        reached_eof = True

        with path.open("rb") as handle:
            for number, raw_line in enumerate(handle, start=1):
                lines_scanned = number
                if b"\x00" in raw_line:
                    raise ValueError("read_file 只读取文本文件，检测到二进制内容")
                if number < offset:
                    continue
                if len(selected) >= limit:
                    has_more = True
                    next_offset = number
                    reached_eof = False
                    break
                line = raw_line.rstrip(b"\r\n").decode("utf-8", errors="replace")
                if len(line) > 8_000:
                    line = line[:8_000] + "..."
                    truncated_lines.append(number)
                rendered_line = f"{number}: {line}"
                if selected and rendered_chars + len(rendered_line) > output_budget:
                    has_more = True
                    next_offset = number
                    reached_eof = False
                    break
                selected.append((number, rendered_line))
                rendered_chars += len(rendered_line) + 1

        if offset > max(lines_scanned, 1):
            raise ValueError(f"offset {offset} 超过文件总行数 {lines_scanned}")
        rendered = "\n".join(line for _, line in selected)
        if has_more and next_offset is not None:
            rendered += f"\n\n（输出未完；下一次从 offset={next_offset} 继续）"
        return ToolOutput(
            content=rendered,
            metadata={
                "path": self.workspace.relative(path),
                "line_start": offset,
                "line_end": selected[-1][0] if selected else offset - 1,
                "known_line_count": lines_scanned if reached_eof else None,
                "has_more": has_more,
                "next_offset": next_offset,
                "truncated_lines": truncated_lines,
            },
        )


class SearchTextTool(_WorkspaceTool):
    name = "search_text"
    description = "使用 ripgrep 在工作区内搜索文本，支持文件 glob 和固定字符串模式。"
    capability = ToolCapability.READ
    parallel_safe = False
    timeout_s = 20.0
    parameters_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "minLength": 1, "maxLength": 1_000},
            "path": {"type": "string", "default": "."},
            "include": {
                "type": ["string", "null"],
                "maxLength": 200,
                "default": None,
                "description": "可选文件 glob，例如 *.py 或 **/*.yaml。",
            },
            "fixed_string": {"type": "boolean", "default": False},
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 50,
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    def call(self, arguments: dict[str, Any], **kwargs: Any) -> ToolOutput:
        executable = shutil.which("rg")
        if executable is None:
            raise RuntimeError("search_text 需要系统安装 ripgrep (rg)")
        target = self.workspace.resolve(arguments.get("path", "."))
        limit = min(int(arguments.get("max_results", 50)), 100)
        command = [
            executable,
            "--json",
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--max-columns",
            "2000",
            "--max-filesize",
            "2M",
        ]
        if arguments.get("fixed_string"):
            command.append("--fixed-strings")
        include = arguments.get("include")
        if include:
            command.extend(["--glob", self.workspace.validate_glob(str(include))])
        command.extend(["--", str(arguments["pattern"]), str(target)])
        process = subprocess.Popen(
            command,
            cwd=self.workspace.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
        )
        timed_out = threading.Event()

        def terminate_on_timeout() -> None:
            if process.poll() is None:
                timed_out.set()
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

        timer = threading.Timer(15.0, terminate_on_timeout)
        timer.daemon = True
        timer.start()
        matches = []
        truncated = False
        try:
            assert process.stdout is not None
            for raw in process.stdout:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "match":
                    continue
                data = event.get("data") or {}
                raw_path = str((data.get("path") or {}).get("text") or "")
                line_number = data.get("line_number")
                text = str((data.get("lines") or {}).get("text") or "").rstrip("\r\n")
                if not raw_path:
                    continue
                try:
                    path = self.workspace.relative(raw_path)
                except PermissionError:
                    continue
                matches.append(
                    {
                        "path": path,
                        "line": line_number,
                        "text": text[:2_000],
                    }
                )
                if len(matches) >= limit:
                    truncated = True
                    if process.poll() is None:
                        process.kill()
                    break
            return_code = process.wait()
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()

        if timed_out.is_set():
            raise ToolTimeoutError("search_text 超过 15 秒并已终止")
        if return_code not in (0, 1) and not truncated:
            raise RuntimeError(f"ripgrep 失败（exit={return_code}）")
        return ToolOutput(
            content={"matches": matches, "truncated": truncated},
            metadata={
                "result_count": len(matches),
                "truncated": truncated,
                "exit_code": return_code,
            },
        )


class ApplyPatchTool(_WorkspaceTool):
    name = "apply_patch"
    description = (
        "在一个已有文本文件中做精确替换。只有 old_text 出现次数与"
        " expected_replacements 完全一致时才原子写入，并返回 unified diff。"
    )
    capability = ToolCapability.WRITE
    mutates_workspace = True
    parallel_safe = False
    timeout_s = None
    _locks_guard: ClassVar[threading.Lock] = threading.Lock()
    _path_locks: ClassVar[dict[Path, threading.Lock]] = {}
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "old_text": {"type": "string", "minLength": 1, "maxLength": 100_000},
            "new_text": {"type": "string", "maxLength": 100_000},
            "expected_replacements": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 1,
            },
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    @classmethod
    def _path_lock(cls, path: Path) -> threading.Lock:
        with cls._locks_guard:
            return cls._path_locks.setdefault(path, threading.Lock())

    def call(self, arguments: dict[str, Any], **kwargs: Any) -> ToolOutput:
        path = self.workspace.resolve(arguments["path"], for_write=True)
        if not path.is_file():
            raise IsADirectoryError(self.workspace.relative(path))
        with self._path_lock(path):
            initial = path.stat(follow_symlinks=False)
            raw = path.read_bytes()
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("apply_patch 拒绝修改超过 2MB 的文件")
            if b"\x00" in raw:
                raise ValueError("apply_patch 只修改文本文件")
            original = raw.decode("utf-8")
            old = str(arguments["old_text"])
            new = str(arguments["new_text"])
            expected = int(arguments.get("expected_replacements", 1))
            actual = original.count(old)
            if actual != expected:
                raise ValueError(
                    f"old_text 实际出现 {actual} 次，期望 {expected} 次；文件未修改"
                )
            updated = original.replace(old, new, expected)
            relative = self.workspace.relative(path)
            diff = "".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    updated.splitlines(keepends=True),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )
            temporary_name = ""
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary_name = handle.name
                    handle.write(updated)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary_name, stat.S_IMODE(initial.st_mode))
                current = path.stat(follow_symlinks=False)
                identity = (
                    "st_dev",
                    "st_ino",
                    "st_mtime_ns",
                    "st_size",
                )
                if any(
                    getattr(initial, name) != getattr(current, name)
                    for name in identity
                ):
                    raise RuntimeError("文件在生成补丁后被并发修改；已拒绝覆盖")
                os.replace(temporary_name, path)
                temporary_name = ""
                if os.name == "posix":
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                if temporary_name and os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return ToolOutput(
            content={
                "path": relative,
                "replacements": expected,
                "diff": diff[:20_000],
            },
            metadata={
                "path": relative,
                "replacements": expected,
                "diff_truncated": len(diff) > 20_000,
            },
        )


__all__ = [
    "ApplyPatchTool",
    "ListFilesTool",
    "ReadFileTool",
    "SearchTextTool",
]

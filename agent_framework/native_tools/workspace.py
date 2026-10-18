"""原生文件工具共享的工作区边界。

路径安全不能靠提示词。所有文件工具都先在这里把模型给出的路径解析成真实路径，
再检查它仍位于固定工作区内；已有符号链接会被 ``resolve`` 展开，因此
``workspace/link -> /etc`` 这类逃逸会在访问前被拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspaceViolation(PermissionError):
    """路径试图离开工作区或触碰受保护目录。"""


@dataclass(frozen=True)
class WorkspaceBoundary:
    """一个不可变的工作区边界。"""

    root: Path
    protected_write_parts: tuple[str, ...] = (
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".claude",
    )

    def __init__(
        self,
        root: str | Path,
        *,
        protected_write_parts: tuple[str, ...] | None = None,
    ) -> None:
        resolved = Path(root).expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise NotADirectoryError(f"工作区不是目录：{resolved}")
        object.__setattr__(self, "root", resolved)
        if protected_write_parts is not None:
            object.__setattr__(
                self,
                "protected_write_parts",
                tuple(str(item) for item in protected_write_parts),
            )

    def resolve(
        self,
        value: str | Path = ".",
        *,
        must_exist: bool = True,
        for_write: bool = False,
    ) -> Path:
        """解析并验证路径，返回工作区内的绝对真实路径。"""

        raw = str(value or ".").strip() or "."
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve(strict=False)
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceViolation(f"路径越出工作区：{raw!r}") from exc

        if for_write and any(
            part in self.protected_write_parts for part in relative.parts
        ):
            raise WorkspaceViolation(f"禁止修改受保护路径：{relative}")
        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"路径不存在：{relative}")
        return resolved

    def relative(self, path: str | Path) -> str:
        """把已验证路径渲染成相对工作区的 POSIX 路径。"""

        resolved = self.resolve(path, must_exist=False)
        relative = resolved.relative_to(self.root)
        return relative.as_posix() or "."

    @staticmethod
    def validate_glob(pattern: str) -> str:
        """拒绝绝对路径和 ``..``，避免 glob 自己重新定义搜索根。"""

        normalized = str(pattern or "**/*").strip() or "**/*"
        path = Path(normalized)
        if path.is_absolute() or ".." in path.parts:
            raise WorkspaceViolation(f"glob 必须是工作区内的相对模式：{pattern!r}")
        return normalized


__all__ = ["WorkspaceBoundary", "WorkspaceViolation"]

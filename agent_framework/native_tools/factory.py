"""原生工具箱装配函数。

每次调用都返回新的 :class:`ToolBox`，不会让不同 Agent 共享带运行状态的工具箱。
"""

from __future__ import annotations

from pathlib import Path

from ..tool import BaseTool, ToolBox, ToolPolicy
from .filesystem import (
    ApplyPatchTool,
    ListFilesTool,
    ReadFileTool,
    SearchTextTool,
)
from .web_search import WebSearchProvider, WebSearchTool


def create_native_toolbox(
    *,
    web_provider: WebSearchProvider | None = None,
    web_region: str = "us-en",
    web_safe_search: str = "moderate",
    policy: ToolPolicy | None = None,
) -> ToolBox:
    """创建所有业务 Agent 默认拥有的最小原生工具箱。

    默认只含网络搜索。文件修改和命令执行需要调用方明确工作区后另行授予。
    """

    return ToolBox(
        [
            WebSearchTool(
                web_provider,
                region=web_region,
                safe_search=web_safe_search,
            )
        ],
        policy=policy,
    )


def create_coding_toolbox(
    root: str | Path,
    *,
    web_provider: WebSearchProvider | None = None,
    web_region: str = "us-en",
    web_safe_search: str = "moderate",
    include_web_search: bool = True,
    policy: ToolPolicy | None = None,
) -> ToolBox:
    """创建读取、搜索与补丁组成的工作区工具箱。

    命令执行不在这里伪装成“安全工具”：它需要宿主提供容器、bwrap/nsjail
    一类真正的 OS 沙箱后再单独接入。
    """

    tools: list[BaseTool] = [
        ListFilesTool(root),
        ReadFileTool(root),
        SearchTextTool(root),
        ApplyPatchTool(root),
    ]
    if include_web_search:
        tools.insert(
            0,
            WebSearchTool(
                web_provider,
                region=web_region,
                safe_search=web_safe_search,
            ),
        )
    return ToolBox(tools, policy=policy)


__all__ = ["create_coding_toolbox", "create_native_toolbox"]

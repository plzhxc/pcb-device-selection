"""框架原生工具。

``create_native_toolbox`` 是业务 Agent 的安全默认，只提供 ``web_search``；
``create_coding_toolbox`` 需要显式工作区，才会增加文件读取、搜索与精确修改能力。
"""

from .factory import create_coding_toolbox, create_native_toolbox
from .filesystem import (
    ApplyPatchTool,
    ListFilesTool,
    ReadFileTool,
    SearchTextTool,
)
from .web_search import (
    DDGSProvider,
    WebSearchProvider,
    WebSearchRequest,
    WebSearchResponse,
    WebSearchResult,
    WebSearchTool,
)
from .workspace import WorkspaceBoundary, WorkspaceViolation

__all__ = [
    "ApplyPatchTool",
    "DDGSProvider",
    "ListFilesTool",
    "ReadFileTool",
    "SearchTextTool",
    "WebSearchProvider",
    "WebSearchRequest",
    "WebSearchResponse",
    "WebSearchResult",
    "WebSearchTool",
    "WorkspaceBoundary",
    "WorkspaceViolation",
    "create_coding_toolbox",
    "create_native_toolbox",
]

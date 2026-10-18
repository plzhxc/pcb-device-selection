"""Agent 框架。

目录分两块，便于对照阅读：

新框架（顶层）——四层，按控制流形态划分，依赖严格单向 workflow → stage → agent → llm

    - :mod:`agent_framework.llm` —— **原子**：一次请求-响应，多厂商适配
    - :mod:`agent_framework.agent` —— **循环**：一个任务，调几次模型取决于模型表现
    - :mod:`agent_framework.stage` —— **图**：一个业务环节，命令式编排 + 步骤埋点
    - :mod:`agent_framework.workflow` —— **线性**：一次业务请求，串联环节 + 断点续跑

    共用基础设施：

    - :mod:`agent_framework.message` —— 消息的中立语义（四类 Message）
    - :mod:`agent_framework.tool` —— 工具的中立语义（定义 / 调用 / 结果）
    - :mod:`agent_framework.structured_output` —— 结构化输出的提取与校验
    - :mod:`agent_framework.trace.write.pipeline` —— 观测管道：发事件、传归属、摘要入参
    - :mod:`agent_framework.trace` —— 观测的落库与可视化（八张表 + 网页）

    新代码一律经子模块显式导入，不在本命名空间铺开::

        from agent_framework.llm import AsyncLLMClient, CallParams
        from agent_framework.agent import BaseAgent, AgentConfig
        from agent_framework.stage import Stage, StageContext
        from agent_framework.workflow import Workflow

旧框架（``old_code/``）
    重构前的实现，仍被现有应用使用。本模块转发其公开符号，使现有 import 保持
    可用；待新框架接管后整体移除。

**新旧同名但语义不同**：``Message`` / ``BaseTool`` / ``ToolParameter`` /
``StructuredOutput`` 在两套里都存在。本命名空间导出的是**旧版**，新版必须经
子模块路径访问。这条约定避免了"import 到哪一个全凭直觉"的隐患。
"""

from .old_code import (
    AgentServices,
    AgentServicesFactory,
    AgentSettings,
    AsyncLLMClientProtocol,
    BaseAgent,
    BaseTool,
    ContentPart,
    ContextDataContainer,
    HierarchicalAgent,
    LoggerProtocol,
    Message,
    MessageContent,
    ProblemContext,
    SourceType,
    StructuredOutput,
    SyncLLMClientProtocol,
    SystemPrompt,
    ToolParameter,
    UserPrompt,
    build_default_agent_services,
    configure_default_agent_services,
    get_default_agent_services,
)

__all__ = [
    "AgentServices",
    "AgentServicesFactory",
    "AgentSettings",
    "AsyncLLMClientProtocol",
    "BaseAgent",
    "BaseTool",
    "ContentPart",
    "ContextDataContainer",
    "HierarchicalAgent",
    "LoggerProtocol",
    "Message",
    "MessageContent",
    "ProblemContext",
    "SourceType",
    "StructuredOutput",
    "SyncLLMClientProtocol",
    "SystemPrompt",
    "ToolParameter",
    "UserPrompt",
    "build_default_agent_services",
    "configure_default_agent_services",
    "get_default_agent_services",
]

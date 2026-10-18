"""Agent 上下文与消息装配契约。

``ContextDataContainer`` 是一次 Agent 调用的可变工作区，按固定顺序保存系统
提示词、知识背景、用户输入、短期对话、可用工具和结构化输出声明。runner 不
直接拼消息，而是在每轮 LLM 调用前调用这里的 ``to_message()``。

多模态只体现在 ``UserPrompt.prompt_content``/``ConversationTurn.content`` 可以
是 content parts；其余执行流程与纯文本完全相同。
"""

import re
import uuid
import json
from typing import Any, Dict, List, Optional
from ..logging import StructuredLogger
from ..messages import Message, MessageContent
from ..tools import BaseTool
from dataclasses import dataclass, field
from jsonschema import Draft7Validator, SchemaError
from enum import Enum


class StrEnum(str, Enum):
    """兼容 Python 3.10 的字符串枚举基类。"""

    pass


@dataclass
class SystemPrompt:
    """系统提示词；一个 Agent 每次运行最多设置一份。"""
    prompt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""
    version: str = "1.0"
    prompt_content: str = ""

@dataclass
class UserPrompt:
    """用户提示词；内容既可以是字符串，也可以是多模态 content parts。"""
    prompt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""
    version: str = "1.0"
    prompt_content: MessageContent = ""

@dataclass
class ConversationTurn:
    """短期记忆中的一轮对话，字段与 LLM ``Message`` 一一对应。"""
    role: str  # 'user', 'assistant', 'system', 'tool', 'reasoning'
    content: Optional[MessageContent] = None
    tool_calls: Optional[List[Any]] = None
    tool_call_id: Optional[str] = None
    reasoning_content: Optional[str] = None
    turn_id: int = 0

    def to_message(self) -> Message:
        """恢复为 provider 无关的 ``Message``，供下一轮请求复用。"""
        return Message(
            role=self.role,
            content=self.content,
            tool_calls=self.tool_calls,
            tool_call_id=self.tool_call_id,
            reasoning_content=self.reasoning_content
        )

class SourceType(StrEnum):
    """知识背景来源类型。

    ``REFERE_NERTLIST`` 的拼写属于既有序列化值，暂时保留以避免数据不兼容。
    """
    DATASHEET = "datasheet"
    REFERE_NERTLIST = "refer_nertlist"
    DATABASE = "database"



@dataclass
class ProblemContext:
    """一条供模型参考的外部知识片段。"""

    source: SourceType
    title: str = ""
    content: str = ""


@dataclass
class ProblemContextBoard:
    """把多条知识片段聚合为一个稳定的用户消息区块。"""

    problem_context_list: List[ProblemContext] = field(default_factory=list)

    def to_text(self):
        """渲染为带标题的 JSON 文本，避免丢失来源和标题字段。"""
        text = "\n## 知识库背景\n"
        problems_context = []
        for problem_context in self.problem_context_list:
            problems_context.append(
                {
                    "source": problem_context.source,
                    "title": problem_context.title,
                    "content": problem_context.content,
                }
            )

        return text + json.dumps(problems_context, indent=4, ensure_ascii=False)

    def problem_context_append(self, problem: ProblemContext):
        """追加一条知识背景。"""
        self.problem_context_list.append(problem)


@dataclass
class ShortTermMemory:
    """仅服务当前 Agent 实例当前运行的对话历史。"""
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    conversation_history: List[ConversationTurn] = field(default_factory=list)

    def add_turn(self, message: Message):
        """添加对话轮次"""
        turn = ConversationTurn(
            role=message.role,
            content=message.content,
            tool_calls=message.tool_calls,
            tool_call_id=message.tool_call_id,
            reasoning_content=message.reasoning_content,
            turn_id=len(self.conversation_history) + 1,
        )
        self.conversation_history.append(turn)

    def to_messages(self) -> List[Message]:
        """将历史记录转换为消息列表"""
        return [turn.to_message() for turn in self.conversation_history]

    def clear_history(self):
        """清除聊天历史"""
        self.conversation_history = []


@dataclass
class StructuredOutput:
    """声明一个需要从 LLM 文本中提取的命名输出。

    ``extract_pattern`` 先定位文本区块；存在 ``schema_def`` 时，再把区块解析为
    JSON 并做 Schema 校验。``required=False`` 允许该区块缺席。
    """
    name: str  # 输出格式名称
    schema_def: Optional[Dict[str, Any]]=None  # JSON Schema定义
    description: str = ""
    extract_pattern: str = ""
    required: bool = True

    def __post_init__(self):
        """在实例初始化后自动调用，用于验证正则表达式和JSON Schema"""
        # 验证正则表达式
        if self.extract_pattern and not self.validate_regex(self.extract_pattern):
            raise ValueError(f"无效的正则表达式: {self.extract_pattern}")

        try:
            # 验证JSON Schema
            if self.schema_def:
                Draft7Validator.check_schema(self.schema_def)
        except SchemaError as e:
            raise ValueError(f"无效的JSON Schema定义: {e}")

    @staticmethod
    def validate_regex(pattern: str) -> bool:
        """验证字符串是否是有效的正则表达式"""
        try:
            re.compile(pattern)
            return True
        except re.error:
            return False


# ==================== 一次 Agent 运行的统一上下文 ====================

class ContextDataContainer:
    """集中保存 prompt、知识、记忆、工具和输出契约。

    容器只负责数据组织，不调用模型，也不执行工具。runner 在每次新运行开始时
    调用 ``clear_all``，具体 Agent 的 ``format`` 随后重新填充这些字段。
    """

    def __init__(self, agent_name: str, logger: Any | None = None):
        """为指定 Agent 创建空的运行上下文。"""
        self.agent_name = agent_name
        self.logger = logger or StructuredLogger("agent_framework.context")
        self.system_prompt: Optional[SystemPrompt] = None
        self.problem_context_board: ProblemContextBoard = None
        self.short_term_memory: ShortTermMemory = ShortTermMemory()
        self.available_tools: Optional[Dict[str,BaseTool]] = None
        self.structured_output: Optional[List[StructuredOutput]] = None
        self.user_prompt: Optional[UserPrompt] = None

    def set_system_prompt(self, SystemPrompt: SystemPrompt) -> None:
        """设置本轮系统提示词，后一次调用覆盖前一次。"""
        self.system_prompt = SystemPrompt

    def set_user_prompt(self, UserPrompt: UserPrompt) -> None:
        """设置本轮用户输入，后一次调用覆盖前一次。"""
        self.user_prompt = UserPrompt

    def tools_append(self, tool: BaseTool) -> None:
        """按工具名注册工具；同名工具以后注册者为准。"""
        if self.available_tools is None:
            self.available_tools = {}
        self.available_tools.update({tool.name: tool})

    def problem_context_append(self, problem_context: ProblemContext) -> None:
        """延迟创建知识板并追加一条背景。"""
        if self.problem_context_board is None:
            self.problem_context_board = ProblemContextBoard()
        self.problem_context_board.problem_context_list.append(problem_context)

    @property
    def tool_list(self):
        """返回适合插入 prompt 的精简工具清单。"""
        if self.available_tools is None:
            return []
        return [
            {
                "name": tool.name,
                "description": tool.description,
            } for tool in self.available_tools.values()
        ]


    def structured_output_append(self, structured_output: StructuredOutput) -> None:
        """登记一个输出区块，runner 会在 LLM 返回后逐项提取。"""
        if self.structured_output is None:
            self.structured_output = []
        self.structured_output.append(structured_output)

    def to_message(self):
        """按固定顺序生成 provider 所需的消息字典列表。

        顺序为 system → knowledge → current user → short-term memory。reasoning
        只作为内部字段随 assistant turn 保存，不单独作为 provider role 发送。
        """
        messages = []
        if self.system_prompt:
            messages.append(Message(role="system", content=self.system_prompt.prompt_content))
        else:
            self.logger.error(
                {
                    "agent": self.agent_name,
                    "event": "system_prompt_not_defined",
                    "data": {
                        "messages": f"{self.agent_name} 的 System Prompt 未定义"
                    }
                }
            )

        # 知识库背景统一渲染成文本；当前用户提示词可以直接保留多模态 parts。
        if self.problem_context_board:
            messages.append(Message(role="user", content=str(self.problem_context_board.to_text())))

        if self.user_prompt:
            messages.append(Message(role="user", content=self.user_prompt.prompt_content))

        if self.short_term_memory:
            # 记忆中包含 assistant/tool 轮次，因此工具结果能自然进入下一次 LLM 请求。
            for msg in self.short_term_memory.to_messages():
                if msg.role == "reasoning":
                    continue
                # Message.to_dict 会原样保留字符串或多模态 content parts。
                messages.append(msg)

        # 转换为 OpenAI 字典格式
        formatted_messages = []
        for msg in messages:
            msg_dict = msg.to_dict()
            # 阿里云 Qwen API 对 content 字段非常敏感，如果是 None 必须显式处理
            if msg_dict.get("content") is None:
                # 规范：assistant 消息带 tool_calls 时 content 可以为 null
                # 但某些 API 还是要求必须有 content 键
                pass
            formatted_messages.append(msg_dict)

        return formatted_messages

    def clear_all(self, confirm: bool = True) -> None:
        """重置下一次运行需要重建的临时状态。

        ``confirm`` 是兼容参数，当前不参与控制流。知识背景板目前不在这里清除，
        因此同一实例重复运行时会继续保留已追加的背景。
        """
        self.system_prompt = None
        self.short_term_memory = ShortTermMemory()
        self.available_tools = None
        self.structured_output = None
        self.user_prompt = None

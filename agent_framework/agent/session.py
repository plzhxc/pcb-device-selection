"""会话：一次任务的上下文与累计状态。

一个 Agent 描述"怎么做事"（提示词模板、工具箱、输出声明），一个会话承载"这次
做事的过程"（对话、用量、工具结果）。二者分开之后：

- Agent 实例只含不变的东西，可被多个协程安全复用；
- 每个会话各持一份对话，不会互相污染；
- 多轮追问天然成立——上下文本来就攒在会话里。

**并发边界**：不同会话之间可以随意并发，各自持有对话，互不相干；但**同一个会话
不能并发**——它是一条对话线，两个协程同时往里追加消息会让 assistant 消息与工具
结果交错错位，模型收到的历史就是坏的。这条约束由运行时检测强制（见
:meth:`Session._enter`），违反时立即抛错，而不是留下随机串台的隐患。

单轮场景不必知道会话的存在，:meth:`BaseAgent.run` 内部开一次性会话即可。需要连续
对话时才显式取出::

    s = writer.session()
    s.run(chapter=1, topic="电源")
    s.follow_up("这一章再展开讲讲纹波")
    s.compact(keep_last=6)          # 上下文太长时压缩
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from ..trace.write.pipeline import digest as _digest
from ..llm.response import LLMResponse, Usage
from ..message import Message, SystemMessage, ToolMessage, UserMessage
from ..structured_output import StructuredOutput
from ..tool import ToolBox, ToolChoice, ToolResult
from .context import Context
from .result import AgentResult, accumulate_usage

if TYPE_CHECKING:
    from .base import BaseAgent


class Session:
    """一次会话；可跨多轮，上下文累积。

    Attributes:
        session_id: 会话标识；多轮追问共享同一个。
        messages: 完整对话轨迹，含工具往返。
        usage: 全部轮次累计的 token 用量。
        turns: 已执行的轮数。
        llm_calls: 累计的 LLM 调用次数。一轮里可能调多次（工具往返、输出纠错、
            传输重试都会各占一次），因此它通常大于 :attr:`turns`。
    """

    def __init__(self, agent: "BaseAgent") -> None:
        self.agent = agent
        self.session_id = uuid.uuid4().hex
        self._running = False
        self.messages: list[Message] = []
        self.usage = Usage()
        self.turns = 0
        #: 本会话累计发起的 LLM 调用次数（含纠错与传输重试）
        self.llm_calls = 0
        self.tool_results: list[ToolResult] = []
        # 首轮 prepare 确定的配置，续轮沿用
        self.outputs: Sequence[StructuredOutput] = agent.outputs
        self.tools: ToolBox = agent.tools
        self.tool_choice: ToolChoice | None = agent.tool_choice
        self.tool_context: Mapping[str, Mapping[str, Any]] = {}
        self.overrides: Mapping[str, Any] = {}
        self.model_tier: str | None = None
        # 最近一轮的入参摘要，仅供观测使用
        self.last_inputs: dict[str, Any] = {}

    # ------------------------------ 轮次 ------------------------------ #
    def _enter(self) -> None:
        """标记本会话开始执行；并发进入直接抛错。

        单会话串行不是可选的：并发追加消息会破坏对话顺序，而症状（模型答非所问、
        工具结果对不上号）远离根因，极难排查。宁可当场失败。
        """

        if self._running:
            raise RuntimeError(
                "同一个 Session 不能并发执行——它是一条对话线。"
                "需要并行请为每个任务各开一个 session（agent.session()），"
                "Agent 实例本身是可以共用的。"
            )
        self._running = True

    def _exit(self) -> None:
        self._running = False

    def _seed(self, **kwargs: Any) -> None:
        """让 Agent 准备首轮上下文，并据此固定本会话的配置。"""

        context = Context(
            outputs=self.agent.outputs,
            tools=self.agent.tools,
            tool_choice=self.agent.tool_choice,
        )
        self.last_inputs = {k: _digest(v) for k, v in kwargs.items()}
        self.agent.prepare(context, **kwargs)
        self.messages.extend(context.render_messages())
        self.outputs = context.outputs
        self.tools = context.tools or self.agent.tools
        self.tool_choice = context.tool_choice
        self.tool_context = context.tool_context
        self.overrides = context.overrides
        self.model_tier = context.model_tier

    def run(self, **kwargs: Any) -> AgentResult:
        """执行首轮；``kwargs`` 传给 :meth:`BaseAgent.prepare`。"""

        self._enter()
        try:
            self._seed(**kwargs)
            return self.agent._execute(self)
        finally:
            self._exit()

    async def arun(self, **kwargs: Any) -> AgentResult:
        """:meth:`run` 的异步版本。"""

        self._enter()
        try:
            self._seed(**kwargs)
            return await self.agent._aexecute(self)
        finally:
            self._exit()

    def follow_up(self, text: str) -> AgentResult:
        """带着已有上下文继续追问。"""

        self._enter()
        try:
            self.last_inputs = {"follow_up": _digest(text)}
            self.messages.append(UserMessage(text))
            return self.agent._execute(self)
        finally:
            self._exit()

    async def afollow_up(self, text: str) -> AgentResult:
        """:meth:`follow_up` 的异步版本。"""

        self._enter()
        try:
            self.last_inputs = {"follow_up": _digest(text)}
            self.messages.append(UserMessage(text))
            return await self.agent._aexecute(self)
        finally:
            self._exit()

    # ------------------------------ 记账 ------------------------------ #
    def note_attempt(self) -> None:
        """记一次已发出的调用（含失败与传输重试）。"""

        self.llm_calls += 1

    def record(self, response: LLMResponse) -> None:
        self.usage = accumulate_usage(self.usage, response.usage)

    def finish_turn(self) -> None:
        self.turns += 1

    # ------------------------------ 上下文压缩 ------------------------------ #
    def compact(self, *, keep_last: int = 8, summary: str | None = None) -> int:
        """压缩历史，返回被移除的消息数。

        系统消息始终保留——它承载角色与规则，丢了模型行为会漂移。中间部分可用
        ``summary`` 顶替：本方法只负责裁剪，**不替你调模型生成摘要**，因为要不要
        为压缩再花一次调用、摘要写多细，都是业务决策。

        ``keep_last`` 是下限而非上限：切口若正好落在一次工具调用中间，会向前挪到
        提出这些调用的助手消息，多留几条。工具结果必须紧跟在声明了对应
        ``tool_call_id`` 的助手消息之后，缺了那条助手消息，各家接口都直接 400；
        更麻烦的是压缩通常发生在长对话里，报错离触发点很远。

        典型用法是先自行生成摘要再传进来::

            digest = summarizer.run(text=session.transcript()).text
            session.compact(keep_last=6, summary=digest)
        """

        system = [m for m in self.messages if isinstance(m, SystemMessage)]
        rest = [m for m in self.messages if not isinstance(m, SystemMessage)]
        if len(rest) <= keep_last:
            return 0

        cut = len(rest) - keep_last if keep_last else len(rest)
        while 0 < cut < len(rest) and isinstance(rest[cut], ToolMessage):
            cut -= 1
        dropped = rest[:cut]
        kept = rest[cut:]
        bridge = [UserMessage(f"## 此前对话摘要\n{summary}")] if summary else []
        self.messages = [*system, *bridge, *kept]
        return len(dropped)

    def transcript(self) -> str:
        """把对话渲染成纯文本，便于交给摘要器或写日志。"""

        lines = []
        for message in self.messages:
            role = type(message).__name__.replace("Message", "").lower()
            lines.append(f"[{role}] {getattr(message, 'text', '')}")
        return "\n".join(lines)


__all__ = ["Session"]

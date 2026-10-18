"""一次运行的上下文准备。

提示词很少是"一个字符串"。它由几部分拼成：角色与任务的主模板、跨 Agent 复用的
约束、本次运行才知道的背景知识（数据手册、参考网表、前序阶段结论）、以及用户的
具体问题。把它们硬拼成一个大 f-string，既难读也难复用。

:class:`Context` 让这些部分**逐条累积**，最后渲染成消息。用法接近旧框架里顺手的
写法，但有一个关键差别：``ctx`` 是 :class:`BaseAgent` 每次运行**新建后传进来的
参数**，不是挂在 Agent 实例上的状态。因此同一个 Agent 被多个协程并发调用时，各自
的上下文互不相干——并发安全来自结构，而不是"记得每次新建实例"的纪律。

准备完成后由 :class:`~agent_framework.agent.session.Session` 读走：消息进入会话的
对话轨迹，其余（输出声明、工具箱、注入值）成为该会话的配置，续轮沿用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..message import Message, SystemMessage, UserMessage
from ..structured_output import StructuredOutput
from ..tool import ToolBox, ToolChoice


@dataclass(frozen=True)
class Knowledge:
    """一条供模型参考的背景知识。

    标注 ``source`` 是必要的：模型需要分清哪段来自数据手册、哪段来自参考设计，
    否则容易把参考设计里的具体取值当成硬性规格照抄。
    """

    source: str
    title: str = ""
    content: str = ""


class Context:
    """上下文准备器；由 :class:`BaseAgent` 每次运行新建并传给 ``prepare``。"""

    def __init__(
        self,
        *,
        outputs: Sequence[StructuredOutput] = (),
        tools: ToolBox | None = None,
        tool_choice: ToolChoice | None = None,
    ) -> None:
        """以 Agent 的类级声明为默认值，``prepare`` 中可继续追加或覆盖。"""

        self._system: str = ""
        self._constraints: list[str] = []
        self._knowledge: list[Knowledge] = []
        self._notes: list[tuple[str, str]] = []
        self._messages: list[Message] = []
        self._user: str = ""
        self._outputs: list[StructuredOutput] = list(outputs)
        self._tools = tools
        self._tool_choice = tool_choice
        self._tool_values: dict[str, dict[str, Any]] = {}
        self._overrides: dict[str, Any] = {}
        self._model_tier: str | None = None

    # ------------------------------ 提示词 ------------------------------ #
    def system(self, template: str, **variables: Any) -> "Context":
        """设置系统提示主体；``variables`` 非空时按 ``str.format`` 渲染。

        长模板建议放在类常量或独立文件里，这里只负责填变量。
        """

        self._system = template.format(**variables) if variables else template
        return self

    def constraint(self, text: str) -> "Context":
        """追加一条约束，附在系统提示之后。

        独立于主模板，便于把"输出必须是合法 JSON"这类通用要求在多个 Agent 间复用。
        """

        self._constraints.append(text.strip())
        return self

    def user(self, text: str) -> "Context":
        """设置用户的具体问题；背景知识会渲染在它前面。"""

        self._user = text
        return self

    # ------------------------------ 背景 ------------------------------ #
    def knowledge(self, source: str, *, title: str = "", content: str = "") -> "Context":
        """追加一条带来源标注的背景知识。"""

        if content:
            self._knowledge.append(Knowledge(source=source, title=title, content=content))
        return self

    def note(self, title: str, content: str) -> "Context":
        """追加一条简短备注，如前序阶段的结论摘要。"""

        if content:
            self._notes.append((title, content))
        return self

    def message(self, message: Message) -> "Context":
        """直接追加一条原始消息。

        逃生舱：用于回放历史对话等结构化拼装表达不了的场景。
        """

        self._messages.append(message)
        return self

    # ------------------------------ 输出与工具 ------------------------------ #
    def output(self, output: StructuredOutput) -> "Context":
        """追加一项本次特有的结构化输出；固定的应声明在 Agent 类上。"""

        self._outputs.append(output)
        return self

    def use_tools(self, tools: ToolBox) -> "Context":
        """替换本次可用的工具箱。"""

        self._tools = tools
        return self

    def choose_tool(self, choice: ToolChoice) -> "Context":
        """设置本次的工具使用策略。"""

        self._tool_choice = choice
        return self

    def tool_values(self, tool_name: str, **values: Any) -> "Context":
        """给指定工具注入参数值，对应其声明为 ``injected=True`` 的参数。"""

        self._tool_values.setdefault(tool_name, {}).update(values)
        return self

    def override(self, **params: Any) -> "Context":
        """临时覆盖调用参数，如 ``model=...`` 或 ``timeout_s=...``。"""

        self._overrides.update(params)
        return self

    def model(self, tier: str | None = None, **params: Any) -> "Context":
        """按任务设置模型强度与调用参数。

        ``tier`` 是抽象的强度档位（如 ``strong`` / ``standard`` / ``fast``），
        由 :attr:`AgentConfig.model_tiers` 在调用时解析成具体模型名，所以 Agent
        代码只表达"这一步需要多强的模型"，不绑定厂商型号。``params`` 直接覆盖
        :class:`CallParams` 的字段（``reasoning=True``、``max_tokens=...``）。

        优先级：这里的设置 > 配置里的角色档位 > Agent 类上的 ``model_tier`` >
        配置的默认模型。
        """

        if tier is not None:
            self._model_tier = str(tier).strip()
        self._overrides.update(params)
        return self

    # ------------------------------ 渲染 ------------------------------ #
    def _render_system(self) -> str:
        parts = [self._system.strip()] if self._system.strip() else []
        parts.extend(self._constraints)
        return "\n\n".join(parts)

    def _render_background(self) -> str:
        """把知识与备注渲染成 Markdown 区块。

        用带标题的 Markdown 而非 JSON 转储：模型对前者的解析更稳，token 也更省。
        """

        blocks: list[str] = []
        if self._knowledge:
            lines = ["## 背景知识"]
            for item in self._knowledge:
                heading = f"### [{item.source}] {item.title}".rstrip()
                lines.append(f"{heading}\n{item.content}")
            blocks.append("\n\n".join(lines))
        if self._notes:
            lines = ["## 备注"]
            for title, content in self._notes:
                lines.append(f"### {title}\n{content}")
            blocks.append("\n\n".join(lines))
        return "\n\n".join(blocks)

    def _render_user(self) -> str:
        background = self._render_background()
        if background and self._user:
            return f"{background}\n\n---\n\n{self._user}"
        return background or self._user

    def render_messages(self) -> list[Message]:
        """渲染成消息序列：一条 system，随后是背景 + 问题的 user。"""

        messages: list[Message] = []
        system = self._render_system()
        if system:
            messages.append(SystemMessage(system))
        messages.extend(self._messages)
        user = self._render_user()
        if user:
            messages.append(UserMessage(user))
        return messages

    # ------------------------------ 只读视图 ------------------------------ #
    @property
    def outputs(self) -> tuple[StructuredOutput, ...]:
        return tuple(self._outputs)

    @property
    def tools(self) -> ToolBox | None:
        return self._tools

    @property
    def tool_choice(self) -> ToolChoice | None:
        return self._tool_choice

    @property
    def tool_context(self) -> dict[str, dict[str, Any]]:
        return {name: dict(values) for name, values in self._tool_values.items()}

    @property
    def overrides(self) -> dict[str, Any]:
        return dict(self._overrides)

    @property
    def model_tier(self) -> str | None:
        return self._model_tier


__all__ = ["Context", "Knowledge"]

"""Agent 运行结果。

失败必须能分型。"跑失败了"这一句话没有排障价值——模型陷入工具循环、输出始终
不合 Schema、输出被长度截断、以及底层调用异常，是四种完全不同的问题，处置方式
也不同（改提示词 / 放宽 Schema / 加大 max_tokens / 查服务端）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from ..llm.response import LLMResponse, Usage, accumulate_usage
from ..message import Message
from ..structured_output import OutputError
from .validation import ValidationReport
from ..tool import ToolResult


class AgentStatus(str, Enum):
    """一次运行的终态。"""

    #: 正常完成，结构化输出（若声明）全部通过校验
    SUCCESS = "success"
    #: 工具循环达到上限仍未收敛
    TOOL_LIMIT = "tool_limit"
    #: 纠错次数用尽，输出仍不符合 Schema
    OUTPUT_INVALID = "output_invalid"
    #: 格式正确但业务规则始终不满足
    VALIDATION_FAILED = "validation_failed"
    #: 模型输出被长度截断
    TRUNCATED = "truncated"
    #: 被内容审查拦截；重试无用，改提示词或换输入才有意义
    CONTENT_FILTERED = "content_filtered"
    #: 底层调用失败（重试后仍失败，或不可重试）
    ERROR = "error"
    #: 调用方主动取消；向上传播，不与模型或业务失败混为一类
    CANCELLED = "cancelled"
    #: 分层 Agent：Master 未产出可用的计划
    PLAN_INVALID = "plan_invalid"
    #: 分层 Agent：子任务结果合并失败
    MERGE_FAILED = "merge_failed"

    @property
    def ok(self) -> bool:
        return self is AgentStatus.SUCCESS


@dataclass(frozen=True)
class AgentResult:
    """一次 Agent 运行的完整结果。

    Attributes:
        status: 终态。
        run_id: 本次运行的标识，用于与观测数据关联。
        values: 结构化输出；未声明输出时为空。
        text: 模型最终正文。
        messages: 完整对话轨迹，含工具往返，便于复盘。
        tool_results: 本次运行执行过的全部工具调用结果。
        usage: 本次运行累计的 token 用量。
        output_errors: 结构化输出的校验错误（``OUTPUT_INVALID`` 时有值）。
        validation: 业务规则校验结果；即使成功也可能带 warning。
        repairs: 为拿到结果而对模型输出施加的修复，如 ``{"plan": ["strip_fences"]}``。
            同一类修复反复出现，说明提示词该改了——容错不该长期替提示词兜底。
        error: 底层异常（``ERROR`` 时有值）。
        iterations: 实际经历的工具循环轮数。
        llm_calls: 实际发起的 LLM 调用次数（含纠错与重试）。
    """

    status: AgentStatus
    #: 本次运行的标识；与观测库 agent_run_info.agent_run_id 对应
    run_id: str = ""
    values: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    messages: Sequence[Message] = ()
    tool_results: Sequence[ToolResult] = ()
    usage: Usage = field(default_factory=Usage)
    output_errors: Sequence[OutputError] = ()
    validation: ValidationReport = field(default_factory=ValidationReport)
    repairs: dict[str, list[str]] = field(default_factory=dict)
    error: BaseException | None = None
    response: LLMResponse | None = None
    iterations: int = 0
    llm_calls: int = 0

    @property
    def ok(self) -> bool:
        return self.status.ok

    def failure_summary(self, *, limit: int = 3, chars: int = 240) -> str | None:
        """把失败原因压成一行可读文本；成功时返回 ``None``。

        ``validation_failed`` 时 :attr:`error` 通常为空，真正可行动的诊断在
        :attr:`validation` 与 :attr:`output_errors` 里，所以优先取它们，最多保留
        ``limit`` 条、每条 ``chars`` 字符，避免把整份校验报告灌进 stage 产物。
        """

        if self.ok:
            return None

        def compact(value: Any, size: int) -> str:
            text = " ".join(str(value or "").split())
            return text if len(text) <= size else text[: size - 1].rstrip() + "…"

        details: list[str] = []
        issues = [
            (getattr(i, "code", None) or "validation_error", getattr(i, "path", None), getattr(i, "message", None))
            for i in (getattr(self.validation, "errors", ()) or ())
        ] + [
            (getattr(e, "kind", None) or "output_invalid", getattr(e, "output_name", None), getattr(e, "message", None))
            for e in (self.output_errors or ())
        ]
        for code, path, message in issues[:limit]:
            head = compact(code, 80) + (f" @ {compact(path, 120)}" if path else "")
            details.append(f"{head}: {compact(message, chars)}" if message else head)
        if len(issues) > limit:
            details.append(f"{len(issues) - limit} more errors omitted")
        if details:
            return "; ".join(details)
        if self.error is not None:
            return f"{self.status.value}: {compact(self.error, chars)}"
        return self.status.value

    def __bool__(self) -> bool:
        return self.ok


__all__ = ["AgentResult", "AgentStatus", "accumulate_usage"]

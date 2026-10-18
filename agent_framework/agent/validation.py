"""业务规则校验。

Schema 只能回答"格式对不对"，回答不了"内容对不对"。一份网表可以完全符合
JSON Schema，却引用了不存在的引脚、把 3.3V 器件接到 5V 网上、选了库里没有的
型号——这些才是真正要命的错误，而它们只有业务知识才判得出来。

校验器就是把这类知识接进循环：结构校验通过后再跑一遍业务规则，不通过就把问题
**发回给模型自我纠正**，和格式错误走同一条纠错路径。

与格式纠错**分开计数**（:attr:`AgentConfig.max_validation_retries`），因为两者
指向的问题不同：格式反复错说明提示词与 Schema 没对齐，业务规则反复不满足说明
上游数据有问题或规则本身需要放宽——混在一个计数器里就分不出来了。

严重级别分两档：``error`` 触发重试，``warning`` 只记录不阻断。有些规则（比如
"建议每个电源引脚配去耦电容"）是提醒而非硬约束，为它们重试是浪费。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from ..message import Message
from ..tool import ToolResult


Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class ValidationIssue:
    """一条校验发现。

    Attributes:
        code: 稳定的机器可读标识，用于统计与告警；不要写成变化的自然语言。
        message: 给模型看的说明——它要据此改，所以必须讲清"哪里错了、该怎么改"。
        severity: ``error`` 阻断并重试，``warning`` 仅记录。
        path: 出问题的位置，如 ``modules[2].pins[0]``；给了模型才知道改哪。
    """

    code: str
    message: str
    severity: Severity = "error"
    path: str | None = None

    @property
    def blocking(self) -> bool:
        return self.severity == "error"

    def as_prompt_line(self) -> str:
        location = f"（位置：{self.path}）" if self.path else ""
        return f"- [{self.code}]{location} {self.message}"


@dataclass(frozen=True)
class ValidationContext:
    """校验器可以看到的东西。

    不只给 ``values``：有些规则要参照工具查回来的事实（例如"选型必须在器件库里"
    需要看知识库查询结果），或要看对话里模型自己声明过什么。
    """

    values: Mapping[str, Any]
    text: str = ""
    messages: Sequence[Message] = ()
    tool_results: Sequence[ToolResult] = ()

    def tool_output(self, name: str) -> Any:
        """取某个工具最近一次成功结果的内容；没有则为 ``None``。"""

        for result in reversed(list(self.tool_results)):
            if result.name == name and result.ok:
                return result.content
        return None


class Validator(ABC):
    """一条业务规则。

    实现 :meth:`validate` 返回发现的问题；返回空序列表示通过。校验器**不应抛
    异常**表达"没通过"——异常留给校验器自身的故障，框架会把它降级成一条
    ``validator_error`` 告警，避免一个规则崩了就让整轮运行失败。
    """

    #: 规则名，用于日志与统计
    name: str = ""

    @abstractmethod
    def validate(
        self, ctx: ValidationContext
    ) -> Sequence[ValidationIssue]:
        """执行校验；返回问题列表，空列表表示通过。"""

        raise NotImplementedError

    async def avalidate(
        self, ctx: ValidationContext
    ) -> Sequence[ValidationIssue]:
        """异步版本。

        默认把已有同步规则卸载到线程，避免阻塞 Agent 的事件循环；原生异步规则
        可覆盖本方法，直接 await 数据库或网络客户端。
        """

        return await asyncio.to_thread(self.validate, ctx)


@dataclass(frozen=True)
class ValidationCrash:
    """某个校验器自己抛异常了——规则**没有执行**，结果既不是通过也不是不通过。"""

    validator: str
    error: str


@dataclass(frozen=True)
class ValidationReport:
    """一次校验的汇总。

    ``crashes`` 与 ``issues`` 分开放：一个是"规则没跑成"，一个是"规则跑了、发现
    问题"。混在一起最坏的后果是前者被当成后者——发回给模型让它改，模型改不了
    我们代码里的 bug，白烧几轮之后以"格式错误"收场，真正的原因一个字都看不到。
    """

    issues: Sequence[ValidationIssue] = ()
    crashes: Sequence[ValidationCrash] = ()

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.blocking]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if not issue.blocking]

    @property
    def ok(self) -> bool:
        """只有阻断级问题才算不通过。"""

        return not self.errors

    def correction_prompt(self) -> str:
        """把阻断级问题渲染成发回给模型的纠错文本。

        只发 error：把 warning 一起发会稀释重点，模型容易去改无关紧要的部分。
        """

        lines = [issue.as_prompt_line() for issue in self.errors]
        return (
            "输出的格式正确，但内容未通过业务规则校验。请针对下列问题修正后重新输出：\n"
            + "\n".join(lines)
        )

    def codes(self) -> list[str]:
        return [issue.code for issue in self.issues] + ["validator_error"] * len(self.crashes)

    def crash_summary(self) -> str:
        return "；".join(f"{c.validator}: {c.error}" for c in self.crashes)


_logger = logging.getLogger("agent_framework.validation")


def _crash(validator: Validator, exc: Exception) -> ValidationCrash:
    name = validator.name or type(validator).__name__
    _logger.warning("校验器 %s 执行失败，该规则本轮未生效", name, exc_info=True)
    return ValidationCrash(validator=name, error=f"{type(exc).__name__}: {exc}")


def run_validators(
    validators: Sequence[Validator], ctx: ValidationContext
) -> ValidationReport:
    """依次执行全部校验器，汇总问题。

    单个校验器抛异常不会打断其余规则——先把能查的都查完；但它会被记进
    ``crashes``，由调用方决定是否致命（见 ``AgentConfig.strict_validation``）。
    崩掉的规则**没有做出判断**，把它当成"通过"就等于业务校验被悄悄绕过。
    """

    issues: list[ValidationIssue] = []
    crashes: list[ValidationCrash] = []
    for validator in validators:
        try:
            issues.extend(validator.validate(ctx) or ())
        except Exception as exc:
            crashes.append(_crash(validator, exc))
    return ValidationReport(issues=tuple(issues), crashes=tuple(crashes))


async def arun_validators(
    validators: Sequence[Validator], ctx: ValidationContext
) -> ValidationReport:
    """:func:`run_validators` 的异步版本。"""

    issues: list[ValidationIssue] = []
    crashes: list[ValidationCrash] = []
    for validator in validators:
        try:
            issues.extend(await validator.avalidate(ctx) or ())
        except Exception as exc:
            crashes.append(_crash(validator, exc))
    return ValidationReport(issues=tuple(issues), crashes=tuple(crashes))


__all__ = [
    "Severity",
    "ValidationCrash",
    "ValidationContext",
    "ValidationIssue",
    "ValidationReport",
    "Validator",
    "arun_validators",
    "run_validators",
]

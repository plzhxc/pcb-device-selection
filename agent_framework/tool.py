"""工具的中立语义：定义、调用请求、执行结果。

本模块**不含任何厂商知识**——不产出 OpenAI 的 ``{"type":"function",...}``，也不产出
Anthropic 的 ``input_schema``。工具描述以 JSON Schema 这一各家共同的底座表达，
翻译成具体厂商格式是 ``llm/providers/`` 的职责。

工具箱是**每个 Agent 各自持有**的，不存在全局注册中心：模型每次调用都要重新
被告知有哪些工具（见 :meth:`ToolBox.specs`），它不会记住。

三个概念要分清：

- :class:`ToolSpec` —— 我们**告诉**模型有什么工具（定义）；
- :class:`ToolCall` —— 模型**请求**调用某个工具（模型输出）；
- :class:`ToolResult` —— 我们执行后**回填**给模型的结果。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import inspect
import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from jsonschema import Draft7Validator


class ToolCapability(str, Enum):
    """工具接触外部世界的能力类型，供权限策略和 Trace 使用。"""

    CUSTOM = "custom"
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    NETWORK = "network"


class PermissionDecision(str, Enum):
    """一次工具调用的权限决策。"""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class ToolStatus(str, Enum):
    """单次工具调用终态。"""

    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"
    TIMEOUT = "timeout"


class ToolTimeoutError(TimeoutError):
    """工具实现主动报告的超时；执行层会保留独立 ``timeout`` 终态。"""


@dataclass(frozen=True)
class ToolPermissionRequest:
    """交给权限策略/审批器的调用快照。"""

    tool_execution_id: str
    tool_call_id: str | None
    name: str
    capability: ToolCapability
    mutates_workspace: bool
    arguments: Mapping[str, Any]


ApprovalHandler = Callable[
    [ToolPermissionRequest],
    bool | Awaitable[bool],
]


@dataclass(frozen=True)
class ToolExecutionContext:
    """模型看不到的执行上下文。

    ``tool_execution_id`` 是框架自产生的稳定标识；不能依赖 provider 给的
    ``tool_call_id``，因为后者允许为空，也可能在坏响应中重复。
    """

    tool_execution_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str = ""
    session_id: str = ""
    approver: ApprovalHandler | None = None


@dataclass(frozen=True)
class ToolPolicy:
    """确定性的工具权限规则。

    优先级为工具名 > capability > default。应用可从 YAML 构造本对象，框架本身
    不读取配置文件或环境变量。``ASK`` 在没有审批器时按拒绝处理。
    """

    default: PermissionDecision | str = PermissionDecision.ALLOW
    by_capability: Mapping[ToolCapability | str, PermissionDecision | str] = field(
        default_factory=dict
    )
    by_tool: Mapping[str, PermissionDecision | str] = field(default_factory=dict)

    @staticmethod
    def _normalize(value: PermissionDecision | str) -> PermissionDecision:
        try:
            return (
                value
                if isinstance(value, PermissionDecision)
                else PermissionDecision(str(value).lower())
            )
        except ValueError as exc:
            raise ValueError(f"未知工具权限决策：{value!r}") from exc

    def decide(self, request: ToolPermissionRequest) -> PermissionDecision:
        if request.name in self.by_tool:
            return self._normalize(self.by_tool[request.name])
        for key in (request.capability, request.capability.value):
            if key in self.by_capability:
                return self._normalize(self.by_capability[key])
        return self._normalize(self.default)


# --------------------------------------------------------------------------- #
# 定义
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ToolParameter:
    """一项工具参数声明。

    Attributes:
        injected: 该参数由**调用方**提供，不交给模型决定。

            用于那些"模型不该猜"的值：上下文标识（flow_id、case_id）、路径根目录、
            当前处理的器件编号、租户与权限范围等。模型幻觉一个 id、或沿用上文里
            过期的值，都会造成难查的错误；安全相关的字段更不能由模型左右，否则
            提示注入就能改写它。

            标记为注入的参数**不会出现在交给模型的 schema 里**——模型压根不知道
            它存在，也就无从编造。
    """

    name: str
    type: str = "string"  # JSON Schema 类型：string/number/integer/boolean/object/array
    description: str = ""
    required: bool = False
    enum: tuple[Any, ...] | None = None
    injected: bool = False
    #: 需要 ``items`` / ``properties`` / 数值范围等完整表达时直接提供 JSON Schema。
    #: ``type`` / ``description`` / ``enum`` 会作为缺省值补入，不会覆盖这里的字段。
    schema: Mapping[str, Any] | None = None

    def to_schema(self) -> dict[str, Any]:
        result: dict[str, Any] = dict(self.schema or {})
        result.setdefault("type", self.type)
        if self.description:
            result.setdefault("description", self.description)
        if self.enum is not None:
            result.setdefault("enum", list(self.enum))
        return result


@dataclass(frozen=True)
class ToolChoice:
    """本次调用的工具使用策略。

    四种语义各家都支持，但表达方式不同（OpenAI 用字符串或嵌套对象，Anthropic 用
    ``{"type": "auto"|"any"|"tool"}``），因此这里用中立结构表达，由 provider 翻译。

    用 :meth:`use` 指定具体工具，而不是直接传工具名字符串——否则名叫 ``auto`` 的
    工具会和"自动选择"产生歧义。
    """

    mode: str = "auto"  # auto / none / required / specific
    tool_name: str | None = None

    @staticmethod
    def use(tool_name: str) -> "ToolChoice":
        """强制调用指定工具。"""

        return ToolChoice(mode="specific", tool_name=tool_name)


#: 模型自行决定是否调用工具（默认）
ToolChoice.AUTO = ToolChoice("auto")  # type: ignore[attr-defined]
#: 禁止调用工具，只产出文本
ToolChoice.NONE = ToolChoice("none")  # type: ignore[attr-defined]
#: 必须调用某个工具，但由模型选择调哪个
ToolChoice.REQUIRED = ToolChoice("required")  # type: ignore[attr-defined]


@dataclass(frozen=True)
class ToolSpec:
    """工具的中立描述，交给 provider 翻译成厂商格式。

    ``parameters`` 是标准 JSON Schema 对象，OpenAI 与 Anthropic 都以此为底座，
    差别只在外层包装，因此这里不需要为任何一家做形状妥协。
    """

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


def build_parameters_schema(parameters: Sequence[ToolParameter]) -> dict[str, Any]:
    """把参数声明列表编译成 JSON Schema 对象。

    注入参数被排除在外：schema 是给模型看的，模型不该知道这些字段的存在。
    """

    visible = [p for p in parameters if not p.injected]
    return {
        "type": "object",
        "properties": {p.name: p.to_schema() for p in visible},
        "required": [p.name for p in visible if p.required],
        # 模型偷偷补进 schema 之外的参数时当场反馈，不能让高危工具自行猜测。
        "additionalProperties": False,
    }


class BaseTool(ABC):
    """工具基类；元数据以类属性声明，执行逻辑由子类实现。

    参数分两半：普通参数交给模型决定，标了 ``injected=True`` 的由我们自己填。
    后者的值在**工具构造时绑定**——工具箱是一个 Agent 一份，建箱时本次运行的
    上下文（case_id、根目录、租户）通常已经确定::

        box = ToolBox([SaveNote(case_id="case19")])

    模型既看不到这些参数，也就无从编造；即便它凭空吐出同名字段，也会在执行前
    被绑定值覆盖。
    """

    name: str = ""
    description: str = ""
    parameters: Sequence[ToolParameter] = ()
    #: 参数复杂时可直接声明完整 JSON Schema；否则由 ``parameters`` 编译。
    parameters_schema: Mapping[str, Any] | None = None
    capability: ToolCapability = ToolCapability.CUSTOM
    mutates_workspace: bool = False
    #: 只有显式声明安全的工具才允许和同一批调用并行。旧工具没有声明时必须按
    #: 保守语义串行执行，不能因为升级框架就改变它们原有的调用顺序。
    parallel_safe: bool = False
    #: 同步工具超时后线程无法被强杀。只有显式声明“调用方停止等待后，后台继续
    #: 运行也绝无副作用”的工具，才允许使用线程级 soft timeout。
    detachable_on_timeout: bool = False
    timeout_s: float | None = None
    max_output_chars: int | None = 20_000
    #: Trace 中需要脱敏的模型参数名；常见 secret/token/password 名会自动脱敏。
    sensitive_parameters: Sequence[str] = ()

    def __init__(self, **injected: Any) -> None:
        """绑定注入参数的值。

        Raises:
            ValueError: 传入了未声明为 ``injected=True`` 的参数名。宁可在装配期
                就报错，也不要等到运行时才发现值悄悄没生效。
        """

        self._injected: dict[str, Any] = {}
        if injected:
            self.bind(**injected)

    def bind(self, **values: Any) -> "BaseTool":
        """补充或覆盖注入参数的值，返回自身以便链式调用。"""

        allowed = {p.name for p in self.parameters if p.injected}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(
                f"{type(self).__name__} 未声明这些注入参数："
                f"{', '.join(sorted(unknown))}；已声明的是 {sorted(allowed) or '（无）'}"
            )
        if not hasattr(self, "_injected"):
            self._injected = {}
        self._injected.update(values)
        return self

    @property
    def injected_values(self) -> dict[str, Any]:
        """当前已绑定的注入参数值。"""

        return dict(getattr(self, "_injected", {}))

    @abstractmethod
    def call(self, arguments: dict[str, Any], **kwargs: Any) -> Any:
        """执行工具。``arguments`` 已由工具箱解析并合入注入值。

        允许返回协程；异步执行路径会自动等待。
        """

        raise NotImplementedError

    def invoke(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        **kwargs: Any,
    ) -> Any:
        """兼容执行入口。

        新工具如需 run/session/call 标识可覆写本方法；旧工具继续只实现
        :meth:`call` 即可，不会因框架增加执行上下文而破坏签名。
        """

        return self.call(arguments, **kwargs)

    def spec(self) -> ToolSpec:
        """产出中立描述；注入参数不出现在其中。"""

        schema = (
            dict(self.parameters_schema)
            if self.parameters_schema is not None
            else build_parameters_schema(self.parameters)
        )
        Draft7Validator.check_schema(schema)
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=schema,
        )


# --------------------------------------------------------------------------- #
# 模型请求的调用
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ToolCall:
    """模型请求的一次工具调用。

    ``arguments`` 保留模型原样输出的 JSON 字符串：解析失败是需要反馈给模型的
    业务信息，不应在这一层静默吞掉。
    """

    id: str | None
    name: str
    arguments: str = ""

    def parsed_arguments(self) -> dict[str, Any] | None:
        """解析参数；无法解析成对象时返回 ``None``。"""

        text = (self.arguments or "").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None


@dataclass(frozen=True)
class ToolOutput:
    """工具实现返回的内容及执行元数据。"""

    content: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """一次工具执行的结果；失败同样以结果表达，不抛异常。"""

    tool_call_id: str | None
    name: str
    content: Any = None
    error: str | None = None
    status: ToolStatus = ToolStatus.SUCCESS
    tool_execution_id: str = ""
    duration_ms: int = 0
    permission: PermissionDecision = PermissionDecision.ALLOW
    requested_arguments: Any = field(default_factory=dict)
    effective_arguments: Any = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, ToolStatus):
            object.__setattr__(self, "status", ToolStatus(str(self.status)))
        if not isinstance(self.permission, PermissionDecision):
            object.__setattr__(
                self,
                "permission",
                PermissionDecision(str(self.permission)),
            )
        # 兼容旧调用方 ``ToolResult(..., error="...")``：它没有显式传 status。
        if self.error is not None and self.status is ToolStatus.SUCCESS:
            object.__setattr__(self, "status", ToolStatus.FAILED)

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.SUCCESS and self.error is None

    def as_text(self) -> str:
        """转成可回填给模型的文本。"""

        if not self.ok:
            return f"工具执行失败：{self.error}"
        if isinstance(self.content, str):
            return self.content
        try:
            return json.dumps(self.content, ensure_ascii=False, default=str)
        except Exception:
            return str(self.content)

    def trace_payload(self) -> dict[str, Any]:
        """供 Agent Trace 直接落库的完整、已限长执行快照。"""

        return {
            "tool_execution_id": self.tool_execution_id,
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "status": self.status.value,
            "permission": self.permission.value,
            "duration_ms": self.duration_ms,
            "requested_arguments": self.requested_arguments,
            "effective_arguments": self.effective_arguments,
            "result": self.content if self.ok else None,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


# --------------------------------------------------------------------------- #
# 工具箱
# --------------------------------------------------------------------------- #
class ToolBox:
    """一个 Agent 手上的工具箱：按名字持有工具，并把模型请求分发到对应实现。

    执行层不写日志、不碰 Agent 上下文：成功与失败都收敛成 :class:`ToolResult`，
    由调用方决定如何记录与回填。
    """

    _SECRET_FRAGMENTS = ("api_key", "apikey", "password", "passwd", "secret", "token")

    def __init__(
        self,
        tools: Iterable[BaseTool] = (),
        *,
        policy: ToolPolicy | None = None,
    ) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._policy_explicit = policy is not None
        self.policy = policy if policy is not None else ToolPolicy()
        for tool in tools:
            self.register(tool)

    def register(self, tool: BaseTool, *, replace: bool = False) -> "ToolBox":
        if not tool.name:
            raise ValueError(f"工具 {type(tool).__name__} 未声明 name")
        if tool.detachable_on_timeout and (
            tool.mutates_workspace
            or tool.capability in {ToolCapability.WRITE, ToolCapability.EXECUTE}
        ):
            raise ValueError(
                f"工具 {tool.name!r} 会写入或执行，不能声明 "
                "detachable_on_timeout=True"
            )
        if tool.name in self._tools and not replace:
            raise ValueError(
                f"工具名 {tool.name!r} 已注册；如需显式替换请传 replace=True"
            )
        # 装配期就验证 Schema，避免第一次真实模型调用才发现定义坏了。
        tool.spec()
        self._tools[tool.name] = tool
        return self

    def extend(
        self,
        tools: "ToolBox | Iterable[BaseTool]",
        *,
        replace: bool = False,
    ) -> "ToolBox":
        """合并另一组工具与显式权限策略。

        未显式传 ``policy`` 的工具箱没有策略所有权；显式策略会随工具一起继承。
        两边都显式配置且内容不同时直接报错，避免一次普通工具合并把 ``DENY``
        静默升级成 ``ALLOW``。
        """

        source: tuple[BaseTool, ...]
        inherited_policy: ToolPolicy | None = None
        if isinstance(tools, ToolBox):
            if tools._policy_explicit:
                if self._policy_explicit and self.policy != tools.policy:
                    raise ValueError("不能合并具有不同显式 ToolPolicy 的工具箱")
                inherited_policy = tools.policy
            source = tuple(tools._tools.values())
        else:
            source = tuple(tools)

        # 先完整预检，再一次性提交。任何坏 Schema、重名或矛盾能力声明都不能让
        # extend 失败后留下一半工具或已经改变的权限策略。
        incoming_names: set[str] = set()
        for tool in source:
            if not tool.name:
                raise ValueError(f"工具 {type(tool).__name__} 未声明 name")
            if tool.detachable_on_timeout and (
                tool.mutates_workspace
                or tool.capability in {ToolCapability.WRITE, ToolCapability.EXECUTE}
            ):
                raise ValueError(
                    f"工具 {tool.name!r} 会写入或执行，不能声明 "
                    "detachable_on_timeout=True"
                )
            if not replace and (
                tool.name in self._tools or tool.name in incoming_names
            ):
                raise ValueError(
                    f"工具名 {tool.name!r} 已注册；" "如需显式替换请传 replace=True"
                )
            tool.spec()
            incoming_names.add(tool.name)

        if inherited_policy is not None:
            self.policy = inherited_policy
            self._policy_explicit = True
        for tool in source:
            self._tools[tool.name] = tool
        return self

    def copy(self) -> "ToolBox":
        """复制注册表与策略；工具实例本身应保持无会话可变状态。"""

        return ToolBox(
            self._tools.values(),
            policy=self.policy if self._policy_explicit else None,
        )

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def is_parallel_safe(self, name: str) -> bool:
        tool = self.get(name)
        return bool(tool is not None and tool.parallel_safe)

    def bind(self, **values: Any) -> "ToolBox":
        """把上下文一次性分发给箱内所有工具，返回自身以便链式调用。

        只有声明了对应 ``injected=True`` 参数的工具会收到该值，其余忽略——因此
        可以把整份上下文一把传下去，不必为每个工具挑拣::

            box = ToolBox([SaveNote(), ReadFile(), Search()]).bind(
                flow_id=flow_id, root="/data"
            )

        若没有任何工具声明某个键，说明大概率是拼错了名字，直接抛错而不是静默
        丢弃——静默丢弃会让工具在运行时拿不到值，却难以定位原因。
        """

        unused = set(values)
        for tool in self._tools.values():
            declared = {p.name for p in tool.parameters if p.injected}
            applicable = {k: v for k, v in values.items() if k in declared}
            if applicable:
                tool.bind(**applicable)
                unused -= set(applicable)
        if unused:
            raise ValueError(
                f"没有任何工具声明这些注入参数：{', '.join(sorted(unused))}"
            )
        return self

    @property
    def empty(self) -> bool:
        return not self._tools

    def specs(self) -> list[ToolSpec]:
        """产出全部工具的中立描述。"""

        return [tool.spec() for tool in self._tools.values()]

    @classmethod
    def _redact_value(
        cls,
        value: Any,
        *,
        sensitive: set[str],
        key: str = "",
    ) -> Any:
        lowered = key.lower()
        if key and (
            lowered in sensitive
            or any(fragment in lowered for fragment in cls._SECRET_FRAGMENTS)
        ):
            return "***"
        if isinstance(value, Mapping):
            return {
                str(item_key): cls._redact_value(
                    item_value,
                    sensitive=sensitive,
                    key=str(item_key),
                )
                for item_key, item_value in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls._redact_value(item, sensitive=sensitive) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    @classmethod
    def _trace_arguments(cls, tool: BaseTool | None, arguments: Any) -> Any:
        sensitive = {
            str(name).lower()
            for name in (tool.sensitive_parameters if tool is not None else ())
        }
        return cls._redact_value(arguments, sensitive=sensitive)

    def requested_arguments(self, call: ToolCall) -> Any:
        """返回适合 Trace 的模型原始参数；非法 JSON 保留有限预览。"""

        tool = self.get(call.name)
        parsed = call.parsed_arguments()
        if parsed is None:
            return {"invalid_json": (call.arguments or "")[:500]}
        return self._trace_arguments(tool, parsed)

    @staticmethod
    def _schema_error(errors: Sequence[Any]) -> str:
        parts = []
        for error in errors[:5]:
            path = ".".join(str(item) for item in error.absolute_path)
            parts.append(f"{path + ': ' if path else ''}{error.message}")
        suffix = f"（另有 {len(errors) - 5} 项）" if len(errors) > 5 else ""
        return "工具参数不符合 Schema：" + "；".join(parts) + suffix

    def _prepare(
        self,
        call: ToolCall,
        injected: Mapping[str, Any] | None = None,
    ) -> tuple[BaseTool | None, dict[str, Any], Any, Any, str | None]:
        """定位工具、验证模型参数、合入注入值，并生成脱敏快照。"""

        tool = self._tools.get(call.name)
        if tool is None:
            requested = self.requested_arguments(call)
            return None, {}, requested, {}, f"工具箱里没有：{call.name!r}"
        parsed = call.parsed_arguments()
        if parsed is None:
            requested = self.requested_arguments(call)
            return (
                tool,
                {},
                requested,
                {},
                f"工具参数不是合法 JSON 对象：{call.arguments[:200]}",
            )

        requested = self._trace_arguments(tool, parsed)
        injected_names = {p.name for p in tool.parameters if p.injected}
        # 模型即使猜到隐藏参数名也无权提供；先丢弃，再由可信调用方注入。
        arguments = {
            key: value for key, value in parsed.items() if key not in injected_names
        }
        errors = sorted(
            Draft7Validator(tool.spec().parameters).iter_errors(arguments),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
        if errors:
            return tool, {}, requested, {}, self._schema_error(errors)

        # 绑定在工具实例上的值为底，执行期传入的同名值可覆盖（更贴近本次调用）。
        supplied = {**tool.injected_values, **(injected or {})}
        missing = []
        for parameter in tool.parameters:
            if not parameter.injected:
                continue
            if parameter.name in supplied:
                # 后写入即覆盖：模型即便凭空编出同名参数，也以调用方的值为准。
                arguments[parameter.name] = supplied[parameter.name]
            elif parameter.required:
                missing.append(parameter.name)
            else:
                arguments.pop(parameter.name, None)
        if missing:
            # 属于接线错误而非模型问题：调用方声明了要注入却没给值。
            return (
                tool,
                {},
                requested,
                {},
                f"缺少注入参数：{', '.join(missing)}",
            )
        effective = self._trace_arguments(tool, arguments)
        return tool, arguments, requested, effective, None

    @staticmethod
    def _context(context: ToolExecutionContext | None) -> ToolExecutionContext:
        if context is None:
            return ToolExecutionContext()
        if context.tool_execution_id:
            return context
        return ToolExecutionContext(
            run_id=context.run_id,
            session_id=context.session_id,
            approver=context.approver,
        )

    def _permission_request(
        self,
        call: ToolCall,
        tool: BaseTool,
        arguments: Mapping[str, Any],
        context: ToolExecutionContext,
    ) -> ToolPermissionRequest:
        return ToolPermissionRequest(
            tool_execution_id=context.tool_execution_id,
            tool_call_id=call.id,
            name=call.name,
            capability=tool.capability,
            mutates_workspace=tool.mutates_workspace,
            arguments=self._freeze(arguments),
        )

    @classmethod
    def _freeze(cls, value: Any) -> Any:
        """给审批器只读快照，防止其在 Schema 校验后篡改执行参数。"""

        if isinstance(value, Mapping):
            return MappingProxyType(
                {str(key): cls._freeze(item) for key, item in value.items()}
            )
        if isinstance(value, list):
            return tuple(cls._freeze(item) for item in value)
        if isinstance(value, tuple):
            return tuple(cls._freeze(item) for item in value)
        if isinstance(value, set):
            return frozenset(cls._freeze(item) for item in value)
        return value

    def _authorize_sync(
        self,
        request: ToolPermissionRequest,
        context: ToolExecutionContext,
    ) -> PermissionDecision:
        decision = self.policy.decide(request)
        if decision is not PermissionDecision.ASK:
            return decision
        if context.approver is None:
            return PermissionDecision.DENY
        approved = context.approver(request)
        if inspect.isawaitable(approved):
            close = getattr(approved, "close", None)
            if callable(close):
                close()
            raise RuntimeError("同步工具审批器不能返回协程")
        return PermissionDecision.ALLOW if approved else PermissionDecision.DENY

    async def _authorize_async(
        self,
        request: ToolPermissionRequest,
        context: ToolExecutionContext,
    ) -> PermissionDecision:
        decision = self.policy.decide(request)
        if decision is not PermissionDecision.ASK:
            return decision
        if context.approver is None:
            return PermissionDecision.DENY
        approved = context.approver(request)
        if inspect.isawaitable(approved):
            approved = await approved
        return PermissionDecision.ALLOW if approved else PermissionDecision.DENY

    @staticmethod
    def _bounded_content(
        content: Any,
        limit: int | None,
    ) -> tuple[Any, dict[str, Any]]:
        if limit is None or limit <= 0:
            return content, {"truncated": False}
        if isinstance(content, str):
            text = content
        else:
            try:
                text = json.dumps(content, ensure_ascii=False, default=str)
            except Exception:
                text = str(content)
        if len(text) <= limit:
            return content, {"truncated": False, "output_chars": len(text)}
        suffix = f"\n...（工具输出已截断，原始长度 {len(text)} 字符）"
        kept = max(0, limit - len(suffix))
        return (
            text[:kept] + suffix,
            {
                "truncated": True,
                "output_chars": len(text),
                "returned_chars": limit,
            },
        )

    def _success(
        self,
        *,
        call: ToolCall,
        tool: BaseTool,
        context: ToolExecutionContext,
        started: float,
        permission: PermissionDecision,
        requested: Any,
        effective: Any,
        outcome: Any,
    ) -> ToolResult:
        if isinstance(outcome, ToolOutput):
            content = outcome.content
            metadata = dict(outcome.metadata)
        else:
            content = outcome
            metadata = {}
        content, bounds = self._bounded_content(content, tool.max_output_chars)
        reported_truncation = bool(metadata.get("truncated"))
        metadata.update(bounds)
        metadata["truncated"] = reported_truncation or bool(bounds.get("truncated"))
        return ToolResult(
            call.id,
            call.name,
            content=content,
            status=ToolStatus.SUCCESS,
            tool_execution_id=context.tool_execution_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            permission=permission,
            requested_arguments=requested,
            effective_arguments=effective,
            metadata=metadata,
        )

    @staticmethod
    def _failure(
        *,
        call: ToolCall,
        context: ToolExecutionContext,
        started: float,
        error: str,
        status: ToolStatus = ToolStatus.FAILED,
        permission: PermissionDecision = PermissionDecision.ALLOW,
        requested: Any = None,
        effective: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        return ToolResult(
            call.id,
            call.name,
            error=error,
            status=status,
            tool_execution_id=context.tool_execution_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            permission=permission,
            requested_arguments={} if requested is None else requested,
            effective_arguments={} if effective is None else effective,
            metadata=dict(metadata or {}),
        )

    def execute(
        self,
        call: ToolCall,
        *,
        injected: Mapping[str, Any] | None = None,
        context: ToolExecutionContext | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        """同步执行一次工具调用。

        ``injected`` 提供由调用方决定的参数值，按工具声明的 ``injected=True``
        逐项取用；多余的键会被忽略，因此可以把一份上下文传给所有工具。
        """

        started = time.monotonic()
        execution_context = self._context(context)
        tool, arguments, requested, effective, error = self._prepare(call, injected)
        if error is not None:
            return self._failure(
                call=call,
                context=execution_context,
                started=started,
                error=error,
                requested=requested,
                effective=effective,
                metadata={"phase": "validation"},
            )
        assert tool is not None
        try:
            request = self._permission_request(call, tool, arguments, execution_context)
            permission = self._authorize_sync(request, execution_context)
            if permission is PermissionDecision.DENY:
                return self._failure(
                    call=call,
                    context=execution_context,
                    started=started,
                    error=f"权限策略拒绝工具 {call.name!r}",
                    status=ToolStatus.DENIED,
                    permission=permission,
                    requested=requested,
                    effective=effective,
                    metadata={"capability": tool.capability.value},
                )

            def invoke() -> Any:
                return tool.invoke(arguments, execution_context, **kwargs)

            # Python 线程无法被安全强杀。默认等工具真正结束；只有工具显式声明
            # detached 绝对无副作用时，才允许先返回 soft TIMEOUT。
            if tool.timeout_s is None or not tool.detachable_on_timeout:
                outcome = invoke()
            else:
                executor = ThreadPoolExecutor(max_workers=1)
                future = executor.submit(invoke)
                try:
                    outcome = future.result(timeout=tool.timeout_s)
                except FutureTimeoutError:
                    future.cancel()
                    return self._failure(
                        call=call,
                        context=execution_context,
                        started=started,
                        error=f"工具执行超过 {tool.timeout_s:g} 秒",
                        status=ToolStatus.TIMEOUT,
                        permission=permission,
                        requested=requested,
                        effective=effective,
                        metadata={
                            "timeout_s": tool.timeout_s,
                            "soft_timeout": True,
                        },
                    )
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)
        except ToolTimeoutError as exc:
            return self._failure(
                call=call,
                context=execution_context,
                started=started,
                error=str(exc) or "工具执行超时",
                status=ToolStatus.TIMEOUT,
                permission=locals().get("permission", PermissionDecision.ALLOW),
                requested=requested,
                effective=effective,
            )
        except Exception as exc:  # 工具内部异常不应中断整轮对话
            return self._failure(
                call=call,
                context=execution_context,
                started=started,
                error=f"{type(exc).__name__}: {exc}",
                permission=locals().get("permission", PermissionDecision.ALLOW),
                requested=requested,
                effective=effective,
            )
        if inspect.isawaitable(outcome):
            # 同步路径拿到协程说明接线错了。不拦下的话，coroutine 的 repr 会被当作
            # 工具结果喂回模型（"<coroutine object ...>"），模型据此胡编，而且
            # 协程从未被 await，连"没执行"这件事都无从发现。
            close = getattr(outcome, "close", None)
            if callable(close):
                close()
            return self._failure(
                call=call,
                context=execution_context,
                started=started,
                error=(
                    f"工具 {call.name!r} 的 call() 是协程，必须走异步路径执行："
                    "请改用 agent.arun()/ToolBox.execute_async()"
                ),
                permission=permission,
                requested=requested,
                effective=effective,
            )
        return self._success(
            call=call,
            tool=tool,
            context=execution_context,
            started=started,
            permission=permission,
            requested=requested,
            effective=effective,
            outcome=outcome,
        )

    async def execute_async(
        self,
        call: ToolCall,
        *,
        injected: Mapping[str, Any] | None = None,
        context: ToolExecutionContext | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        """异步执行；同步工具在线程中运行，协程工具在当前事件循环等待。"""

        started = time.monotonic()
        execution_context = self._context(context)
        tool, arguments, requested, effective, error = self._prepare(call, injected)
        if error is not None:
            return self._failure(
                call=call,
                context=execution_context,
                started=started,
                error=error,
                requested=requested,
                effective=effective,
                metadata={"phase": "validation"},
            )
        assert tool is not None
        try:
            request = self._permission_request(call, tool, arguments, execution_context)
            permission = await self._authorize_async(request, execution_context)
            if permission is PermissionDecision.DENY:
                return self._failure(
                    call=call,
                    context=execution_context,
                    started=started,
                    error=f"权限策略拒绝工具 {call.name!r}",
                    status=ToolStatus.DENIED,
                    permission=permission,
                    requested=requested,
                    effective=effective,
                    metadata={"capability": tool.capability.value},
                )

            async def invoke() -> Any:
                if inspect.iscoroutinefunction(
                    tool.call
                ) or inspect.iscoroutinefunction(tool.invoke):
                    outcome = tool.invoke(
                        arguments,
                        execution_context,
                        **kwargs,
                    )
                else:
                    outcome = await asyncio.to_thread(
                        tool.invoke,
                        arguments,
                        execution_context,
                        **kwargs,
                    )
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                return outcome

            # 大量业务工具仍是同步文件/数据库/SDK 调用。直接在这里 call 会冻结
            # 整个 event loop，使并发 Agent、心跳和取消全部停住。明确的 async
            # 实现直接调用；同步实现卸载到线程。同步函数若动态返回 awaitable，
            # 回到事件循环后仍会正确等待。
            invoke_is_async = inspect.iscoroutinefunction(
                tool.call
            ) or inspect.iscoroutinefunction(tool.invoke)
            if not tool.detachable_on_timeout and not invoke_is_async:
                # shield 防止 asyncio 取消只切断 await、却留下仍在写入的后台线程。
                # 收到取消后先等待真实调用闭合，再由 Agent 记录 cancelled 并传播。
                task = asyncio.create_task(invoke())
                try:
                    outcome = await asyncio.shield(task)
                except asyncio.CancelledError as cancelled:
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            # 外层可以重复 cancel；终态仍要等真实同步调用闭合。
                            continue
                        except Exception:
                            break
                    if task.done():
                        try:
                            task.result()
                        except (Exception, asyncio.CancelledError):
                            pass
                    raise cancelled
            elif tool.timeout_s is None:
                outcome = await invoke()
            else:
                outcome = await asyncio.wait_for(invoke(), timeout=tool.timeout_s)
            return self._success(
                call=call,
                tool=tool,
                context=execution_context,
                started=started,
                permission=permission,
                requested=requested,
                effective=effective,
                outcome=outcome,
            )
        except (asyncio.TimeoutError, ToolTimeoutError) as exc:
            timeout = tool.timeout_s
            return self._failure(
                call=call,
                context=execution_context,
                started=started,
                error=(
                    str(exc)
                    if isinstance(exc, ToolTimeoutError) and str(exc)
                    else (
                        f"工具执行超过 {timeout:g} 秒"
                        if timeout is not None
                        else "工具执行超时"
                    )
                ),
                status=ToolStatus.TIMEOUT,
                permission=locals().get("permission", PermissionDecision.ALLOW),
                requested=requested,
                effective=effective,
                metadata={
                    "timeout_s": timeout,
                    "soft_timeout": tool.detachable_on_timeout,
                },
            )
        except Exception as exc:
            return self._failure(
                call=call,
                context=execution_context,
                started=started,
                error=f"{type(exc).__name__}: {exc}",
                permission=locals().get("permission", PermissionDecision.ALLOW),
                requested=requested,
                effective=effective,
            )


__all__ = [
    "ApprovalHandler",
    "BaseTool",
    "PermissionDecision",
    "ToolCall",
    "ToolCapability",
    "ToolChoice",
    "ToolBox",
    "ToolExecutionContext",
    "ToolOutput",
    "ToolParameter",
    "ToolPermissionRequest",
    "ToolPolicy",
    "ToolResult",
    "ToolSpec",
    "ToolStatus",
    "ToolTimeoutError",
    "build_parameters_schema",
]

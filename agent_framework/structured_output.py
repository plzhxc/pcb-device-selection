"""结构化输出：区块提取、JSON 解析与 Schema 校验。

模型按提示把结果包在命名区块里（例如 ``<<<DEVICES_START>>> ... <<<DEVICES_END>>>``），
本模块负责取出来、解析成 JSON、按 Schema 校验。

设计立场：**容错，但留痕。**

模型输出总带瑕疵——套一层 markdown 围栏、标记大小写不一致、JSON 尾随逗号、
中文全角标点。为这些小毛病判失败并重试，是在浪费 token 和时间。因此提取与解析
都采用分层降级：严格匹配失败就退到更宽松的策略，直到拿到可用结果。

但容错不能是黑箱：每一步修复都记录在 :attr:`ParseResult.repairs` 里。否则
"提示词其实一直没生效、全靠修复兜底"这种问题会被永久掩盖。

修复只在**字符串字面量之外**生效。像 ``{"desc": "温度：25度"}`` 里的全角冒号属于
正文内容，一旦被"修"成半角就破坏了数据。

失败以 :class:`OutputError` 返回，不写日志、不碰 Agent 记忆——由 agent 层决定
是记录、重试，还是把 :meth:`OutputError.correction_prompt` 发回给模型自我纠正。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from contextvars import ContextVar
from typing import Any, Literal, Sequence


# --------------------------------------------------------------------------- #
# 文本清理
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\s*\n?(.*?)```", re.DOTALL)

#: 全角 → 半角。仅用于 JSON 结构字符，且只在字符串字面量之外替换。
_FULLWIDTH = {
    "：": ":",
    "，": ",",
    "｛": "{",
    "｝": "}",
    "［": "[",
    "］": "]",
    "“": '"',
    "”": '"',
}


def strip_code_fences(text: str) -> tuple[str, bool]:
    """剥掉 markdown 代码围栏；返回 ``(内容, 是否发生剥离)``。"""

    match = _FENCE_RE.search(text)
    if match and match.group(1).strip():
        return match.group(1).strip(), True
    return text, False


def _walk_outside_strings(text: str):
    """逐字符遍历，产出 ``(下标, 字符, 是否在字符串字面量内)``。

    JSON 修复必须区分结构与内容：字符串里的逗号、冒号、括号都是数据的一部分，
    改动它们就是损坏数据。
    """

    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            yield index, char, True
            continue
        if char == '"':
            in_string = True
            yield index, char, True
            continue
        yield index, char, False


def find_json_span(text: str) -> str | None:
    """在自由文本中找出第一个括号配平的 JSON 片段。

    用于模型忘记输出标记、但确实给了 JSON 的情况。字符串内的括号会被跳过，
    因此 ``{"note": "}"}`` 这类内容不会导致误判。
    """

    start = None
    opener = None
    depth = 0
    for index, char, in_string in _walk_outside_strings(text):
        if in_string:
            continue
        if start is None:
            if char in "{[":
                start, opener, depth = index, char, 1
            continue
        if char == opener:
            depth += 1
        elif char == ("}" if opener == "{" else "]"):
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


# --------------------------------------------------------------------------- #
# JSON 修复
# --------------------------------------------------------------------------- #
def _drop_comments(text: str) -> str:
    """去掉 ``//`` 行注释与 ``/* */`` 块注释（字符串外）。

    自己扫而不是复用 :func:`_walk_outside_strings`：那个生成器会把注释里的字符
    也一起走一遍，注释里出现奇数个引号（``// 他说 "好``）就会把"当前在字符串内"
    的状态翻反，之后真正的结构字符全被当成字符串内容——后面的注释删不掉，标点
    也不再归一，而且一路无声。
    """

    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    escaped = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length:
            nxt = text[index + 1]
            if nxt == "/":
                end = text.find("\n", index)
                index = length if end == -1 else end
                continue
            if nxt == "*":
                end = text.find("*/", index)
                index = length if end == -1 else end + 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _normalize_punctuation(text: str) -> str:
    """把结构位置上的全角标点换成半角（字符串外）。"""

    return "".join(
        _FULLWIDTH.get(char, char) if not in_string else char
        for _, char, in_string in _walk_outside_strings(text)
    )


def _drop_trailing_commas(text: str) -> str:
    """去掉 ``,}`` / ``,]`` 里多余的逗号（字符串外）。"""

    chars = list(text)
    drop: set[int] = set()
    pending: int | None = None
    for index, char, in_string in _walk_outside_strings(text):
        if in_string:
            pending = None
            continue
        if char == ",":
            pending = index
        elif char in "}]":
            if pending is not None:
                drop.add(pending)
            pending = None
        elif not char.isspace():
            pending = None
    return "".join(c for i, c in enumerate(chars) if i not in drop)


def _fix_single_quotes(text: str) -> str:
    """整体把单引号换成双引号。

    仅在完全没有双引号时才尝试——否则无法可靠区分"用作定界符的单引号"和
    "英文缩写里的撇号"，贸然替换会损坏内容。
    """

    if '"' in text or "'" not in text:
        return text
    return text.replace("'", '"')


def _fix_escapes(text: str) -> str:
    r"""把非法反斜杠转义补成合法形式。

    只处理**非法**转义：合法的 ``\\`` ``\n`` ``\uXXXX`` 一律不动。曾经这里还有
    第二条"三个反斜杠压成两个"的正则，它会连带咬坏 ``C:\\bin`` 这类合法的双反斜杠
    （``\\`` + ``\b`` 被 json 解释成"反斜杠 + 退格符"），而且改坏之后照样解析成功、
    Schema 照样通过——属于静默的数据损坏，因此移除。

    扫描时跳过已配对的合法转义，避免把 ``\\`` 后面的字符误判成新的转义起点。
    """

    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        nxt = text[index + 1] if index + 1 < length else ""
        if nxt in ('"', "\\", "/", "b", "f", "n", "r", "t"):
            out.append(char)
            out.append(nxt)
            index += 2          # 合法转义，整体跳过
            continue
        if nxt == "u" and re.match(r"[0-9a-fA-F]{4}", text[index + 2 : index + 6] or ""):
            out.append(text[index : index + 6])
            index += 6
            continue
        out.append("\\\\")       # 非法转义，补成字面反斜杠
        index += 1
    return "".join(out)


#: 依次尝试的修复手段；顺序即从最安全到最激进。
#:
#: ``single_to_double_quotes`` 必须排在 ``normalize_punctuation`` **之前**：
#: 字符串边界识别只认双引号，单引号 JSON 会让全文都被判为"字符串外"，于是值里
#: 的全角标点（``输入电压：5V``）也被一并归一——先转成双引号，边界识别才准。
_REPAIRS: tuple[tuple[str, Any], ...] = (
    ("strip_fences", lambda t: strip_code_fences(t)[0]),
    ("drop_comments", _drop_comments),
    ("single_to_double_quotes", _fix_single_quotes),
    ("normalize_punctuation", _normalize_punctuation),
    ("drop_trailing_commas", _drop_trailing_commas),
    ("fix_escapes", _fix_escapes),
)


_last_json_error: ContextVar[str] = ContextVar("last_json_error", default="")


def _record_json_error(error: BaseException) -> None:
    _last_json_error.set(str(error))


def last_json_error() -> str:
    """最近一次 JSON 解析失败的具体原因（位置 + 症状）。

    用 ContextVar 而不是全局变量：并发跑多个 Agent 时，各自读到的必须是自己
    那次的错误。
    """

    return _last_json_error.get()


def loads_tolerant(value: Any) -> tuple[Any, list[str]]:
    """解析模型产出的 JSON，逐步施加修复。

    返回 ``(结果, 已施加的修复列表)``；无法解析时结果为 ``None``。
    每成功一步就立即返回，避免过度修复把原本正确的内容改坏。

    失败原因见 :func:`last_json_error`——"不是合法 JSON"这句话对人和模型都没有
    用，"第 12 行第 3 列缺少逗号"才能让模型改对、让人一眼看出是截断还是格式跑偏。
    """

    if not isinstance(value, str):
        try:
            return json.loads(value), []
        except (json.JSONDecodeError, TypeError) as error:
            _record_json_error(error)
            return None, []

    try:
        return json.loads(value), []
    except json.JSONDecodeError as error:
        _record_json_error(error)

    applied: list[str] = []
    current = value
    for name, repair in _REPAIRS:
        candidate = repair(current)
        if candidate == current:
            continue
        current = candidate
        applied.append(name)
        try:
            return json.loads(current), applied
        except json.JSONDecodeError as error:
            _record_json_error(error)
            continue

    # 全部修复仍不行：可能前后混着说明文字，最后试一次提取配平片段
    span = find_json_span(current)
    if span is not None and span != current:
        try:
            return json.loads(span), [*applied, "extract_json_span"]
        except json.JSONDecodeError as error:
            _record_json_error(error)
    return None, applied


def safe_json_loads(value: Any) -> Any:
    """:func:`loads_tolerant` 的简化版，只要结果。"""

    return loads_tolerant(value)[0]


# --------------------------------------------------------------------------- #
# 区块提取
# --------------------------------------------------------------------------- #
def extract_text_block(pattern: str, text: str | None) -> str | None:
    """按正则取出第一个捕获组；未命中或内容为空时返回 ``None``。"""

    if text is None:
        return None
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return None
    return match.group(1).strip() or None


def extract_tolerant(
    pattern: str,
    text: str | None,
    *,
    expect_json: bool,
    allow_whole_text: bool = True,
) -> tuple[str | None, list[str]]:
    """分层降级地提取区块，返回 ``(内容, 修复记录)``。

    降级顺序：严格匹配 → 忽略大小写 → 剥掉围栏后重试 → （期待 JSON 时）
    直接在全文里找配平的 JSON 片段。

    最后一层专治"模型压根忘了输出标记，但 JSON 本身是对的"——这种情况下重试
    一次通常还是忘，不如直接把结果取出来。

    ``allow_whole_text=False`` 时禁用这最后一层。**声明了多项输出时必须禁用**：
    全文兜底只会返回第一个配平 JSON，把它塞给标记缺失的那个键，等于张冠李戴，
    而且两项 Schema 可能都恰好通过、``ok=True``，错误彻底静默。
    """

    if not text:
        return None, []

    strict = extract_text_block(pattern, text)
    if strict is not None:
        return strict, []

    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if match and match.group(1).strip():
        return match.group(1).strip(), ["ignore_case_marker"]

    # Prefer a valid JSON span from the original answer before inspecting
    # Markdown fences.  A JSON string may itself contain fenced Markdown
    # (notably a Mermaid diagram); stripping that inner fence first turns a
    # node such as ``BAT[LiPo]`` into the apparent output block and guarantees
    # a false ``invalid_json`` correction loop.
    if expect_json and allow_whole_text:
        original_span = find_json_span(text)
        if original_span is not None:
            parsed_span, _ = loads_tolerant(original_span)
            if parsed_span is not None:
                return original_span, ["marker_missing_used_json_span"]

    unfenced, stripped = strip_code_fences(text)
    if stripped:
        inner = extract_text_block(pattern, unfenced)
        if inner is not None:
            return inner, ["strip_fences"]

    if not allow_whole_text:
        return None, []

    if expect_json:
        span = find_json_span(unfenced if stripped else text)
        if span is not None:
            repairs = ["strip_fences"] if stripped else []
            return span, [*repairs, "marker_missing_used_json_span"]
    elif stripped and unfenced.strip():
        # 无 schema 的输出（Markdown 等）：围栏内容本身就是想要的正文
        return unfenced.strip(), ["strip_fences", "marker_missing_used_fence"]

    return None, []


# --------------------------------------------------------------------------- #
# 声明与结果
# --------------------------------------------------------------------------- #
#: ``schema_broken`` 是唯一一个"错在我们"的取值：Schema 定义本身非法，
#: 重试无意义，调用方应当直接终止而不是发回给模型改。
ErrorKind = Literal[
    "empty_answer", "missing_block", "invalid_json", "schema_invalid", "schema_broken"
]


@dataclass(frozen=True)
class StructuredOutput:
    """一项要从模型回答中提取的输出。

    Attributes:
        name: 结果字典里的键名。
        extract_pattern: 定位区块的正则，必须含一个捕获组。
        schema_def: JSON Schema；为 ``None`` 时保留提取到的原始文本（适合
            Markdown 等非 JSON 结果）。
        required: 缺失时是否判为错误。
    """

    name: str
    extract_pattern: str
    schema_def: dict[str, Any] | None = None
    required: bool = True


@dataclass(frozen=True)
class OutputError:
    """一次提取/校验失败。"""

    kind: ErrorKind
    message: str
    output_name: str | None = None
    extract_pattern: str | None = None

    def correction_prompt(self) -> str:
        """产出可直接发回给模型的纠错文本。"""

        if self.kind == "missing_block":
            return (
                f"提取内容失败，请确保输出中包含可被 {self.extract_pattern} "
                "匹配的区块，并确保区块内容符合 JSON Schema 要求。"
            )
        if self.kind == "schema_broken":
            # 这条不该发给模型；真发出去也只能让它更糊涂。
            return f"输出声明的 JSON Schema 本身有问题，需要修代码：{self.message}"
        if self.kind in ("invalid_json", "schema_invalid"):
            return (
                "JSON Schema 校验失败，请严格按照 JSON Schema 输出。"
                f"错误信息：{self.message}"
            )
        return self.message


@dataclass(frozen=True)
class ParseResult:
    """提取结果集合。

    Attributes:
        values: 各输出的解析结果；只在完全成功时有意义。
        errors: 提取或校验错误。
        repairs: 为拿到结果而施加的修复，形如 ``{"data": ["strip_fences"]}``。
            持续出现同一类修复，说明提示词该改了——容错不该长期替提示词兜底。
    """

    values: dict[str, Any] = field(default_factory=dict)
    errors: list[OutputError] = field(default_factory=list)
    repairs: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def repaired(self) -> bool:
        return any(self.repairs.values())

    def correction_prompts(self) -> list[str]:
        return [error.correction_prompt() for error in self.errors]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _validate_schema(instance: Any, schema: dict[str, Any]) -> tuple[str | None, bool]:
    """按 Schema 校验。

    Returns:
        ``(错误描述, 是否为 Schema 本身写错了)``。校验通过时为 ``(None, False)``。

        两者必须分开：Schema 写错是**我们的 bug**，模型再输出几遍也过不了。
        混为一谈的话，一个手滑写坏的 schema 会让每次运行都白跑满纠错轮数，最后
        以"模型输出格式错误"收场——排查方向从一开始就是错的。
    """

    from jsonschema import validate  # 延迟导入：无结构化输出时不需要这个依赖
    from jsonschema.exceptions import SchemaError, ValidationError

    try:
        validate(instance=instance, schema=schema)
        return None, False
    except ValidationError as error:
        return str(error), False
    except SchemaError as error:
        return f"JSON Schema 定义本身非法：{error}", True
    except Exception as error:  # jsonschema 之外的意外故障同样不该甩锅给模型
        return f"Schema 校验器执行失败：{type(error).__name__}: {error}", True


def parse_structured_outputs(
    answer: str | None,
    outputs: Sequence[StructuredOutput],
) -> ParseResult:
    """从模型回答中提取并校验所有声明的输出。

    未声明任何输出时，原样返回文本（裸文本 Agent 不需要包装）。
    可选输出缺失不算错误；必选输出缺失、空回答、JSON 或 Schema 错误都算。
    """

    if not outputs:
        return ParseResult(values={"answer": answer} if answer is not None else {})

    if not answer or not str(answer).strip():
        return ParseResult(
            errors=[
                OutputError(
                    kind="empty_answer",
                    message="模型返回空响应（可能为服务瞬时错误或提示过长）",
                )
            ]
        )

    values: dict[str, Any] = {}
    errors: list[OutputError] = []
    repairs: dict[str, list[str]] = {}

    for output in outputs:
        extracted, extract_repairs = extract_tolerant(
            output.extract_pattern,
            answer,
            expect_json=output.schema_def is not None,
            allow_whole_text=len(outputs) == 1,
        )
        if extracted is None:
            if output.required:
                errors.append(
                    OutputError(
                        kind="missing_block",
                        message=f"未找到输出区块 {output.name!r}",
                        output_name=output.name,
                        extract_pattern=output.extract_pattern,
                    )
                )
            continue

        if output.schema_def is None:
            values[output.name] = extracted
            if extract_repairs:
                repairs[output.name] = extract_repairs
            continue

        parsed, parse_repairs = loads_tolerant(extracted)
        applied = [*extract_repairs, *parse_repairs]
        if parsed is None:
            detail = last_json_error()
            errors.append(
                OutputError(
                    kind="invalid_json",
                    message=(
                        f"区块 {output.name!r} 不是合法 JSON"
                        + (f"：{detail}" if detail else "")
                    ),
                    output_name=output.name,
                    extract_pattern=output.extract_pattern,
                )
            )
            continue

        schema_error, is_our_bug = _validate_schema(parsed, output.schema_def)
        if schema_error is not None:
            errors.append(
                OutputError(
                    kind="schema_broken" if is_our_bug else "schema_invalid",
                    message=schema_error,
                    output_name=output.name,
                    extract_pattern=output.extract_pattern,
                )
            )
            continue

        values[output.name] = parsed
        if applied:
            repairs[output.name] = applied

    return ParseResult(values=values, errors=errors, repairs=repairs)


__all__ = [
    "ErrorKind",
    "OutputError",
    "ParseResult",
    "StructuredOutput",
    "extract_text_block",
    "extract_tolerant",
    "find_json_span",
    "last_json_error",
    "loads_tolerant",
    "parse_structured_outputs",
    "safe_json_loads",
    "strip_code_fences",
]

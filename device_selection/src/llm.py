"""大模型辅助理解（可选，非核心）。

定位很明确：**只做"把用户的话翻译成结构化意图"这一件事**。

* 规则能识别的句子，轮不到它（调用顺序是 规则 → 大模型 → 兜底摘要）；
* 它不能产出器件型号、不能判定满足与否、不能碰数据库；
* 它的输出必须过白名单校验，字段名、比较符、取值任何一项不合规就整条丢弃，
  退回规则兜底——宁可答不上来，也不能让它改错东西。

配置读 `workspace/PCB_UI/llm.local.env`（`QWEN_BASE_URL` / `QWEN_API_KEY` /
`QWEN_MODEL`），也可以用环境变量覆盖。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .evaluate import ALL_OPS
from .units import parse_interval, parse_quantity

DEFAULT_ENV_FILE = Path("/share/home/zhengzhiqi/workspace/PCB_UI/llm.local.env")

# 允许被改的字段白名单。范围之外的字段一律拒绝，防止大模型凭空发明字段名。
FIELD_WHITELIST = (
    "input_voltage_range",
    "output_voltage",
    "output_current",
    "switching_frequency",
    "operating_temperature",
    "package",
    "topology",
    "quiescent_current_iq",
    "dropout_voltage",
    "channel_count",
    "resolution",
)

KINDS = ("set_condition", "explain", "search", "decision", "summarize", "unknown")

_SYSTEM = """你在为一个器件选型工具做“意图翻译”。只输出 JSON，不要解释、不要代码块。

可用意图：
1. 改条件：{"kind":"set_condition","item_id":"<选型项编号>","field":"<字段>","op":"<比较符>","value":<数值或字符串>,"unit":"<单位,可空>","target":"hard|preferred"}
2. 解释候选：{"kind":"explain","model":"<型号>"}
3. 查器件：{"kind":"search","query":"<型号或关键词>"}
4. 记录决定：{"kind":"decision","model":"<型号>","decision":"keep|exclude|discuss|confirmed"}
5. 总结清单：{"kind":"summarize"}
6. 无法归类：{"kind":"unknown"}

字段只能用这些：%s
比较符只能用这些：%s
单位用 SI 写法（V、A、Hz、°C、mA、kHz…），没有单位就留空字符串。
item_id 必须是用户上下文里列出的编号之一。

注意：用户说“输出电流改成 800mA”这类话时，op 用 ">="（能力下限），不是相等；
说“输出电压改成 3.3V”时 op 用 "=="（设定值）。
""" % (", ".join(FIELD_WHITELIST), ", ".join(ALL_OPS))


def load_env(path: Path | str = DEFAULT_ENV_FILE) -> dict[str, str]:
    """读配置文件，环境变量优先。"""

    values: dict[str, str] = {}
    target = Path(path)
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    for key in ("QWEN_BASE_URL", "QWEN_API_KEY", "QWEN_MODEL"):
        if os.environ.get(key):
            values[key] = str(os.environ[key])
    return values


class LLMUnavailable(RuntimeError):
    """没有配置或连不上大模型。"""


class LLMTranslator:
    """把自然语言翻译成结构化意图。失败一律返回 None，由调用方退回规则。"""

    def __init__(
        self,
        env_file: Path | str = DEFAULT_ENV_FILE,
        *,
        enabled: bool | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.env = load_env(env_file)
        self.timeout = timeout
        mode = str(os.environ.get("DEVICE_SELECTION_LLM", "")).strip().lower()
        if enabled is None:
            enabled = mode != "off"
        self.enabled = bool(enabled) and bool(
            self.env.get("QWEN_BASE_URL") and self.env.get("QWEN_API_KEY")
        )
        self.model = self.env.get("QWEN_MODEL", "")
        self.last_error = ""

    # ------------------------------------------------------------------ 状态
    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "base_url": self.env.get("QWEN_BASE_URL", ""),
            "env_file": str(DEFAULT_ENV_FILE),
            "role": "仅做意图翻译；检索与判定仍由规则完成",
        }

    def available(self) -> bool:
        return self.enabled

    # ------------------------------------------------------------------ 调用
    def _client(self):
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMUnavailable("openai SDK 未安装") from exc
        return OpenAI(
            base_url=self.env["QWEN_BASE_URL"],
            api_key=self.env["QWEN_API_KEY"],
            timeout=self.timeout,
        )

    def translate(
        self,
        message: str,
        *,
        items: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any] | None:
        """返回通过校验的意图，或 None（不可用 / 解析失败 / 校验不过）。"""

        if not self.enabled:
            return None
        text = str(message or "").strip()
        if not text:
            return None

        listing = "\n".join(
            f"- {item.get('id')}：{item.get('name') or ''}"
            f"（当前条件：{item.get('conditions') or '无'}）"
            for item in items
        )
        try:
            client = self._client()
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {
                        "role": "user",
                        "content": f"当前选型项：\n{listing or '（无）'}\n\n用户说：{text}",
                    },
                ],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=300,
            )
            raw = response.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001 - 任何失败都退回规则
            self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            return None

        intent = _parse(raw)
        if intent is None:
            self.last_error = f"输出不是合法 JSON：{raw[:120]}"
            return None
        validated, why = validate(intent, items)
        if validated is None:
            self.last_error = why
            return None
        validated["_via"] = "llm"
        return validated


def _parse(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def validate(
    intent: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any] | None, str]:
    """白名单校验。返回 (通过的意图, 拒绝原因)。"""

    kind = str(intent.get("kind") or "").strip()
    if kind not in KINDS:
        return None, f"未知意图 {kind!r}"
    if kind == "unknown":
        return None, "模型自己归类为 unknown"
    if kind == "summarize":
        return {"kind": "summarize"}, ""
    if kind == "search":
        query = str(intent.get("query") or "").strip()
        if not query:
            return None, "search 缺少 query"
        return {"kind": "search", "query": query}, ""
    if kind == "explain":
        model = str(intent.get("model") or "").strip()
        if not model:
            return None, "explain 缺少 model"
        return {"kind": "explain", "model": model}, ""
    if kind == "decision":
        model = str(intent.get("model") or "").strip()
        decision = str(intent.get("decision") or "").strip()
        if not model or decision not in ("keep", "exclude", "discuss", "confirmed"):
            return None, "decision 参数不合法"
        return {"kind": "decision", "model": model, "decision": decision}, ""

    # set_condition
    field = str(intent.get("field") or "").strip()
    if field not in FIELD_WHITELIST:
        return None, f"字段 {field!r} 不在白名单里"
    op = str(intent.get("op") or "").strip()
    if op not in ALL_OPS:
        return None, f"比较符 {op!r} 不合法"
    unit = str(intent.get("unit") or "").strip()
    value, unit, op, why = _normalize_value(op, intent.get("value"), unit)
    if value is None:
        return None, why
    item_id = str(intent.get("item_id") or "").strip()
    known = {str(i.get("id")) for i in items}
    if known and item_id not in known:
        return None, f"选型项编号 {item_id!r} 不在当前清单里"
    target = str(intent.get("target") or "preferred").strip()
    if target not in ("hard", "preferred"):
        target = "preferred"
    return {
        "kind": "set_condition",
        "item_id": item_id,
        "field": field,
        "op": op,
        "value": value,
        "unit": unit,
        "target": target,
    }, ""


def _normalize_value(
    op: str,
    raw: Any,
    unit: str,
) -> tuple[Any, str, str, str]:
    """把模型给的人类写法收敛成可比较的取值。

    模型经常返回 `"4.5V to 5.5V"`、`"800mA"` 这类字符串。**由本阶段的解析器
    来决定它们是什么意思**，解析不了才拒绝——这样判定的口径始终在我这边，
    而不是模型说了算。
    """

    if raw is None:
        return None, "", op, "缺少取值"
    if isinstance(raw, bool):
        return raw, unit, "is" if op in ("==", ">=") else op, ""
    if isinstance(raw, (int, float)):
        return float(raw), unit, op, ""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            low, high = float(raw[0]), float(raw[1])
        except (TypeError, ValueError):
            return None, "", op, f"取值不是数值区间：{raw!r}"
        return [min(low, high), max(low, high)], unit, "cover", ""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, "", op, "取值为空"
        interval = parse_interval(text)
        if interval is not None:
            return (
                [interval.low, interval.high],
                interval.unit or unit,
                "cover" if op in (">=", "<=", "==", "cover") else op,
                "",
            )
        quantity = parse_quantity(text)
        if quantity is not None:
            return (
                quantity.value,
                quantity.unit or unit,
                op if op not in ("cover",) else "cover",
                "",
            )
        # 纯文本取值（例如 package / topology）
        if op in ("is", "is_not", "contains", "not_contains", "in", "not_in"):
            return text, unit, op, ""
        return None, "", op, f"无法把 {text!r} 解析成可比较的取值"
    return None, "", op, f"取值形态不合法：{type(raw).__name__}"

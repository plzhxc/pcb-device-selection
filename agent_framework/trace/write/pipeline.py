"""观测管道：发事件、传归属、摘要入参。四层共用。

本模块是 trace 包里**唯一被框架核心依赖的部分**，因此刻意做成叶子——不 import
任何 writer、任何数据库、任何 agent/stage/workflow，只有标准库。这样 ``llm``、
``agent``、``stage``、``workflow`` 四层 import 它时不会拖进整个落库子系统，也不会
产生循环导入。

分工：本模块只管**把事件发出去**；谁接、落到哪张表，是同包内 writer/observer 的事。
观察者是注入的，因此换一套实现（OpenTelemetry、文件日志、测试替身）不用动框架。

观测**不能拖垮主流程**——写库超时、表结构对不上、观察者自己有 bug，都不该让
一次正常的模型调用失败。但"不影响主流程"不等于"当作没发生"：此前四层各写了
一份 ``except Exception: pass``，观察者要是整体挂了（数据库连不上、hook 名写错），
业务照跑，追踪表里一行没有，而且**没有任何信号**——上线后往往是几天后有人问
"怎么查不到记录"才发现。

因此这里统一成一处投递：吞掉异常，但按 (hook, 异常类型) 去重后打一条 warning
（首次带完整栈），并累计失败次数供自检读取。同类错误只吼一次，避免每次调用刷屏。
"""

from __future__ import annotations

from collections.abc import Mapping
import contextvars
from dataclasses import fields, is_dataclass
from enum import Enum
import logging
import os
import threading
from typing import Any

_logger = logging.getLogger("agent_framework.trace")

_lock = threading.Lock()
_failures: dict[str, int] = {}
_reported: set[tuple[str, str]] = set()


def emit(observer: Any, hook: str, event: Any) -> bool:
    """把 ``event`` 投递给 ``observer`` 的 ``hook`` 方法。

    Returns:
        投递是否成功。主流程可以忽略返回值；自检逻辑可以据此判断观测是否健在。
    """

    handler = getattr(observer, hook, None)
    if handler is None:
        _record(
            hook, "MissingHook", f"观察者 {type(observer).__name__} 没有 {hook} 方法"
        )
        return False
    try:
        handler(event)
    except Exception as exc:  # 观测失败不影响主流程，但要留痕
        _record(hook, type(exc).__name__, str(exc), exc_info=True)
        return False
    return True


def _record(hook: str, kind: str, detail: str, *, exc_info: bool = False) -> None:
    with _lock:
        _failures[hook] = _failures.get(hook, 0) + 1
        first = (hook, kind) not in _reported
        if first:
            _reported.add((hook, kind))
    if first:
        _logger.warning(
            "观测事件 %s 投递失败（%s：%s）；主流程继续，后续同类失败只计数不再打日志",
            hook,
            kind,
            detail,
            exc_info=exc_info,
        )


def digest(value: Any, limit: int | None = None) -> Any:
    """生成可写入 JSON 的完整输入快照，不做字符或集合截断。

    函数名为兼容既有调用保留；它现在做的是 lossless snapshot，而不是摘要。
    ``limit`` 也只为兼容旧调用签名保留并被忽略。Agent、Stage、Workflow 三层由此
    使用同一套完整记录规则，页面不会再出现 ``...(241 chars)`` 一类占位文本。
    """

    del limit
    return _snapshot(value, seen=set())


def _snapshot(value: Any, *, seen: set[int]) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _snapshot(value.value, seen=seen)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, os.PathLike):
        return os.fspath(value)

    identity = id(value)
    if identity in seen:
        # 循环对象无法表示为 JSON；明确标记循环，而不是静默裁掉剩余内容。
        return f"<recursive {type(value).__name__}>"

    if isinstance(value, Mapping):
        seen.add(identity)
        try:
            return {str(key): _snapshot(item, seen=seen) for key, item in value.items()}
        finally:
            seen.remove(identity)

    if isinstance(value, (list, tuple, set, frozenset)):
        seen.add(identity)
        try:
            return [_snapshot(item, seen=seen) for item in value]
        finally:
            seen.remove(identity)

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        seen.add(identity)
        try:
            try:
                dumped = model_dump(mode="json")
            except TypeError:
                dumped = model_dump()
            return _snapshot(dumped, seen=seen)
        finally:
            seen.remove(identity)

    if is_dataclass(value) and not isinstance(value, type):
        seen.add(identity)
        try:
            return {
                field.name: _snapshot(getattr(value, field.name), seen=seen)
                for field in fields(value)
            }
        finally:
            seen.remove(identity)

    # 自定义对象没有通用的 JSON 结构契约；保留完整 repr，绝不按长度裁剪。
    return repr(value)


def observer_failures() -> dict[str, int]:
    """各 hook 累计的投递失败次数；全空表示观测链路健康。"""

    with _lock:
        return dict(_failures)


def reset_observer_failures() -> None:
    """清空失败计数与去重记录（测试与长驻进程的周期自检用）。"""

    with _lock:
        _failures.clear()
        _reported.clear()


# --------------------------------------------------------------------------- #
# 跨层归属
# --------------------------------------------------------------------------- #
# 观测事件只带**本层**的 id：``StageEnded`` 不知道自己属于哪个 workflow，
# ``CallEnded`` 不知道是哪个 agent 发起的。跨层外键靠这三个 ContextVar 随执行流
# 自动传播，不必把 id 一层层塞进函数签名。
#
# 定义放在这里而不是各层的 observer 模块里：本模块是**叶子**，谁都可以依赖它，
# 反过来则会让本模块依赖 agent/stage/workflow，直接产生循环导入。
# 各层进入自己的作用域时 set、退出时 reset。
_current_run: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_current_run", default=None
)
_current_stage: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "stage_current", default=None
)
_current_workflow: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "workflow_current", default=None
)


def current_run_id() -> str | None:
    """当前 Agent 运行 id；不在运行内时为 ``None``。"""

    return _current_run.get()


def current_stage_id() -> str | None:
    """当前 stage 运行 id；不在 stage 内时为 ``None``。"""

    return _current_stage.get()


def current_workflow_id() -> str | None:
    """当前 workflow 运行 id；不在 workflow 内时为 ``None``。"""

    return _current_workflow.get()


__all__ = [
    "current_run_id",
    "current_stage_id",
    "current_workflow_id",
    "digest",
    "emit",
    "observer_failures",
    "reset_observer_failures",
]

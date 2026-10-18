"""Trace 写入侧的公共基础设施。

四层 Writer 共享三件事：

* :class:`TraceWriteDispatcher`：单线程 FIFO 后台队列，数据库 I/O 不阻塞
  Agent 所在的 asyncio 事件循环；
* :class:`BaseTraceWriter`：统一事务、回滚、连接释放、失败计数与事件序号；
* :func:`json_dump`：完整保存 payload，不截断字段。

``runtime.init()`` 会把同一个 dispatcher 注入四个 Writer，因此跨层事件也保持
提交顺序。直接实例化 Writer 时默认仍为同步模式，保留独立写入 API 的返回值语义。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Generic, Sequence, TypeVar, cast

from ..database import Dialect, TraceDatabase


_T = TypeVar("_T")
_SENTINEL = object()


def json_dump(value: Any) -> str:
    """序列化完整观测数据，不转义中文，也不裁剪 payload。"""

    return json.dumps(value, ensure_ascii=False, default=str)


class TraceWriterClosed(RuntimeError):
    """后台写入器已经停止接收新事件。"""


class TraceWriteFuture(Future[_T], Generic[_T]):
    """后台写入结果。

    Trace 事件一旦被接受就不能由调用方取消，否则会在内存队列里静默丢事件。
    """

    def cancel(self) -> bool:
        return False


@dataclass(slots=True)
class _WriteJob(Generic[_T]):
    operation: Callable[[], _T]
    future: TraceWriteFuture[_T]
    on_error: Callable[[Exception], None]


class TraceWriteDispatcher:
    """可靠的进程内 FIFO 数据库写入队列。

    队列刻意不设置容量上限：Trace 可以在数据库暂时变慢时施加内存背压，但不能
    因为队列满而静默删掉完整的请求、响应或工具 payload。运维侧可以通过
    :attr:`pending_writes` 监控积压。
    """

    def __init__(self, *, thread_name: str = "trace-db-writer") -> None:
        self._queue: queue.Queue[_WriteJob[Any] | object] = queue.Queue()
        self._condition = threading.Condition()
        self._accepting = True
        self._stop_sent = False
        self._submitted = 0
        self._completed = 0
        self._thread = threading.Thread(
            target=self._run,
            name=thread_name,
            daemon=True,
        )
        self._thread.start()

    def submit(
        self,
        operation: Callable[[], _T],
        *,
        on_error: Callable[[Exception], None],
    ) -> TraceWriteFuture[_T]:
        """按调用顺序接受一个写入；已关闭时明确报错，不静默丢弃。"""

        if not callable(operation):
            raise TypeError("operation must be callable")
        future: TraceWriteFuture[_T] = TraceWriteFuture()
        with self._condition:
            if not self._accepting:
                raise TraceWriterClosed("trace writer is shutting down")
            self._submitted += 1
            # Queue 无界，put 不会在业务线程上等待数据库或队列空位。
            self._queue.put(
                _WriteJob(
                    operation=operation,
                    future=future,
                    on_error=on_error,
                )
            )
        return future

    @property
    def pending_writes(self) -> int:
        with self._condition:
            return self._submitted - self._completed

    @property
    def accepting(self) -> bool:
        with self._condition:
            return self._accepting

    def flush(self, timeout: float | None = None) -> bool:
        """等待调用本方法前已提交的所有事件完成。"""

        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            target = self._submitted
            while self._completed < target:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def shutdown(self, timeout: float | None = None) -> bool:
        """停止接收新事件，排空队列，然后关闭工作线程。"""

        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            self._accepting = False

        remaining = (
            None if deadline is None else max(0.0, deadline - time.monotonic())
        )
        if not self.flush(remaining):
            return False

        with self._condition:
            if not self._stop_sent:
                self._stop_sent = True
                self._queue.put(_SENTINEL)

        remaining = (
            None if deadline is None else max(0.0, deadline - time.monotonic())
        )
        self._thread.join(remaining)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            queued = self._queue.get()
            try:
                if queued is _SENTINEL:
                    return
                job = cast(_WriteJob[Any], queued)
                job.future.set_running_or_notify_cancel()
                try:
                    result = job.operation()
                except Exception as error:
                    try:
                        job.on_error(error)
                    except Exception:
                        pass
                    job.future.set_exception(error)
                else:
                    job.future.set_result(result)
                finally:
                    with self._condition:
                        self._completed += 1
                        self._condition.notify_all()
            finally:
                self._queue.task_done()


class BaseTraceWriter:
    """所有具体 Trace Writer 的基类。

    Args:
        connect_factory: 零参 DB-API 连接工厂。
        dialect: ``sqlite`` 或 ``mysql``。
        ensure_schema: 是否在构造时初始化八张 Trace 表。
        dispatcher: 可选的共享后台队列。传入后所有写入立即入队；不传则同步执行。
        background: 单独使用 Writer 时是否为它创建专属后台队列。Runtime 应传入
            一个共享 ``dispatcher``，不要为四层分别设置此项。
    """

    def __init__(
        self,
        connect_factory: Any,
        *,
        dialect: Dialect,
        ensure_schema: bool = True,
        dispatcher: TraceWriteDispatcher | None = None,
        background: bool = False,
    ) -> None:
        if not callable(connect_factory):
            raise TypeError("connect_factory must be callable")
        if dispatcher is not None and background:
            raise ValueError("pass dispatcher or background=True, not both")
        self._db = TraceDatabase(connect_factory, dialect=dialect)
        # 兼容旧 Writer 内部命名；新代码统一使用 _db。
        self._database = self._db
        self._state_lock = threading.Lock()
        self._seq: dict[str, int] = {}
        self._failed_writes = 0
        self._last_error: Exception | None = None
        self._dispatcher = (
            dispatcher
            if dispatcher is not None
            else (TraceWriteDispatcher() if background else None)
        )
        self._owns_dispatcher = dispatcher is None and background
        if ensure_schema:
            self.ensure_schema()

    @property
    def failed_writes(self) -> int:
        with self._state_lock:
            return self._failed_writes

    @property
    def last_error(self) -> Exception | None:
        with self._state_lock:
            return self._last_error

    @property
    def pending_writes(self) -> int:
        dispatcher = self._dispatcher
        return dispatcher.pending_writes if dispatcher is not None else 0

    @property
    def background(self) -> bool:
        return self._dispatcher is not None

    def ensure_schema(self) -> None:
        self._db.ensure_schema()

    # ------------------------------ 序号 ------------------------------ #
    def _next_seq(self, key: str) -> int:
        with self._state_lock:
            seq = self._seq.get(key, 0) + 1
            self._seq[key] = seq
            return seq

    def _sequence_known(self, key: str) -> bool:
        """本进程是否仍在维护该运行的实时事件序号。"""

        with self._state_lock:
            return key in self._seq

    def _forget(self, key: str) -> None:
        with self._state_lock:
            self._seq.pop(key, None)

    # ------------------------------ 事务 ------------------------------ #
    def _transaction(self, operation: Callable[[Any], _T]) -> _T:
        """在一个短事务中执行 operation(cursor)，异常时统一回滚。"""

        connection = cursor = None
        try:
            connection = self._db.connect()
            cursor = connection.cursor()
            self._db.begin(connection, cursor)
            result = operation(cursor)
            self._db.commit(connection, cursor)
            return result
        except Exception:
            if connection is not None and cursor is not None:
                try:
                    self._db.rollback(connection, cursor)
                except Exception:
                    pass
            raise
        finally:
            for handle in (cursor, connection):
                try:
                    if handle is not None:
                        handle.close()
                except Exception:
                    pass

    def _record_failure(self, error: Exception) -> None:
        with self._state_lock:
            self._failed_writes += 1
            self._last_error = error

    def _submit(
        self,
        operation: Callable[[], _T],
        *,
        suppress_errors: bool = False,
        failure_result: Any = None,
    ) -> _T | TraceWriteFuture[_T] | Any:
        """同步执行或提交到后台队列，所有失败都在 Base 中统一计数。"""

        dispatcher = self._dispatcher
        if dispatcher is not None:
            try:
                return dispatcher.submit(
                    operation,
                    on_error=self._record_failure,
                )
            except Exception as error:
                self._record_failure(error)
                if suppress_errors:
                    return failure_result
                raise

        try:
            return operation()
        except Exception as error:
            self._record_failure(error)
            if suppress_errors:
                return failure_result
            raise

    def _write(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> bool:
        """把多条 SQL 作为一个事务写入。

        后台模式返回的是“已可靠入队”；实际数据库失败通过 ``failed_writes`` /
        ``last_error`` 暴露，并可用 :meth:`flush` 等待完成。
        """

        frozen = tuple((sql, tuple(parameters)) for sql, parameters in statements)

        def operation() -> bool:
            def execute(cursor: Any) -> bool:
                for sql, parameters in frozen:
                    self._db.execute(cursor, sql, parameters)
                return True

            return self._transaction(execute)

        result = self._submit(
            operation,
            suppress_errors=True,
            failure_result=False,
        )
        if isinstance(result, Future):
            return True
        return bool(result)

    # ------------------------------ 生命周期 ------------------------------ #
    def flush(self, timeout: float | None = None) -> bool:
        dispatcher = self._dispatcher
        return True if dispatcher is None else dispatcher.flush(timeout)

    def shutdown(self, timeout: float | None = None) -> bool:
        dispatcher = self._dispatcher
        if dispatcher is None:
            return True
        if self._owns_dispatcher:
            return dispatcher.shutdown(timeout)
        return dispatcher.flush(timeout)

    def timestamp(self, moment: Any = None) -> Any:
        return self._db.timestamp(moment) if moment is not None else self._db.timestamp()


__all__ = [
    "BaseTraceWriter",
    "TraceWriteDispatcher",
    "TraceWriteFuture",
    "TraceWriterClosed",
    "json_dump",
]

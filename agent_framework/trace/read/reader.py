"""只读查询实现。

设计上只有三条规矩：

1. **只读**：本模块不出现 INSERT / UPDATE / DELETE，也不 import 任何 writer。
   连只读副本也能跑。
2. **参数化**：所有条件走占位符，绝不拼字符串——可视化的过滤条件直接来自 URL，
   拼字符串就是把注入口开在最外层。
3. **不做业务判断**：查出来什么返回什么。哪条算"慢"、哪条要标红，是前端的事；
   放在这里会让同一份数据在不同页面口径不一。

方言差异只有占位符一处（sqlite 用 ``?``，mysql 用 ``%s``），由
:class:`~..database.TraceDatabase` 统一。
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from ..database import Dialect, TraceDatabase


def _loads(value: Any, default: Any) -> Any:
    """JSON 列取值；坏数据不该让整个页面挂掉。"""

    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _limit(value: int, *, default: int = 50, maximum: int = 500) -> int:
    """把 URL 传入的分页大小限制在适合控制台查询的范围内。"""

    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(resolved, maximum))


def _stage_io(row: dict[str, Any]) -> None:
    """读取新旧 Stage 行为统一的“开始输入 / 结束输出”视图。

    新记录只写 ``inputs_json``；历史记录可能把一部分输入存在
    ``upstream_json``。读取时无损合并，API 和页面都不再暴露第二套输入字段。
    """

    inputs = _loads(row.pop("inputs_json", None), {})
    legacy_upstream = _loads(row.pop("upstream_json", None), {})
    if not isinstance(inputs, dict):
        inputs = {"value": inputs}
    if isinstance(legacy_upstream, dict):
        for raw_name, value in legacy_upstream.items():
            name = str(raw_name)
            resolved = name
            suffix = 1
            while resolved in inputs:
                suffix_text = "" if suffix == 1 else f"_{suffix}"
                resolved = f"{name}#stage_output{suffix_text}"
                suffix += 1
            inputs[resolved] = value
    row["inputs"] = inputs
    row["outputs"] = _loads(row.pop("outputs_json", None), {})
    row["output_keys"] = _loads(row.pop("output_keys_json", None), [])
    row["dag"] = _loads(row.pop("dag_json", None), {})


def _attach_stage_dag(
    stage: dict[str, Any],
    steps: Sequence[dict[str, Any]],
) -> None:
    """Read the Agent DAG returned by Stage and attach Agent run summaries.

    A missing DAG means that the historical Stage predates the DAG contract.
    It is deliberately *not* reconstructed from ``seq`` or timestamps: doing
    so would turn observation order into a false dependency graph.
    """

    persisted = stage.get("dag")
    if not isinstance(persisted, dict) or not isinstance(
        persisted.get("nodes"), list
    ):
        stage["dag"] = {
            "available": False,
            "reason": "stage_dag_not_recorded",
            "version": None,
            "stage_name": stage.get("name") or "",
            "nodes": [],
            "edges": [],
            "metadata": {"actual": False},
        }
        return

    by_step_id = {
        str(step.get("step_id")): step
        for step in steps
        if step.get("step_id")
    }
    by_run_id = {
        str(run.get("agent_run_id")): run
        for step in steps
        for run in (step.get("runs") or [])
        if isinstance(run, dict) and run.get("agent_run_id")
    }
    nodes: list[dict[str, Any]] = []
    for raw_node in persisted.get("nodes") or []:
        if not isinstance(raw_node, dict):
            continue
        node = dict(raw_node)
        node_type = str(node.get("type") or "")
        if node_type == "step":
            # Read compatibility for the short-lived Step-DAG format. No edge
            # projection or ordering is performed here.
            step = by_step_id.get(str(node.get("step_id") or ""))
            if step is None:
                node["execution"] = {
                    "missing": True,
                    "step_id": node.get("step_id") or "",
                }
                node["agent_runs"] = []
            else:
                # The step table is the authoritative execution record; the
                # DAG snapshot supplies topology and immutable correlation id.
                node["name"] = step.get("name") or node.get("name")
                node["kind"] = step.get("kind") or node.get("kind")
                node["status"] = step.get("status") or node.get("status")
                node["skipped"] = bool(step.get("skipped"))
                node["duration_ms"] = step.get("duration_ms")
                node["agent_run_ids"] = list(step.get("agent_runs") or [])
                node["execution"] = step
                node["agent_runs"] = list(step.get("runs") or [])
        elif node_type == "agent":
            run_id = str(
                node.get("agent_run_id")
                or next(iter(node.get("agent_run_ids") or []), "")
            )
            run = by_run_id.get(run_id)
            if run is None:
                node["agent_run"] = {
                    "agent_run_id": run_id,
                    "missing": True,
                }
            else:
                node["name"] = run.get("agent") or node.get("name")
                node["task_type"] = run.get("task_type") or ""
                node["status"] = run.get("status") or node.get("status")
                node["duration_ms"] = run.get("duration_ms")
                node["total_tokens"] = int(run.get("total_tokens") or 0)
                node["agent_run"] = {
                    key: run.get(key)
                    for key in (
                        "agent_run_id",
                        "agent",
                        "task_type",
                        "status",
                        "duration_ms",
                        "total_tokens",
                        "created_at",
                        "ended_at",
                        "error",
                    )
                }
        elif node_type == "stage_end":
            node["status"] = stage.get("status") or node.get("status")
            node["duration_ms"] = stage.get("duration_ms")
        nodes.append(node)

    edges = [
        dict(edge)
        for edge in (persisted.get("edges") or [])
        if isinstance(edge, dict)
    ]
    raw_metadata = persisted.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    actual = metadata.get("actual") is True
    stage["dag"] = {
        **persisted,
        "available": actual,
        "reason": (
            None
            if actual
            else str(metadata.get("dag_error") or "stage_dag_invalid")
        ),
        "nodes": nodes,
        "edges": edges,
    }


class TraceReader:
    """观测数据的只读入口。

    Args:
        connect_factory: 零参可调用对象，返回 DB-API 连接。
        dialect: ``mysql`` 或 ``sqlite``。
    """

    def __init__(self, connect_factory: Any, *, dialect: Dialect = "mysql") -> None:
        if not callable(connect_factory):
            raise TypeError("connect_factory must be callable")
        self._db = TraceDatabase(connect_factory, dialect=dialect)

    # ------------------------------ 底层 ------------------------------ #
    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        connection = cursor = None
        try:
            connection = self._db.connect()
            cursor = connection.cursor()
            self._db.execute(cursor, sql, tuple(params))
            columns = [c[0] for c in cursor.description or ()]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            for handle in (cursor, connection):
                try:
                    if handle is not None:
                        handle.close()
                except Exception:
                    pass

    def _one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def health(self) -> dict[str, Any]:
        """连通性与各表行数；页面顶部的自检用。"""

        tables = (
            "workflow_run_info",
            "stage_run_info",
            "stage_step_records",
            "agent_run_info",
            "agent_run_records",
            "agent_session_info",
            "llm_call_info",
            "llm_run_records",
        )
        counts: dict[str, int] = {}
        errors: dict[str, str] = {}
        for table in tables:
            try:
                # 表名来自上面的封闭常量元组，不接收用户输入。
                row = self._one(f"SELECT COUNT(*) AS n FROM {table}")
                counts[table] = int((row or {}).get("n", 0) or 0)
            except Exception as exc:
                counts[table] = 0
                errors[table] = type(exc).__name__
        return {
            "ok": not errors,
            "dialect": self._db.dialect,
            "tables": counts,
            "errors": errors,
        }

    # ------------------------------ workflow ------------------------------ #
    def workflows(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        name: str | None = None,
        trace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """流水线列表，最近的在前。"""

        where, params = [], []
        for column, value in (
            ("status", status),
            ("name", name),
            ("trace_id", trace_id),
        ):
            if value:
                where.append(f"{column} = %s")
                params.append(value)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        rows = self._rows(
            "SELECT workflow_run_id, trace_id, name, status, stages_total, stages_done, "
            "failed_stage, failed_stages_json, total_tokens, total_input_tokens, "
            "total_output_tokens, cached_tokens, reasoning_tokens, duration_ms, "
            "error, created_at, ended_at FROM workflow_run_info"
            f"{clause} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            [*params, _limit(limit), max(0, int(offset))],
        )
        for row in rows:
            row["failed_stages"] = _loads(row.pop("failed_stages_json", None), [])
        return rows

    def workflow(self, workflow_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM workflow_run_info WHERE workflow_run_id = %s", [workflow_id]
        )
        if row is None:
            return None
        row["inputs"] = _loads(row.pop("inputs_json", None), {})
        row["stage_names"] = _loads(row.pop("stage_names_json", None), [])
        row["failed_stages"] = _loads(row.pop("failed_stages_json", None), [])
        return row

    def workflow_usage(self, workflow_id: str) -> dict[str, int]:
        """Aggregate live LLM usage for one Workflow.

        Workflow totals are finalized only when the Workflow closes.  Reading
        the child calls keeps an active-run console truthful without loading
        every request or response snapshot.
        """

        row = (
            self._one(
                "SELECT COUNT(*) AS llm_calls, "
                "COALESCE(SUM(total_input_tokens), 0) AS total_input_tokens, "
                "COALESCE(SUM(total_output_tokens), 0) AS total_output_tokens, "
                "COALESCE(SUM(total_tokens), 0) AS total_tokens, "
                "COALESCE(SUM(cached_tokens), 0) AS cached_tokens, "
                "COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens "
                "FROM llm_call_info WHERE flow_id = %s",
                [workflow_id],
            )
            or {}
        )
        return {
            name: int(row.get(name, 0) or 0)
            for name in (
                "llm_calls",
                "total_input_tokens",
                "total_output_tokens",
                "total_tokens",
                "cached_tokens",
                "reasoning_tokens",
            )
        }

    def workflow_stage_progress(self, workflow_id: str) -> dict[str, int]:
        """Aggregate live Stage progress before the Workflow is finalized."""

        row = self._one(
            "SELECT COUNT(*) AS stages_executed, "
            "COALESCE(SUM(CASE WHEN LOWER(COALESCE(status, '')) IN "
            "('success', 'succeeded', 'complete', 'completed', 'skipped') "
            "THEN 1 ELSE 0 END), 0) AS stages_done "
            "FROM stage_run_info WHERE workflow_run_id = %s",
            [workflow_id],
        ) or {}
        return {
            "stages_executed": int(row.get("stages_executed", 0) or 0),
            "stages_done": int(row.get("stages_done", 0) or 0),
        }

    # ------------------------------ stage ------------------------------ #
    def stages(self, workflow_id: str) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM stage_run_info WHERE workflow_run_id = %s ORDER BY seq",
            [workflow_id],
        )
        for row in rows:
            _stage_io(row)
        return rows

    def stage(self, stage_id: str) -> dict[str, Any] | None:
        """Read one Stage row with the same normalized input/output contract."""

        row = self._one(
            "SELECT * FROM stage_run_info WHERE stage_run_id = %s",
            [stage_id],
        )
        if row is None:
            return None
        _stage_io(row)
        return row

    def stage_output(
        self,
        workflow_id: str,
        stage_name: str,
    ) -> dict[str, Any] | None:
        """Read only one Stage's final output for an owning Workflow.

        Console-style consumers do not need the progressively accumulated
        Stage inputs, DAG, Step rows, or Agent/LLM payloads.  Keeping this as a
        narrow parameterized query avoids loading or exposing that unrelated
        trace data when an operator opens one recorded Stage result.
        """

        row = self._one(
            "SELECT stage_run_id, workflow_run_id, name, seq, status, "
            "outputs_json, output_keys_json, duration_ms, error, "
            "created_at, ended_at FROM stage_run_info "
            "WHERE workflow_run_id = %s AND name = %s "
            "ORDER BY seq DESC LIMIT 1",
            [workflow_id, stage_name],
        )
        if row is None:
            return None
        row["outputs"] = _loads(row.pop("outputs_json", None), {})
        row["output_keys"] = _loads(row.pop("output_keys_json", None), [])
        return row

    def steps(self, stage_id: str) -> list[dict[str, Any]]:
        """环节内的步骤。``agent_profile`` 是当时的 Agent 配置快照。"""

        rows = self._rows(
            "SELECT * FROM stage_step_records WHERE stage_run_id = %s ORDER BY seq",
            [stage_id],
        )
        for row in rows:
            row["agent_profile"] = _loads(row.pop("agent_profile_json", None), {})
            row["agent_runs"] = _loads(row.pop("agent_runs_json", None), [])
            row["output_keys"] = _loads(row.pop("output_keys_json", None), [])
        return rows

    # ------------------------------ agent ------------------------------ #
    def agent_runs(self, run_ids: Sequence[str]) -> list[dict[str, Any]]:
        """按 id 批量取 Agent 运行。

        一次取一批而不是循环单查：一个 stage 动辄几十个 agent 运行，逐个查会把
        页面拖成几十次往返。
        """

        ids = [r for r in run_ids if r]
        if not ids:
            return []
        holders = ", ".join(["%s"] * len(ids))
        rows = self._rows(
            f"SELECT * FROM agent_run_info WHERE agent_run_id IN ({holders}) "
            "ORDER BY created_at",
            ids,
        )
        for row in rows:
            row["inputs"] = _loads(row.pop("inputs_json", None), {})
            row["repairs"] = _loads(row.pop("repairs_json", None), {})
            row["output_keys"] = _loads(row.pop("output_keys_json", None), [])
        return rows

    def agents_for_stage(self, stage_id: str) -> list[dict[str, Any]]:
        """取一个 Stage 的全部 Agent 运行。

        ``stage_step_records.agent_runs_json`` 是 Step 与 Agent 的精确归属，但发生
        观测写入失败时可能缺少某个 id。这里同时按 ``agent_run_info.stage_id`` 查询，
        让详情页能把这些“未挂到 Step 的运行”明确展示出来，而不是静默丢失。
        """

        rows = self._rows(
            "SELECT * FROM agent_run_info WHERE stage_id = %s ORDER BY created_at",
            [stage_id],
        )
        for row in rows:
            row["inputs"] = _loads(row.pop("inputs_json", None), {})
            row["repairs"] = _loads(row.pop("repairs_json", None), {})
            row["output_keys"] = _loads(row.pop("output_keys_json", None), [])
        return rows

    def agent_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self.agent_runs([run_id])
        return rows[0] if rows else None

    def agent_records(self, run_id: str) -> list[dict[str, Any]]:
        """一次运行内的编排事件：跑了哪些工具、纠错了几次、为什么判定不合格。"""

        rows = self._rows(
            "SELECT seq, event_type, step_index, level, payload_json, recorded_at "
            "FROM agent_run_records WHERE agent_run_id = %s ORDER BY seq",
            [run_id],
        )
        for row in rows:
            row["payload"] = _loads(row.pop("payload_json", None), {})
        return rows

    def session(self, session_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM agent_session_info WHERE session_id = %s", [session_id]
        )

    # ------------------------------ llm ------------------------------ #
    def llm_calls(self, agent_run_id: str) -> list[dict[str, Any]]:
        """取一次 Agent 运行发起的全部逻辑 LLM 调用。

        使用 ``SELECT *`` 是有意的：完整请求快照也属于用户要求保留的 Trace 信息，
        同时兼容早期 SQLite 表尚未拥有 cached/reasoning 两列的数据文件。
        """

        rows = self._rows(
            "SELECT * FROM llm_call_info WHERE agent_id = %s ORDER BY created_at",
            [agent_run_id],
        )
        for row in rows:
            row["request"] = _loads(row.pop("request_json", None), {})
        return rows

    def stage_llm_usage(self, stage_id: str) -> list[dict[str, Any]]:
        """Return call metrics for a Stage without loading request snapshots.

        The integrated engineering console needs provider/model/token data, but
        it must not pull complete prompts and tool schemas into its read path.
        Keeping this projection here also avoids one query per Agent.
        """

        return self._rows(
            "SELECT llm_call_id, agent_id, provider, model, "
            "total_input_tokens, total_output_tokens, total_tokens, "
            "cached_tokens, reasoning_tokens, retry_count, duration_ms, "
            "usage_complete, created_at "
            "FROM llm_call_info WHERE stage_id = %s ORDER BY created_at",
            [stage_id],
        )

    def llm_call(self, call_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM llm_call_info WHERE llm_call_id = %s", [call_id])
        if row is None:
            return None
        row["request"] = _loads(row.pop("request_json", None), {})
        return row

    def llm_records(self, call_id: str) -> list[dict[str, Any]]:
        """一次调用的完整生命周期：每次 attempt 的开始、成功/失败、重试。"""

        rows = self._rows(
            "SELECT seq, event_type, attempt_no, level, duration_ms, payload_json, "
            "recorded_at FROM llm_run_records WHERE llm_call_id = %s ORDER BY seq",
            [call_id],
        )
        for row in rows:
            row["payload"] = _loads(row.pop("payload_json", None), {})
        return rows

    def llm_detail(self, call_id: str) -> dict[str, Any] | None:
        """一次 LLM 调用、完整事件序列及其 Workflow/Stage/Agent 归属。"""

        call = self.llm_call(call_id)
        if call is None:
            return None
        call["records"] = self.llm_records(call_id)
        if call.get("agent_id"):
            call["agent_run"] = self._one(
                "SELECT agent_run_id, agent, status, stage_id, flow_id "
                "FROM agent_run_info WHERE agent_run_id = %s",
                [call["agent_id"]],
            )
        if call.get("stage_id"):
            call["stage"] = self._one(
                "SELECT stage_run_id, name, status, workflow_run_id "
                "FROM stage_run_info WHERE stage_run_id = %s",
                [call["stage_id"]],
            )
        if call.get("flow_id"):
            call["workflow"] = self._one(
                "SELECT workflow_run_id, name, status FROM workflow_run_info "
                "WHERE workflow_run_id = %s",
                [call["flow_id"]],
            )
        return call

    # ------------------------------ 组合 ------------------------------ #
    def workflow_tree(self, workflow_id: str) -> dict[str, Any] | None:
        """一次业务请求的执行树：workflow → stage → agent。

        一次查完整棵树而不是让前端逐层请求：树本身不大（一次请求几十到几百行），
        而逐层请求会让"点开一个环节"变成十几次往返。
        """

        root = self.workflow(workflow_id)
        if root is None:
            return None

        stages = self.stages(workflow_id)
        for stage in stages:
            # 挂到 step_records 而不是 steps：stage 行里已有同名的**数字列** steps
            # （步骤数），覆盖它会让"读列表"和"读单行"拿到类型不同的同名字段。
            steps = self.steps(stage["stage_run_id"])
            run_ids = [rid for step in steps for rid in step["agent_runs"]]
            direct_runs = self.agents_for_stage(stage["stage_run_id"])
            runs = {r["agent_run_id"]: r for r in direct_runs}
            # 兼容旧数据：Step 已保存 run id，但 agent 行的 stage_id 为空。
            for fallback_run in self.agent_runs(
                [rid for rid in run_ids if rid not in runs]
            ):
                runs[fallback_run["agent_run_id"]] = fallback_run
            for step in steps:
                step["runs"] = []
                for rid in step["agent_runs"]:
                    resolved_run = runs.get(rid)
                    if resolved_run is None:
                        # 观测本身可能丢行（写库失败被计数但不抛）。这里如实标出来，
                        # 而不是静默跳过——"少了一条"和"本来就没有"必须能区分。
                        step["runs"].append({"agent_run_id": rid, "missing": True})
                        continue
                    step["runs"].append(resolved_run)
            stage["step_records"] = steps
            attached = set(run_ids)
            stage["unassigned_agent_runs"] = []
            for run in direct_runs:
                if run["agent_run_id"] not in attached:
                    stage["unassigned_agent_runs"].append(run)
            _attach_stage_dag(stage, steps)
        root["stages"] = stages
        return root

    def stage_detail(self, stage_id: str) -> dict[str, Any] | None:
        """一个业务环节的完整视图：Stage DAG 与关联 Agent 运行。"""

        stage = self.stage(stage_id)
        if stage is None:
            return None

        steps = self.steps(stage_id)
        run_ids = [rid for step in steps for rid in step["agent_runs"]]
        direct_runs = self.agents_for_stage(stage_id)
        runs = {r["agent_run_id"]: r for r in direct_runs}
        for fallback_run in self.agent_runs(
            [rid for rid in run_ids if rid not in runs]
        ):
            runs[fallback_run["agent_run_id"]] = fallback_run
        for step in steps:
            step["runs"] = []
            for rid in step["agent_runs"]:
                resolved_run = runs.get(rid)
                if resolved_run is None:
                    step["runs"].append({"agent_run_id": rid, "missing": True})
                    continue
                step["runs"].append(resolved_run)
        stage["step_records"] = steps
        attached = set(run_ids)
        stage["unassigned_agent_runs"] = []
        for run in direct_runs:
            if run["agent_run_id"] not in attached:
                stage["unassigned_agent_runs"].append(run)
        _attach_stage_dag(stage, steps)

        if stage.get("workflow_run_id"):
            parent = self._one(
                "SELECT workflow_run_id, name, status FROM workflow_run_info "
                "WHERE workflow_run_id = %s",
                [stage["workflow_run_id"]],
            )
            stage["workflow"] = parent
        return stage

    def agent_detail(self, run_id: str) -> dict[str, Any] | None:
        """一次 Agent 运行的完整视图：运行行 + 编排事件 + LLM 调用 + 会话。"""

        run = self.agent_run(run_id)
        if run is None:
            return None
        run["records"] = self.agent_records(run_id)
        run["llm_calls_detail"] = self.llm_calls(run_id)
        hierarchy: dict[str, Any] = {
            "plan": None,
            "sub_runs": [],
            "merge": None,
        }
        for record in run["records"]:
            event = str(record.get("event_type") or "")
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if event == "hierarchy_plan":
                hierarchy["plan"] = payload
            elif event == "hierarchy_subtasks":
                rows = payload.get("sub_runs")
                if isinstance(rows, list):
                    hierarchy["sub_runs"] = [
                        dict(item) for item in rows if isinstance(item, dict)
                    ]
                hierarchy["subtasks"] = payload
            elif event == "hierarchy_merge":
                hierarchy["merge"] = payload
        child_ids = [
            str(item.get("agent_run_id") or "")
            for item in hierarchy["sub_runs"]
            if item.get("agent_run_id")
        ]
        children = {
            item["agent_run_id"]: item for item in self.agent_runs(child_ids)
        }
        for item in hierarchy["sub_runs"]:
            child_id = str(item.get("agent_run_id") or "")
            child = children.get(child_id)
            if child is None:
                item["run"] = {
                    "agent_run_id": child_id,
                    "missing": bool(child_id),
                    "status": item.get("status") or "error",
                }
            else:
                child["llm_calls_detail"] = self.llm_calls(child_id)
                item["run"] = child
        if any(
            hierarchy.get(key)
            for key in ("plan", "sub_runs", "merge")
        ):
            run["hierarchy"] = hierarchy
        if run.get("session_id"):
            run["session"] = self.session(run["session_id"])
        if run.get("stage_id"):
            run["stage"] = self._one(
                "SELECT stage_run_id, name, workflow_run_id FROM stage_run_info "
                "WHERE stage_run_id = %s",
                [run["stage_id"]],
            )
        return run

    def recent_failures(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """最近失败的流水线，排查入口。"""

        return self._rows(
            "SELECT workflow_run_id, name, status, failed_stage, error, "
            "duration_ms, created_at FROM workflow_run_info "
            "WHERE status <> 'success' ORDER BY created_at DESC LIMIT %s",
            [_limit(limit)],
        )

    def model_usage(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """按模型汇总用量，看钱花在哪。"""

        return self._rows(
            "SELECT provider, model, COUNT(*) AS calls, "
            "SUM(total_input_tokens) AS input_tokens, "
            "SUM(total_output_tokens) AS output_tokens, "
            "SUM(total_tokens) AS total_tokens, "
            "SUM(retry_count) AS retries, "
            "AVG(duration_ms) AS avg_duration_ms "
            "FROM llm_call_info GROUP BY provider, model "
            "ORDER BY total_tokens DESC LIMIT %s",
            [_limit(limit)],
        )


__all__ = ["TraceReader"]

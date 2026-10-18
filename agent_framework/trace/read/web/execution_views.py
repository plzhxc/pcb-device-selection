"""Stage、Agent 与 LLM 页面路由和关系投影。

Python 保留数据查询、404、Agent 去重和 Mermaid 图源；实际页面位于
``templates/stage.html``、``agent.html``、``llm.html``。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import TraceReader
from .common import render_page


router = APIRouter()


def _reader(request: Request) -> TraceReader:
    return request.app.state.trace_reader


def _mermaid_label(value: Any, limit: int = 56) -> str:
    """生成不能改变 Mermaid 结构的纯文本标签。"""

    text = " ".join(str(value or "—").split())
    for token in ('"', "\\", "[", "]", "{", "}", "<", ">", ";"):
        text = text.replace(token, " ")
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}…"


def _stage_mermaid(stage: dict[str, Any]) -> str:
    """Render the Agent DAG returned by Stage without internal expansion."""

    dag = stage.get("dag") or {}
    raw_nodes = dag.get("nodes") or []
    raw_edges = dag.get("edges") or []
    if not dag.get("available") or not raw_nodes:
        return ""

    lines = ["flowchart LR"]
    node_ids: dict[str, str] = {}
    agent_nodes: list[str] = []
    legacy_step_nodes: list[str] = []
    boundary_nodes: list[str] = []
    failed_nodes: list[str] = []
    skipped_nodes: list[str] = []

    for index, node in enumerate(raw_nodes):
        raw_id = str(node.get("id") or f"anonymous:{index}")
        graph_id = f"N{index}"
        node_ids[raw_id] = graph_id
        node_type = str(node.get("type") or "")
        if node_type == "agent":
            label = _mermaid_label(
                f"{node.get('name') or 'Agent'} · "
                f"{node.get('role') or 'agent'} · "
                f"{node.get('status') or 'unknown'}"
            )
            agent_nodes.append(graph_id)
        elif node_type == "step":
            # Compatibility only for records written before Agent-granularity
            # Stage DAGs. Edges are still rendered verbatim.
            label = _mermaid_label(
                f"{node.get('name') or 'step'} · {node.get('status') or 'unknown'}"
            )
            legacy_step_nodes.append(graph_id)
        elif node_type == "stage_start":
            label = _mermaid_label(f"Stage start · {node.get('name') or ''}")
            boundary_nodes.append(graph_id)
        elif node_type == "stage_end":
            label = _mermaid_label(
                f"Stage end · {node.get('status') or stage.get('status') or ''}"
            )
            boundary_nodes.append(graph_id)
        else:
            label = _mermaid_label(node.get("name") or node_type or raw_id)
        lines.append(f'  {graph_id}["{label}"]')
        status = str(node.get("status") or "").lower()
        if status in {"failed", "error", "cancelled"}:
            failed_nodes.append(graph_id)
        if node.get("skipped") or status == "skipped":
            skipped_nodes.append(graph_id)

    for edge in raw_edges:
        source = node_ids.get(str(edge.get("from") or ""))
        target = node_ids.get(str(edge.get("to") or ""))
        if not source or not target:
            continue
        label = _mermaid_label(edge.get("label") or "", limit=32)
        if label and label != "—":
            lines.append(f"  {source} -->|{label}| {target}")
        else:
            lines.append(f"  {source} --> {target}")

    lines.extend(
        [
            "  classDef boundary "
            "fill:#e0f2fe,stroke:#0284c7,color:#0c4a6e",
            "  classDef legacyStep "
            "fill:#eef2ff,stroke:#4f46e5,color:#312e81",
            "  classDef agent "
            "fill:#dcfce7,stroke:#16a34a,color:#14532d",
            "  classDef failed "
            "fill:#fee2e2,stroke:#dc2626,color:#7f1d1d",
            "  classDef skipped "
            "fill:#f1f5f9,stroke:#94a3b8,color:#475569",
        ]
    )
    if boundary_nodes:
        lines.append(f"  class {','.join(boundary_nodes)} boundary")
    if agent_nodes:
        lines.append(f"  class {','.join(agent_nodes)} agent")
    if legacy_step_nodes:
        lines.append(f"  class {','.join(legacy_step_nodes)} legacyStep")
    if failed_nodes:
        lines.append(f"  class {','.join(failed_nodes)} failed")
    if skipped_nodes:
        lines.append(f"  class {','.join(skipped_nodes)} skipped")
    return "\n".join(lines)


def _agent_hierarchy_mermaid(run: dict[str, Any]) -> str:
    """Render one HierarchicalAgent master and its directly derived runs."""

    hierarchy = run.get("hierarchy") or {}
    sub_runs = hierarchy.get("sub_runs") or []
    if not sub_runs:
        return ""
    master_label = _mermaid_label(f"Master · {run.get('agent') or '?'}")
    lines = ["flowchart LR", f'  M["{master_label}"]']
    child_nodes: list[str] = []
    failed_nodes: list[str] = []
    for index, item in enumerate(sub_runs):
        node = f"C{index}"
        child_nodes.append(node)
        child = item.get("run") or {}
        child_label = _mermaid_label(
            f"Sub {int(item.get('index') or index + 1):02d} · "
            f"{child.get('agent') or child.get('agent_run_id') or 'failed'}"
        )
        lines.append(f'  {node}["{child_label}"]')
        edge = "spawned" if item.get("ok") else "failed"
        lines.append(f"  M -->|{edge}| {node}")
        if not item.get("ok"):
            failed_nodes.append(node)
    lines.extend(
        [
            "  classDef master fill:#eef2ff,stroke:#4f46e5,color:#312e81",
            "  classDef child fill:#f8fafc,stroke:#64748b,color:#0f172a",
            "  classDef failed fill:#fee2e2,stroke:#dc2626,color:#7f1d1d",
            "  class M master",
            f"  class {','.join(child_nodes)} child",
        ]
    )
    if failed_nodes:
        lines.append(f"  class {','.join(failed_nodes)} failed")
    return "\n".join(lines)


def _stage_agents(stage: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(node.get("agent_run") or {})
        for node in ((stage.get("dag") or {}).get("nodes") or [])
        if node.get("type") == "agent"
        and isinstance(node.get("agent_run"), dict)
        and not node["agent_run"].get("missing")
    ]


def _llm_status(records: list[dict[str, Any]]) -> str:
    if not records:
        return "unknown"
    event = str(records[-1].get("event_type") or "").lower()
    if event == "call_completed":
        return "success"
    if event == "call_failed":
        return "error"
    if event == "call_cancelled":
        return "cancelled"
    return "running"


@router.get(
    "/stage/{stage_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def page_stage(request: Request, stage_id: str) -> str:
    stage = _reader(request).stage_detail(stage_id)
    if stage is None:
        raise HTTPException(404, f"stage_run_id={stage_id} not found")
    return render_page(
        request,
        "stage.html",
        title=f"Stage · {str(stage.get('name') or stage_id)[:40]}",
        stage=stage,
        agents=_stage_agents(stage),
        mermaid_source=_stage_mermaid(stage),
    )


@router.get(
    "/agent/{agent_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def page_agent(request: Request, agent_id: str) -> str:
    run = _reader(request).agent_detail(agent_id)
    if run is None:
        raise HTTPException(404, f"agent_run_id={agent_id} not found")
    return render_page(
        request,
        "agent.html",
        title=f"Agent · {str(run.get('agent') or agent_id)[:40]}",
        run=run,
        records=run.get("records") or [],
        calls=run.get("llm_calls_detail") or [],
        session=run.get("session") or {},
        hierarchy=run.get("hierarchy") or {},
        hierarchy_mermaid=_agent_hierarchy_mermaid(run),
    )


@router.get(
    "/llm/{llm_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def page_llm(request: Request, llm_id: str) -> str:
    call = _reader(request).llm_detail(llm_id)
    if call is None:
        raise HTTPException(404, f"llm_call_id={llm_id} not found")
    records = call.get("records") or []
    return render_page(
        request,
        "llm.html",
        title=f"LLM · {str(call.get('model') or llm_id)[:40]}",
        call=call,
        records=records,
        status=_llm_status(records),
    )


__all__ = ["router"]

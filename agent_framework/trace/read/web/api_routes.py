"""当前八表 Schema 的只读 JSON API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .. import TraceReader


router = APIRouter()


def _reader(request: Request) -> TraceReader:
    return request.app.state.trace_reader


def _required(value: Any, *, kind: str, identifier: str) -> Any:
    if value is None:
        raise HTTPException(404, f"{kind}={identifier} not found")
    return value


@router.get("/health", tags=["health"], summary="Trace 数据库健康检查")
@router.get("/api/health", tags=["health"], include_in_schema=False)
def health(request: Request) -> JSONResponse:
    result = _reader(request).health()
    return JSONResponse(result, status_code=200 if result["ok"] else 503)


@router.get("/api/workflows", tags=["workflow"])
@router.get("/api/flows", tags=["workflow"], include_in_schema=False)
def workflows(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = None,
    name: str | None = None,
    trace_id: str | None = None,
) -> list[dict[str, Any]]:
    return _reader(request).workflows(
        limit=limit,
        offset=offset,
        status=status,
        name=name,
        trace_id=trace_id,
    )


@router.get("/api/workflow/{workflow_id}", tags=["workflow"])
@router.get("/api/flow/{workflow_id}", tags=["workflow"], include_in_schema=False)
def workflow(request: Request, workflow_id: str) -> dict[str, Any]:
    return _required(
        _reader(request).workflow(workflow_id),
        kind="workflow_run_id",
        identifier=workflow_id,
    )


@router.get("/api/workflow/{workflow_id}/tree", tags=["workflow"])
@router.get("/api/flow/{workflow_id}/tree", tags=["workflow"], include_in_schema=False)
def workflow_tree(request: Request, workflow_id: str) -> dict[str, Any]:
    return _required(
        _reader(request).workflow_tree(workflow_id),
        kind="workflow_run_id",
        identifier=workflow_id,
    )


@router.get("/api/workflow/{workflow_id}/stages", tags=["workflow"])
@router.get("/api/flow/{workflow_id}/stages", tags=["workflow"], include_in_schema=False)
def workflow_stages(request: Request, workflow_id: str) -> list[dict[str, Any]]:
    if _reader(request).workflow(workflow_id) is None:
        raise HTTPException(404, f"workflow_run_id={workflow_id} not found")
    return _reader(request).stages(workflow_id)


@router.get("/api/stage/{stage_id}", tags=["stage"])
def stage(request: Request, stage_id: str) -> dict[str, Any]:
    return _required(
        _reader(request).stage_detail(stage_id),
        kind="stage_run_id",
        identifier=stage_id,
    )


@router.get("/api/stage/{stage_id}/steps", tags=["stage"])
@router.get("/api/stage/{stage_id}/raw", tags=["stage"], include_in_schema=False)
def stage_steps(request: Request, stage_id: str) -> list[dict[str, Any]]:
    if _reader(request).stage_detail(stage_id) is None:
        raise HTTPException(404, f"stage_run_id={stage_id} not found")
    return _reader(request).steps(stage_id)


@router.get("/api/stage/{stage_id}/agents", tags=["stage"])
def stage_agents(request: Request, stage_id: str) -> list[dict[str, Any]]:
    if _reader(request).stage_detail(stage_id) is None:
        raise HTTPException(404, f"stage_run_id={stage_id} not found")
    return _reader(request).agents_for_stage(stage_id)


@router.get("/api/stage/{stage_id}/dag", tags=["stage"])
def stage_hierarchy(request: Request, stage_id: str) -> dict[str, Any]:
    """Return the Stage-produced actual DAG enriched with execution rows."""

    detail = _reader(request).stage_detail(stage_id)
    if detail is None:
        raise HTTPException(404, f"stage_run_id={stage_id} not found")
    return {
        "stage_run_id": detail["stage_run_id"],
        "name": detail["name"],
        "status": detail.get("status"),
        "dag": detail.get("dag") or {},
    }


@router.get("/api/agent/{agent_id}", tags=["agent"])
def agent(request: Request, agent_id: str) -> dict[str, Any]:
    return _required(
        _reader(request).agent_detail(agent_id),
        kind="agent_run_id",
        identifier=agent_id,
    )


@router.get("/api/agent/{agent_id}/events", tags=["agent"])
def agent_events(request: Request, agent_id: str) -> list[dict[str, Any]]:
    if _reader(request).agent_run(agent_id) is None:
        raise HTTPException(404, f"agent_run_id={agent_id} not found")
    return _reader(request).agent_records(agent_id)


@router.get("/api/agent/{agent_id}/llm_calls", tags=["agent"])
def agent_llm_calls(request: Request, agent_id: str) -> list[dict[str, Any]]:
    if _reader(request).agent_run(agent_id) is None:
        raise HTTPException(404, f"agent_run_id={agent_id} not found")
    return _reader(request).llm_calls(agent_id)


@router.get("/api/llm/{llm_id}", tags=["llm"])
def llm(request: Request, llm_id: str) -> dict[str, Any]:
    return _required(
        _reader(request).llm_detail(llm_id),
        kind="llm_call_id",
        identifier=llm_id,
    )


@router.get("/api/llm/{llm_id}/events", tags=["llm"])
def llm_events(request: Request, llm_id: str) -> list[dict[str, Any]]:
    if _reader(request).llm_call(llm_id) is None:
        raise HTTPException(404, f"llm_call_id={llm_id} not found")
    return _reader(request).llm_records(llm_id)


@router.get("/api/session/{session_id}", tags=["agent"])
def session(request: Request, session_id: str) -> dict[str, Any]:
    return _required(
        _reader(request).session(session_id),
        kind="session_id",
        identifier=session_id,
    )


@router.get("/api/failures", tags=["overview"])
def failures(
    request: Request,
    limit: int = Query(20, ge=1, le=500),
) -> list[dict[str, Any]]:
    return _reader(request).recent_failures(limit=limit)


@router.get("/api/usage/models", tags=["overview"])
def model_usage(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    return _reader(request).model_usage(limit=limit)


@router.get(
    "/api/flow/{workflow_id}/module_tree",
    tags=["compatibility"],
    include_in_schema=False,
)
def legacy_module_tree(request: Request, workflow_id: str) -> dict[str, Any]:
    if _reader(request).workflow(workflow_id) is None:
        raise HTTPException(404, f"workflow_run_id={workflow_id} not found")
    return {
        "available": False,
        "workflow_run_id": workflow_id,
        "reason": (
            "The current eight-table trace schema does not persist the legacy "
            "PCB module_decomposition projection."
        ),
        "replacement": f"/api/workflow/{workflow_id}/tree",
    }


__all__ = ["router"]

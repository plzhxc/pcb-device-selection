"""Overview 与 Workflow 页面路由。

这里只查询数据和计算首页汇总；HTML 位于 ``templates/``。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import TraceReader
from .common import render_page


router = APIRouter()


def _reader(request: Request) -> TraceReader:
    return request.app.state.trace_reader


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def page_index(request: Request) -> str:
    workflows = _reader(request).workflows(limit=30)
    succeeded = sum(
        1
        for item in workflows
        if str(item.get("status") or "").lower()
        in {"success", "succeeded", "completed", "complete"}
    )
    running = sum(
        1
        for item in workflows
        if str(item.get("status") or "").lower()
        in {"running", "started", "pending"}
    )
    summary = {
        "total": len(workflows),
        "succeeded": succeeded,
        "running": running,
        "failed": len(workflows) - succeeded - running,
        "last_run": (
            workflows[0].get("created_at") if workflows else None
        ),
    }
    return render_page(
        request,
        "overview.html",
        title="Tracer · Overview",
        workflows=workflows,
        summary=summary,
    )


@router.get(
    "/flow/{workflow_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
@router.get(
    "/workflow/{workflow_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
@router.get(
    "/run/{workflow_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def page_workflow(request: Request, workflow_id: str) -> str:
    tree = _reader(request).workflow_tree(workflow_id)
    if tree is None:
        raise HTTPException(
            404,
            f"workflow_run_id={workflow_id} not found",
        )
    return render_page(
        request,
        "workflow.html",
        title=f"Workflow · {str(tree.get('name') or workflow_id)[:40]}",
        tree=tree,
    )


__all__ = ["router"]

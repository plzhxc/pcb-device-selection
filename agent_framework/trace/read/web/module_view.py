"""旧 PCB Module URL 的明确兼容页面。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .common import render_page


router = APIRouter()


@router.get(
    "/flow/{workflow_id}/modules",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def page_flow_modules(request: Request, workflow_id: str) -> str:
    """保留旧 URL，但不伪造当前 Schema 没有保存的 PCB 专属数据。"""

    tree = request.app.state.trace_reader.workflow_tree(workflow_id)
    if tree is None:
        raise HTTPException(
            404,
            f"workflow_run_id={workflow_id} not found",
        )
    return render_page(
        request,
        "modules.html",
        title="PCB Module View · 不可用",
        tree=tree,
    )


__all__ = ["router"]

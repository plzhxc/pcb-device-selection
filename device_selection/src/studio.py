"""与需求分析工作台的 HTTP 客户端。

放在 `src/` 而不是 `tools/`，因为 web 服务也要用它——服务端不应该依赖一个
命令行脚本。`tools/bridge_studio.py` 复用这里的实现。
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request
import uuid
import zipfile

DEFAULT_BASE = "http://[::1]:8878"


class StudioClient:
    """需求分析工作台的 HTTP 客户端。"""

    def __init__(self, base: str = DEFAULT_BASE, *, timeout: float = 120.0) -> None:
        self.base = str(base).rstrip("/")
        self.timeout = timeout

    def _call(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        raw: bool = False,
    ) -> Any:
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"{method} {path} 失败 {exc.code}：{detail}") from exc
        if raw:
            return body
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body

    def health(self) -> dict:
        return self._call("GET", "/api/health")

    def projects(self) -> list[dict]:
        return self._call("GET", "/api/projects")

    def project(self, project_id: str) -> dict:
        return self._call("GET", f"/api/projects/{project_id}")

    def handoff_check(self, project_id: str) -> dict:
        return self._call("POST", f"/api/projects/{project_id}/handoff-check")

    def create_handoff(
        self,
        project_id: str,
        *,
        base_version: int,
        base_domain_revision: int,
        allow_draft: bool = True,
    ) -> dict:
        return self._call(
            "POST",
            f"/api/projects/{project_id}/handoff",
            {
                "request_id": uuid.uuid4().hex,
                "base_version": base_version,
                "base_domain_revision": base_domain_revision,
                "allow_draft": allow_draft,
            },
        )

    def download_handoff(self, project_id: str, artifact_id: str) -> bytes:
        # 这个接口返回 ZIP 二进制，不能按 JSON 解析
        return self._call(
            "GET", f"/api/projects/{project_id}/handoff/{artifact_id}", raw=True
        )


def fetch_spec(
    client: StudioClient, project_id: str, out_dir: Path | None = None
) -> tuple[dict, dict]:
    """取一份最新交接包并解出 RequirementSpec。"""

    project = client.project(project_id)
    version = int((project.get("document") or {}).get("version") or 0)
    revision = int(project.get("domain_revision") or 0)

    readiness = client.handoff_check(project_id)
    artifact = client.create_handoff(
        project_id,
        base_version=version,
        base_domain_revision=revision,
        allow_draft=True,
    )
    content = client.download_handoff(project_id, artifact["id"])

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "handoff.zip").write_bytes(content)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        spec = json.loads(archive.read("requirement-spec.json"))
        names = archive.namelist()

    if out_dir is not None:
        (out_dir / "requirement-spec.in.json").write_text(
            json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    meta = {
        "project_id": project_id,
        "title": project.get("title"),
        "document_version": version,
        "domain_revision": revision,
        "artifact_id": artifact["id"],
        "handoff_files": names,
        "readiness": {
            "status": readiness.get("status"),
            "blockers": readiness.get("blockers"),
            "contract_valid": readiness.get("contract_valid"),
        },
    }
    return spec, meta

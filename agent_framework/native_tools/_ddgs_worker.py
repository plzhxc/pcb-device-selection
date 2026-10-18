"""隔离执行 DDGS 的私有子进程入口。

DDGS 9.14.4 的部分 engine 会修改进程级 HTTP/2 实现。它不能和承载 LLM Client
的主进程共享，因此这里只接受一份 JSON 请求、执行一次搜索、输出一份有界 JSON
后退出。此模块不是 Agent 工具的公共 API。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping


def run_search(
    payload: Mapping[str, Any],
    *,
    ddgs_type: type[Any] | None = None,
) -> dict[str, Any]:
    """执行一次搜索；``ddgs_type`` 只用于无网络 contract test。"""

    query = payload.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 400:
        raise ValueError("query 必须是 1 到 400 个字符")
    max_results = payload.get("max_results")
    if (
        isinstance(max_results, bool)
        or not isinstance(max_results, int)
        or not 1 <= max_results <= 10
    ):
        raise ValueError("max_results 必须是 1 到 10 的整数")
    timeout_s = payload.get("timeout_s")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or timeout_s <= 0:
        raise ValueError("timeout_s 必须是正整数")

    if ddgs_type is None:
        from ddgs import DDGS

        ddgs_type = DDGS

    client = ddgs_type(
        proxy=payload.get("proxy"),
        timeout=timeout_s,
        verify=payload.get("verify", True),
    )
    rows = client.text(
        query,
        region=str(payload["region"]),
        safesearch=str(payload["safe_search"]),
        timelimit=payload.get("time_limit"),
        max_results=max_results,
        backend=str(payload["backend"]),
    )
    bounded_rows = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        bounded_rows.append(
            {
                "title": str(row.get("title") or "")[:500],
                "href": str(row.get("href") or row.get("url") or "")[:2_048],
                "body": str(row.get("body") or row.get("snippet") or "")[:2_000],
            }
        )
        if len(bounded_rows) >= max_results:
            break
    return {"status": "success", "rows": bounded_rows}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, Mapping):
            raise TypeError("worker 请求必须是 JSON object")
        result = run_search(payload)
    except Exception as exc:
        result = {
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc)[:2_000],
        }
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

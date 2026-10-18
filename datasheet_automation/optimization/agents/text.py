"""Text-specialist agents used by the datasheet optimization pipeline."""

from __future__ import annotations

import json
import re
from typing import Any

from agent_framework import AgentServices, BaseAgent, SystemPrompt, UserPrompt

from ..parsing import extract_fenced_block, extract_json_payload, guess_title
from ..settings import build_optimization_agent_services


def _clean_agent_meta(markdown: str) -> str:
    marker = re.compile(
        r"^\s*(?:I (?:will|would|need|am)\b|Let's\b|我将|我会|下面是|以下是|"
        r"分析过程|推理过程)",
        re.IGNORECASE,
    )
    return "\n".join(
        line for line in str(markdown or "").splitlines() if not marker.search(line)
    ).strip()


def _blocks_from_payload(payload: Any) -> list[dict[str, str]]:
    if isinstance(payload, dict):
        keys = sorted(
            payload,
            key=lambda key: (
                int(match.group(1)) if (match := re.search(r"(\d+)$", str(key))) else 10**9,
                str(key),
            ),
        )
        values = [payload[key] for key in keys]
    elif isinstance(payload, list):
        values = payload
    else:
        return []

    blocks: list[dict[str, str]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        content = _clean_agent_meta(str(item.get("content") or ""))
        title = str(item.get("title") or "").strip() or guess_title(content)
        if content or title:
            blocks.append({"type": "text", "title": title, "content": content})
    return blocks


class LayoutOptimizationAgent(BaseAgent):
    """Repair OCR layout while preserving all technical evidence."""

    SYSTEM_PROMPT = """
你是电子元器件数据手册 Markdown/OCR 版面优化 Agent。

要求：
1. 修复断行、异常空格、错误缩进、重复页眉页脚和明显乱码。
2. 保留原文技术含义、参数、单位、表格、公式、章节编号和图片引用。
3. 不翻译，不推断，不新增或删除技术事实，也不改写图片路径。
4. 正文中不得出现分析过程、自我说明或优化策略。

严格输出两个 fenced block：
```optimization
优化后的 Markdown
```
```uncertainty
无法确定的问题；没有则写 none
```
""".strip()

    def __init__(
        self,
        *,
        model: str | None = None,
        reasoning: bool | None = None,
        services: AgentServices | None = None,
    ) -> None:
        super().__init__(
            task_type="datasheet_layout",
            model=model,
            reasoning=reasoning,
            services=services or build_optimization_agent_services(),
        )
        self.retries = 3

    def format(self, **kwargs: Any) -> None:
        self.context_container.set_system_prompt(
            SystemPrompt(name=self.agent_name, prompt_content=self.SYSTEM_PROMPT)
        )
        self.context_container.set_user_prompt(
            UserPrompt(name="datasheet_markdown", prompt_content=str(kwargs["markdown"]))
        )

    def optimize(self, markdown: str) -> tuple[str, str]:
        fallback = str(markdown or "").strip()
        answer = str(self.run(markdown=fallback) or "")
        optimized = _clean_agent_meta(extract_fenced_block(answer, "optimization"))
        uncertainty = extract_fenced_block(answer, "uncertainty").strip()
        return optimized or fallback, uncertainty


class SemanticSegmentationAgent(BaseAgent):
    """Split optimized Markdown into self-contained semantic blocks."""

    SYSTEM_PROMPT = """
你是电子元器件数据手册语义分块 Agent。

要求：
1. 每个块必须包含 title 和 content，content 保留 Markdown。
2. 标题尽量沿用原文章节标题和编号。
3. 表格、公式、图片引用与其标题/说明保持在同一块。
4. 不翻译、不总结到丢失细节、不推断；不得新增、删除或改写图片路径。
5. 只输出 JSON，不输出分析过程。

输出格式：
```json
{"block1": {"title": "1. Features", "content": "# 1. Features\\n..."}}
```
""".strip()

    def __init__(
        self,
        *,
        model: str | None = None,
        reasoning: bool | None = None,
        services: AgentServices | None = None,
    ) -> None:
        super().__init__(
            task_type="datasheet_segmentation",
            model=model,
            reasoning=reasoning,
            services=services or build_optimization_agent_services(),
        )
        self.retries = 3

    def format(self, **kwargs: Any) -> None:
        self.context_container.set_system_prompt(
            SystemPrompt(name=self.agent_name, prompt_content=self.SYSTEM_PROMPT)
        )
        self.context_container.set_user_prompt(
            UserPrompt(name="optimized_markdown", prompt_content=str(kwargs["markdown"]))
        )

    def segment(self, markdown: str) -> list[dict[str, str]]:
        fallback = [{"type": "text", "title": guess_title(markdown), "content": markdown.strip()}]
        answer = str(self.run(markdown=markdown) or "")
        return _blocks_from_payload(extract_json_payload(answer)) or fallback


class BlockCoherenceAgent(BaseAgent):
    """Merge only neighboring blocks that were split at the wrong boundary."""

    SYSTEM_PROMPT = """
你是电子元器件数据手册分块一致性检查 Agent。

检查相邻块；只合并被错误拆开的同一章节、表格、公式、图片及说明。
不要合并独立章节，不翻译、不删除技术信息、不推断、不改图片路径。
只返回含 title/content 的 JSON 对象，不输出分析过程。
""".strip()

    def __init__(
        self,
        *,
        model: str | None = None,
        reasoning: bool | None = None,
        services: AgentServices | None = None,
    ) -> None:
        super().__init__(
            task_type="datasheet_coherence",
            model=model,
            reasoning=reasoning,
            services=services or build_optimization_agent_services(),
        )
        self.retries = 3

    def format(self, **kwargs: Any) -> None:
        self.context_container.set_system_prompt(
            SystemPrompt(name=self.agent_name, prompt_content=self.SYSTEM_PROMPT)
        )
        self.context_container.set_user_prompt(
            UserPrompt(name="semantic_blocks", prompt_content=str(kwargs["blocks_json"]))
        )

    def merge(self, blocks: list[dict[str, str]]) -> list[dict[str, str]]:
        if len(blocks) <= 1:
            return blocks
        payload = {
            f"block{index + 1}": {
                "title": block.get("title", ""),
                "content": block.get("content", ""),
            }
            for index, block in enumerate(blocks)
        }
        answer = str(
            self.run(blocks_json=json.dumps(payload, ensure_ascii=False, indent=2)) or ""
        )
        return _blocks_from_payload(extract_json_payload(answer)) or blocks


__all__ = [
    "LayoutOptimizationAgent",
    "SemanticSegmentationAgent",
    "BlockCoherenceAgent",
    "guess_title",
]

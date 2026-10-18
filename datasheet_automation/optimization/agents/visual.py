"""Visual-specialist agents used by the datasheet optimization pipeline."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from agent_framework import AgentServices, BaseAgent, SystemPrompt, UserPrompt

from ..parsing import clip_text, extract_json_payload
from ..settings import build_optimization_agent_services


IMAGE_CATEGORIES = {
    "应用电路图",
    "特性曲线",
    "功能框图",
    "封装尺寸图",
    "引脚配置图",
    "表格",
    "其他类别",
}


def _image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class VisualUnderstandingAgent(BaseAgent):
    """Classify and describe one datasheet image in a single VLM call."""

    SYSTEM_PROMPT = """
你是电子元器件数据手册图片理解 Agent。

结合图片、caption 和相邻正文，忠实提取：
- title：优先使用原图图号、caption 或上下文标题；
- category：只能是 应用电路图、特性曲线、功能框图、封装尺寸图、引脚配置图、表格、其他类别；
- description：适合知识库检索的中文描述，保留英文引脚名、网络名、参数名、单位和可读数值。

应用电路图应说明可确认的器件、网络和连接关系；曲线应说明坐标轴、条件和趋势；
封装/引脚图应记录可读尺寸、编号和方向。无法确认的内容写“不确定”，禁止推断。
只输出 JSON：{"title": "...", "category": "...", "description": "..."}
""".strip()

    def __init__(
        self,
        *,
        model: str | None = None,
        reasoning: bool | None = None,
        services: AgentServices | None = None,
    ) -> None:
        super().__init__(
            task_type="datasheet_image_description",
            model=model,
            reasoning=reasoning,
            services=services or build_optimization_agent_services(),
        )
        self.retries = 3

    def format(self, **kwargs: Any) -> None:
        image_path = Path(kwargs["image_path"])
        context = (
            f"图片路径：{kwargs.get('img_path', image_path.name)}\n\n"
            f"caption：{clip_text(kwargs.get('caption'), 1000)}\n\n"
            f"前文：{clip_text(kwargs.get('context_before'), 1500)}\n\n"
            f"后文：{clip_text(kwargs.get('context_after'), 1500)}"
        )
        self.context_container.set_system_prompt(
            SystemPrompt(name=self.agent_name, prompt_content=self.SYSTEM_PROMPT)
        )
        self.context_container.set_user_prompt(
            UserPrompt(
                name="datasheet_image",
                prompt_content=[
                    {"type": "text", "text": context},
                    {"type": "image_url", "image_url": {"url": _image_data_url(image_path)}},
                ],
            )
        )

    def analyze(
        self,
        *,
        image_path: str | Path,
        img_path: str,
        caption: str = "",
        context_before: str = "",
        context_after: str = "",
    ) -> dict[str, str]:
        path = Path(image_path)
        if not path.is_file():
            raise FileNotFoundError(f"visual image is unavailable: {path}")
        answer = str(
            self.run(
                image_path=path,
                img_path=img_path,
                caption=caption,
                context_before=context_before,
                context_after=context_after,
            )
            or ""
        )
        payload = extract_json_payload(answer)
        if not isinstance(payload, dict):
            raise ValueError("visual agent did not return a JSON object")
        title = str(payload.get("title") or caption or path.name).strip()
        category = str(payload.get("category") or "其他类别").strip()
        if category not in IMAGE_CATEGORIES:
            category = "其他类别"
        description = str(payload.get("description") or "").strip()
        return {"title": title, "category": category, "description": description}


class TableOptimizationAgent(BaseAgent):
    """Normalize one MinerU table without losing values or conditions."""

    SYSTEM_PROMPT = """
你是电子元器件数据手册表格整理 Agent。

根据 caption、表格正文、脚注和相邻正文，返回：
1. title：优先保留原表号和标题；
2. description：尽量用 Markdown 表格忠实保留所有字段、数值、单位、条件和脚注。

不要翻译，不要推断，不要总结到丢失数值。只输出 JSON：
{"title": "...", "description": "..."}
""".strip()

    def __init__(
        self,
        *,
        model: str | None = None,
        reasoning: bool | None = None,
        services: AgentServices | None = None,
    ) -> None:
        super().__init__(
            task_type="datasheet_table_description",
            model=model,
            reasoning=reasoning,
            services=services or build_optimization_agent_services(),
        )
        self.retries = 3

    def format(self, **kwargs: Any) -> None:
        body = (
            f"caption：{clip_text(kwargs.get('caption'), 1000)}\n\n"
            f"表格正文：\n{clip_text(kwargs.get('table_body'), 6000)}\n\n"
            f"脚注：{clip_text(kwargs.get('footnote'), 1200)}\n\n"
            f"前文：{clip_text(kwargs.get('context_before'), 1200)}\n\n"
            f"后文：{clip_text(kwargs.get('context_after'), 1200)}"
        )
        self.context_container.set_system_prompt(
            SystemPrompt(name=self.agent_name, prompt_content=self.SYSTEM_PROMPT)
        )
        self.context_container.set_user_prompt(
            UserPrompt(name="datasheet_table", prompt_content=body)
        )

    def optimize(self, **values: Any) -> dict[str, str]:
        answer = str(self.run(**values) or "")
        payload = extract_json_payload(answer)
        if not isinstance(payload, dict):
            raise ValueError("table agent did not return a JSON object")
        return {
            "title": str(payload.get("title") or values.get("caption") or "datasheet_table").strip(),
            "description": str(payload.get("description") or values.get("table_body") or "").strip(),
        }


__all__ = ["VisualUnderstandingAgent", "TableOptimizationAgent", "IMAGE_CATEGORIES"]

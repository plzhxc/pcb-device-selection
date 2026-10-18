"""Provider-neutral tool contracts."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Union


@dataclass
class ToolParameter:
    """一项 provider-neutral 工具参数声明。"""

    name: str
    type: str  # 'string', 'number', 'boolean', 'object', 'array'
    description: str
    required: bool = False


class BaseTool(ABC):
    name: str = ""
    description: str = ""
    parameters: List[ToolParameter] = []

    def __init__(self) -> None:
        """工具通常只通过类属性声明元数据；子类可按需保存业务状态。"""

    def to_dict(self) -> Dict[str, Any]:
        return self.get_openai_format()

    def to_json(self, **json_kwargs) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, **json_kwargs)

    @abstractmethod
    def call(self, params: Union[str, dict], **kwargs) -> Union[str, list, dict]:
        raise NotImplementedError

    def get_openai_parameters_schema(self) -> Dict[str, Any]:
        """获取 OpenAI 兼容的参数模式"""
        return self._convert_parameters_to_openai_schema(self.parameters)

    def _convert_parameters_to_openai_schema(
        self, parameters: List[ToolParameter]
    ) -> Dict[str, Any]:
        """将 ToolParameter 列表转换为 OpenAI schema 格式"""
        properties = {}
        required = []

        for param in parameters:
            properties[param.name] = {
                "type": param.type,
                "description": param.description,
            }
            if param.required:
                required.append(param.name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    @property
    def function(self) -> dict:
        """获取OpenAI格式的函数定义"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.get_openai_parameters_schema(),
        }

    def get_openai_format(self) -> dict:
        """获取OpenAI兼容的工具格式"""
        return {"type": "function", "function": self.function}

"""结构化输出的区块提取、JSON 解析与 Schema 校验。

Agent 在 ``format`` 中向上下文登记一个或多个 ``StructuredOutput``。模型返回后，
本服务依次提取命名区块并校验；失败信息会作为 user 消息写回短期记忆，使 runner
下一轮可以让模型自我修正。没有登记输出时，原始文本直接作为结果返回。
"""

from __future__ import annotations

from typing import Any, Callable

from jsonschema import validate

from ..messages import Message
from .response_parsing import extract_text_block, safe_json_loads


class StructuredOutputRuntime:
    """处理结构化输出，但不持有 Agent 状态。

    三个 provider 始终读取 BaseAgent 当前的上下文/logger，避免重建上下文后服务
    仍引用旧对象。
    """

    def __init__(
        self,
        context: Callable[[], Any],
        logger: Callable[[], Any],
        business_logger: Callable[[], Any],
    ) -> None:
        """保存三个延迟 provider，不复制 BaseAgent 的可变状态。"""
        self._context_provider = context
        self._logger_provider = logger
        self._business_logger_provider = business_logger

    @property
    def _context(self) -> Any:
        """取得当前上下文容器。"""
        return self._context_provider()

    @property
    def _logger(self) -> Any:
        """取得当前诊断 logger。"""
        return self._logger_provider()

    @property
    def _business_logger(self) -> Any:
        """取得结构化业务 logger。"""
        return self._business_logger_provider()

    def add_validation_error(self, error_message: str) -> None:
        """把 Schema 错误作为下一轮模型可见的纠错消息。"""
        self._context.short_term_memory.add_turn(
            Message(
                role="user",
                content=(
                    "Json SChema格式验证错误,请确保严格按照Json Schema要求输出, "
                    f"错误信息如下: {error_message}"
                ),
            )
        )

    def add_extraction_error(self, extract_pattern: str) -> None:
        """把缺失输出区块的信息写回短期记忆。"""
        self._context.short_term_memory.add_turn(
            Message(
                role="user",
                content=(
                    f"提取内容失败,请确保内容能够按照{extract_pattern}提取内容,"
                    "并确保提取的内容符合Json Schema要求"
                ),
            )
        )

    def validate_outputs(
        self,
        answer_content: str,
        *,
        add_validation_error: Callable[[str], None],
        add_extraction_error: Callable[[str], None],
    ) -> tuple[Any, bool]:
        """提取所有声明输出，返回 ``(result, has_errors)``。

        可选输出缺失不会失败；必选输出缺失、空回答、JSON 解析错误或 Schema
        错误都会令 ``has_errors`` 为真。
        """
        if not self._context.structured_output:
            # 裸文本 Agent 不需要额外包装，保持原始回答类型。
            return answer_content, False

        if not answer_content or not str(answer_content).strip():
            self._logger.warning(
                msg={
                    "event": "llm_empty_answer",
                    "data": {
                        "hint": "LLM 返回空响应（可能服务 transient 错误或 prompt 过长）"
                    },
                },
                immediate=True,
            )
            return None, True

        results: dict[str, Any] = {}
        has_errors = False
        for output in self._context.structured_output:
            # 第一步只按正则找到区块；是否解析 JSON 由 schema_def 决定。
            extracted = extract_text_block(
                pattern=output.extract_pattern,
                text=answer_content,
            )
            if extracted is None:
                if output.required:
                    self._logger.debug(
                        msg={
                            "event": "answer_extraction_error",
                            "data": {"llm_answer": answer_content},
                        }
                    )
                    add_extraction_error(output.extract_pattern)
                    has_errors = True
                continue

            if output.schema_def is None:
                # 无 schema 的输出保留提取后的文本，适合 Markdown 等非 JSON 结果。
                results[output.name] = extracted
                continue
            try:
                extracted_json = safe_json_loads(extracted)
                validate(instance=extracted_json, schema=output.schema_def)
                results[output.name] = extracted_json
            except Exception as error:
                add_validation_error(str(error))
                self._logger.debug(
                    msg={
                        "event": "schema_validate_error",
                        "data": {
                            "llm_answer": answer_content,
                            "error": str(error),
                        },
                    }
                )
                has_errors = True

        if has_errors:
            return None, True
        return results, False

    def process(
        self,
        request_id: str,
        answer_content: str,
        *,
        validator: Callable[..., tuple[Any, bool]],
        business_logging: bool,
        validator_kwargs: dict[str, Any],
    ) -> tuple[Any, bool]:
        """把 validator 契约转换为 runner 使用的 ``(result, success)``。

        注意第二个返回值在 validator 中表示 ``has_errors``，在本方法返回值中
        表示 ``success``，转换集中在这里完成。
        """
        results, has_errors = validator(answer_content, **validator_kwargs)
        if has_errors:
            self._logger.debug(
                msg={
                    "event": "schema_validate_fail",
                    "data": {"answer_content": answer_content},
                }
            )
            if business_logging:
                self._business_logger.debug(
                    {
                        "agent": self._context.agent_name,
                        "event": "schema_validate_fail",
                        "request_id": request_id,
                        "data": {
                            "results": results,
                            "answer_content": answer_content,
                        },
                    }
                )
            return None, False
        return results, True

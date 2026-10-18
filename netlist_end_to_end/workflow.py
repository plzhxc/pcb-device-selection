"""The two-stage PCBAgent workflow: a DAG of DAG-declared stages."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Callable

from agent_framework.agent import AgentConfig, AgentObserver
from agent_framework.dag import AgentRuntime
from agent_framework.llm import AsyncLLMClient
from agent_framework.stage import StageObserver
from agent_framework.tool import ToolBox
from agent_framework.workflow import Workflow, WorkflowObserver

from .contracts import FlowOptions, PCBFlowResult, STAGE_ORDER, StageName, unwrap_stage_output, wrap_stage_output
from .factory import build_agent_runtime
from .generation_input import validate_generation_input
from .netlist import NetlistStage
from .tools import (
    KnowledgeConfig,
    NetlistKnowledgePrefetcher,
    create_netlist_knowledge_prefetcher,
)
from .top_port import TopPortStage


def build_workflow(
    runtime: AgentRuntime,
    options: FlowOptions | None = None,
    *,
    netlist_knowledge_prefetcher: NetlistKnowledgePrefetcher | None = None,
    stage_observer: StageObserver | None = None,
    workflow_observer: WorkflowObserver | None = None,
) -> Workflow:
    """Two stages in a chain; every stage instantiates its agents through ``runtime``."""

    resolved = options or FlowOptions()
    return Workflow(
        "netlist_end_to_end",
        [
            NetlistStage(resolved, knowledge_prefetcher=netlist_knowledge_prefetcher, observer=stage_observer),
            TopPortStage(resolved, observer=stage_observer),
        ],
        observer=workflow_observer,
        stop_on_error=True,
        runtime=runtime,
    )


class PCBWorkflow:
    """Application facade: translates the two stage outputs into the stable PCBAgent view."""

    def __init__(
        self,
        runtime: AgentRuntime,
        options: FlowOptions | None = None,
        *,
        netlist_knowledge_prefetcher: NetlistKnowledgePrefetcher | None = None,
        stage_observer: StageObserver | None = None,
        workflow_observer: WorkflowObserver | None = None,
    ) -> None:
        self.options = options or FlowOptions()
        self.engine = build_workflow(
            runtime,
            self.options,
            netlist_knowledge_prefetcher=netlist_knowledge_prefetcher,
            stage_observer=stage_observer,
            workflow_observer=workflow_observer,
        )

    @classmethod
    def from_client(
        cls,
        client: AsyncLLMClient,
        config: AgentConfig,
        options: FlowOptions | None = None,
        *,
        agent_configs: Mapping[Any, AgentConfig] | None = None,
        toolboxes: Mapping[Any, ToolBox] | None = None,
        native_toolbox_factory: Callable[[], ToolBox] | None = None,
        knowledge_config: KnowledgeConfig | None = None,
        netlist_knowledge_prefetcher: NetlistKnowledgePrefetcher | None = None,
        agent_observer: AgentObserver | None = None,
        stage_observer: StageObserver | None = None,
        workflow_observer: WorkflowObserver | None = None,
    ) -> "PCBWorkflow":
        """Construct the generation pipeline with deterministic knowledge prefetch."""

        resolved_knowledge = knowledge_config or KnowledgeConfig.from_env()
        runtime_kwargs: dict[str, Any] = {
            "configs": agent_configs,
            "toolboxes": toolboxes,
            "observer": agent_observer,
        }
        if native_toolbox_factory is not None:
            runtime_kwargs["native_toolbox_factory"] = native_toolbox_factory
        runtime = build_agent_runtime(client, config, **runtime_kwargs)

        prefetcher = netlist_knowledge_prefetcher
        if prefetcher is None and resolved_knowledge.enabled:
            prefetcher = create_netlist_knowledge_prefetcher(resolved_knowledge)
        return cls(
            runtime,
            options,
            netlist_knowledge_prefetcher=prefetcher,
            stage_observer=stage_observer,
            workflow_observer=workflow_observer,
        )

    async def arun(
        self,
        *,
        user_input: str,
        generation_input: Mapping[str, Any],
        generate_netlist: bool = True,
        interactive: bool = False,
        start_from: StageName | str | None = None,
        stop_after: StageName | str | None = None,
        upstream: Mapping[Any, Any] | PCBFlowResult | None = None,
        **inputs: Any,
    ) -> PCBFlowResult:
        """Generate local netlists and/or reconcile their ports from explicit input."""

        prepared = validate_generation_input(generation_input)
        normalized_input = str(user_input or "").strip()
        start = StageName.parse(start_from) if start_from is not None else None
        stop = StageName.parse(stop_after) if stop_after is not None else None
        if start is not None and stop is not None and STAGE_ORDER.index(stop) < STAGE_ORDER.index(start):
            raise ValueError("stop_after cannot be earlier than start_from")

        if not generate_netlist:
            raise ValueError("the active workflow requires generate_netlist=True")

        framework_result = await self.engine.arun(
            start_from=start.value if start else None,
            stop_after=stop.value if stop else None,
            upstream=self._normalize_upstream(upstream),
            user_input=normalized_input,
            generation_input=prepared,
            generate_netlist=generate_netlist,
            interactive=interactive,
            **inputs,
        )
        return PCBFlowResult.from_framework(framework_result)

    async def async_run_full_workflow(self, **kwargs: Any) -> dict[str, Any]:
        return (await self.arun(**kwargs)).as_dict()

    async def async_run_stage(
        self,
        stage: StageName | str,
        *,
        user_input: str,
        upstream: Mapping[Any, Any] | PCBFlowResult | None = None,
        generation_input: Mapping[str, Any],
        **inputs: Any,
    ) -> Any:
        """Run one Stage directly against explicit upstream hand-offs."""

        name = StageName.parse(stage)
        result = await self.engine.stage(name.value).execute(
            inputs={"user_input": str(user_input or ""), "generation_input": validate_generation_input(generation_input), **inputs},
            upstream=self._normalize_upstream(upstream),
            index=1,
            runtime=self.engine.runtime,
        )
        if not result.ok:
            if result.error is not None:
                raise result.error
            raise RuntimeError(f"stage {name.value} failed")
        return unwrap_stage_output(result.outputs)

    def run_full_workflow(self, **kwargs: Any) -> dict[str, Any]:
        return self._run_sync(self.async_run_full_workflow(**kwargs))

    def run_stage(self, stage: StageName | str, **kwargs: Any) -> Any:
        return self._run_sync(self.async_run_stage(stage, **kwargs))

    @staticmethod
    def _normalize_upstream(upstream: Mapping[Any, Any] | PCBFlowResult | None) -> dict[str, Any]:
        if upstream is None:
            return {}
        if isinstance(upstream, PCBFlowResult):
            values: dict[StageName, Any] = {
                StageName.NETLIST: list(upstream.netlist_output),
                StageName.TOP_PORT: upstream.top_port_result,
            }
            return {stage.value: wrap_stage_output(value) for stage, value in values.items() if value}
        return {StageName.parse(name).value: wrap_stage_output(value) for name, value in upstream.items()}

    @staticmethod
    def _run_sync(awaitable: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)
        raise RuntimeError(
            "a synchronous PCBWorkflow method cannot run inside an active event loop; await the async method instead"
        )


__all__ = ["PCBWorkflow", "build_workflow"]

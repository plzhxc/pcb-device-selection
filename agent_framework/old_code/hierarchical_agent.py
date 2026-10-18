"""HierarchicalAgent — BaseAgent 子类，提供"Master plan + 并行 Subs + Python merge"模板。

设计动机
========
某些任务一次 LLM 输出过大（5-10K tokens）会触发空响应 / 重试地狱。
拆成多个并行的小 LLM (300-800 tokens) + Python merge 既快又稳。

子类只需 override
================
1. ``master_plan_pattern`` / ``master_plan_schema``  — Master LLM plan 提取 + 校验
2. ``sub_agent_class``                                — 任一 BaseAgent 子类（任意层级嵌套）
3. ``build_sub_kwargs(plan, sub_task) -> kwargs``     — 构造每个 sub 的输入
4. ``merge(plan, sub_results, **kwargs) -> result``  — 纯 Python 合并

flow::

    set_master_prompt() / set_user_prompt() in _post_format (子类做)
        ↓
    1 次 Master LLM → plan = {sub_tasks: [...], ...}
        ↓
    asyncio.gather([sub_agent.async_run(...) for sub_task in plan.sub_tasks])
        ↓
    self.merge(plan, sub_results) — 纯 Python
        ↓
    return {"plan": plan, "sub_results": [...], "result": merged}

用法（子类例子）::

    class NetlistHierarchicalAgent(HierarchicalAgent):
        master_plan_pattern = r'```plan\\s*(.*?)```'
        master_plan_schema = {...}
        sub_agent_class = NetlistFunctionalBlockAgent  # 另一个 BaseAgent 子类

        def build_sub_kwargs(self, plan, sub_task):
            return {
                "sub_task": sub_task,
                "main_chip": plan["main_chip"],
                "shared_nets": plan.get("shared_nets") or [],
                ...
            }

        def merge(self, plan, sub_results, **kwargs):
            # Python 合并 main_chip + 所有 sub 输出
            ...
"""
import asyncio
from typing import Any, Dict, List, Optional, Type
from .base_agent import BaseAgent
from .runtime.context import StructuredOutput


class HierarchicalAgent(BaseAgent):
    """Hierarchical agent 模板基类（Master + parallel Subs + merge）。

    子类必须设：
      - ``master_plan_pattern``  (re str)
      - ``master_plan_schema``   (jsonschema dict)
      - ``sub_agent_class``      (BaseAgent 子类)
      - 实现 ``build_sub_kwargs(plan, sub_task) -> kwargs``
      - 实现 ``merge(plan, sub_results, **kwargs) -> result``
      - 实现 ``_post_format(**kwargs)`` 配 master prompt / 上下文
    """

    master_plan_pattern: str = r'```plan\s*(.*?)```'
    master_plan_schema: Optional[Dict[str, Any]] = None
    sub_agent_class: Optional[Type[BaseAgent]] = None

    # ``None`` keeps the value dynamic so configure() can be called after this
    # module is imported. Subclasses may still pin an explicit positive limit.
    sub_concurrency: Optional[int] = None

    def _sub_concurrency_limit(self) -> int:
        """读取并校验子任务并发上限，保留 configure() 的运行时覆盖能力。"""
        value = (
            self.settings.max_parallel_sub_agents
            if self.sub_concurrency is None
            else self.sub_concurrency
        )
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("sub_concurrency must be a positive integer")
        return value

    def build_sub_kwargs(self, plan: Dict[str, Any], sub_task: Dict[str, Any]) -> Dict[str, Any]:
        """子类 override：构造每个 sub 的输入 kwargs。"""
        raise NotImplementedError(
            f"{self.__class__.__name__} 必须实现 build_sub_kwargs(plan, sub_task)"
        )

    def merge(self, plan: Dict[str, Any], sub_results: List[Any], **kwargs) -> Any:
        """子类 override：把 Master plan + 所有 sub_results 合并成最终结果。"""
        raise NotImplementedError(
            f"{self.__class__.__name__} 必须实现 merge(plan, sub_results, **kwargs)"
        )

    def _register_plan_output(self) -> None:
        """注册 master plan 为 StructuredOutput。子类在 _post_format 里调一次。"""
        self.context_container.structured_output_append(
            StructuredOutput(
                name="plan",
                description="Master meta plan with sub_tasks",
                schema_def=self.master_plan_schema,
                extract_pattern=self.master_plan_pattern,
            )
        )

    async def _spawn_subs(self, plan: Dict[str, Any], **outer_kwargs) -> List[Any]:
        """受信号量保护地并行执行所有 SubAgent。

        单个子任务失败会被转换为带 ``error`` 的结果项，不会取消其他子任务；
        最终由 ``merge`` 决定如何处理部分成功。
        """
        if self.sub_agent_class is None:
            raise NotImplementedError(
                f"{self.__class__.__name__}.sub_agent_class 未设置"
            )
        sub_tasks = plan.get("sub_tasks") or []
        if not sub_tasks:
            return []

        sem = asyncio.Semaphore(self._sub_concurrency_limit())

        async def _run_one(st: Dict[str, Any]) -> Dict[str, Any]:
            """执行单个子任务，并把异常归一化为普通结果项。"""
            # 信号量覆盖“实例化 + 执行”全过程，避免同时创建过多客户端请求。
            async with sem:
                sub = self.sub_agent_class()
                try:
                    sub_kwargs = self.build_sub_kwargs(plan, st)
                    result = await sub.async_run(**sub_kwargs)
                    return {"sub_task": st, "result": result}
                except Exception as e:
                    return {"sub_task": st, "result": None, "error": str(e)}

        return await asyncio.gather(*[_run_one(st) for st in sub_tasks])

    async def async_run(self, **kwargs) -> Dict[str, Any]:
        """Master LLM → plan → spawn subs → merge。"""
        # 第一步仍走 BaseAgent 的完整结构化输出循环，得到名为 plan 的输出。
        llm_out = await super().async_run(**kwargs)
        if not isinstance(llm_out, dict):
            return {"plan": None, "result": None, "error": "Master LLM 未返回结构化输出"}
        plan = llm_out.get("plan")
        if not isinstance(plan, dict) or not plan:
            return {"plan": None, "result": None, "error": "Master plan 缺失或非法",
                    "_llm_out": llm_out}
        # 第二步并发处理 plan 中的 sub_tasks，第三步只用 Python 合并结果。
        sub_results = await self._spawn_subs(plan, **kwargs)
        try:
            merged = self.merge(plan, sub_results, **kwargs)
        except Exception as e:
            return {"plan": plan, "sub_results": sub_results, "result": None,
                    "error": f"merge 失败: {e}"}
        return {"plan": plan, "sub_results": sub_results, "result": merged}

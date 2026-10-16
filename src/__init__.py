"""器件选型阶段（device_selection）自有实现。

本包只做一件事：把已确认的需求条目转换成有证据、可讨论的候选清单。
它不修改需求，不凭模型记忆补造型号，也绝不把"查不到"当成"满足"。
"""

__all__ = ["config", "db", "units", "catalog", "attributes", "evaluate", "service"]

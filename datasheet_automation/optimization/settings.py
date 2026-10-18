"""Optimization-owned configuration adapter for :mod:`agent_framework`."""

from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path

from agent_framework import AgentServices, AgentSettings, build_default_agent_services


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_TYPES = (
    "datasheet_layout",
    "datasheet_segmentation",
    "datasheet_coherence",
    "datasheet_image_description",
    "datasheet_table_description",
)


def load_local_env(path: Path | None = None) -> None:
    """Load an explicitly configured optional secrets file.

    The source package never probes ``.env`` files beside its code.  Callers
    either pass a path or set ``PCBAGENT_DATASHEET_ENV_FILE``.
    """

    configured = os.environ.get("PCBAGENT_DATASHEET_ENV_FILE", "").strip()
    env_path = path or (Path(configured).expanduser() if configured else None)
    if env_path is None:
        return
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


def build_optimization_agent_services() -> AgentServices:
    """Build services using only optimization settings and framework adapters."""

    load_local_env()
    base = AgentSettings.from_env()
    settings = replace(
        base,
        task_model_mapping={name: base.default_model for name in TASK_TYPES},
        reasoning_mode_config={name: base.default_reasoning for name in TASK_TYPES},
    )
    return build_default_agent_services(settings)


__all__ = ["PROJECT_ROOT", "build_optimization_agent_services", "load_local_env"]

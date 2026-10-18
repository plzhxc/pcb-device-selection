"""Model-strength configuration: tiers → models, per-role overrides.

``models.yaml`` next to this file is the single default; ``PCBAGENT_MODEL_CONFIG``
may name an external YAML whose keys override it (deep merge, only the keys it
writes).  :func:`agent_configs` turns the merged mapping into the framework's
``AgentConfig`` objects: one base config plus one per role, with the tier table
attached so ``ctx.model("strong")`` inside any Agent resolves at call time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import os
from pathlib import Path
from typing import Any

import yaml

from agent_framework.agent import AgentConfig
from agent_framework.llm import CallParams

from ..roles import AgentRole

DEFAULT_PATH = Path(__file__).with_name("models.yaml")
ENV_PATH = "PCBAGENT_MODEL_CONFIG"
_PARAM_KEYS = ("reasoning", "temperature", "top_p", "max_tokens", "seed", "timeout_s")


class ModelConfigError(ValueError):
    """The model configuration is malformed; fail before any LLM call."""


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_model_config(path: str | Path | None = None) -> dict[str, Any]:
    """Merged ``{tiers, defaults, roles}``; ``path`` (or the env var) overlays the default file."""

    data = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8")) or {}
    external = path or os.environ.get(ENV_PATH)
    if external:
        overlay = yaml.safe_load(Path(external).expanduser().read_text(encoding="utf-8")) or {}
        if not isinstance(overlay, Mapping):
            raise ModelConfigError(f"{external}: top level must be a mapping")
        data = _deep_merge(data, overlay)
    _validate(data)
    return data


def _validate(data: Mapping[str, Any]) -> None:
    tiers = data.get("tiers")
    if not isinstance(tiers, Mapping) or not tiers:
        raise ModelConfigError("tiers must be a non-empty mapping of tier -> model id")
    for name, model in tiers.items():
        if not isinstance(model, str) or not model.strip():
            raise ModelConfigError(f"tiers.{name}: model id must be a non-empty string")
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, Mapping):
        raise ModelConfigError("defaults must be a mapping")
    if defaults.get("tier") not in tiers:
        raise ModelConfigError(f"defaults.tier must be one of {sorted(tiers)}")
    roles = data.get("roles") or {}
    if not isinstance(roles, Mapping):
        raise ModelConfigError("roles must be a mapping of role -> settings")
    known = {role.value for role in AgentRole}
    for role, settings in roles.items():
        if role not in known:
            raise ModelConfigError(f"roles.{role}: unknown Agent role")
        if not isinstance(settings, Mapping):
            raise ModelConfigError(f"roles.{role}: settings must be a mapping")
        if "tier" in settings and settings["tier"] not in tiers:
            raise ModelConfigError(f"roles.{role}.tier must be one of {sorted(tiers)}")
        unknown = set(settings) - {"tier", *_PARAM_KEYS}
        if unknown:
            raise ModelConfigError(f"roles.{role}: unsupported keys {sorted(unknown)}")


def _params(base: CallParams, settings: Mapping[str, Any]) -> CallParams:
    values = {key: settings[key] for key in _PARAM_KEYS if key in settings}
    return replace(base, **values) if values else base


def agent_configs(
    config: Mapping[str, Any],
    *,
    timeout_s: float | None = None,
) -> tuple[AgentConfig, dict[AgentRole, AgentConfig]]:
    """(base config, per-role configs) for :func:`PCBWorkflow.from_client`.

    The base config carries the default tier's model and the tier table; it
    deliberately leaves ``tier`` unset so an Agent class's own ``model_tier``
    still wins over the default.  Role entries set ``tier`` explicitly and so
    override the class attribute.  ``timeout_s`` (from the CLI) beats the file.
    """

    tiers = {str(k): str(v) for k, v in config["tiers"].items()}
    defaults = dict(config.get("defaults") or {})
    if timeout_s is not None:
        defaults["timeout_s"] = float(timeout_s)
    base_params = _params(CallParams(model=tiers[defaults["tier"]]), defaults)
    base = AgentConfig(params=base_params, model_tiers=tiers)
    per_role: dict[AgentRole, AgentConfig] = {}
    for role_name, settings in (config.get("roles") or {}).items():
        role = AgentRole(role_name)
        tier = settings.get("tier")
        role_params = replace(base_params, model=tiers[tier]) if tier else base_params
        per_role[role] = replace(base, params=_params(role_params, settings), tier=tier)
    return base, per_role


def describe(config: Mapping[str, Any]) -> dict[str, Any]:
    """Compact, secret-free view for logs and the console."""

    tiers = config["tiers"]
    default_tier = config["defaults"]["tier"]
    return {
        "tiers": dict(tiers),
        "default": {"tier": default_tier, "model": tiers[default_tier]},
        "roles": {role: {"tier": s.get("tier", default_tier), "model": tiers[s.get("tier", default_tier)]} for role, s in (config.get("roles") or {}).items()},
    }


__all__ = ["DEFAULT_PATH", "ENV_PATH", "ModelConfigError", "agent_configs", "describe", "load_model_config"]

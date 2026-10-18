"""Read editable netlist prompt resources without process-local caching."""

from __future__ import annotations

import json
from importlib.resources import files
from string import Template
from typing import Any


def load_prompt(name: str) -> str:
    """Load one UTF-8 Markdown resource, preserving its exact whitespace."""
    if not name.endswith(".md") or "/" in name or "\\" in name:
        raise ValueError(f"Expected a Markdown prompt filename, got {name!r}")
    resource = files(__package__).joinpath(name)
    if not resource.is_file():
        raise FileNotFoundError(f"Netlist prompt resource does not exist: {name}")
    return resource.read_bytes().decode("utf-8")


def render_prompt(name: str, /, **values: object) -> str:
    """Substitute named ${fields}; literal dollar signs use $$ in templates."""
    return Template(load_prompt(name)).substitute(values)


class PromptText:
    """Keep legacy class/instance text attributes live as Markdown is edited."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __get__(self, instance: object, owner: type | None = None) -> str:
        return load_prompt(self.name)


class PromptJSON:
    """Expose an editable JSON example as the existing dictionary attribute."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __get__(self, instance: object, owner: type | None = None) -> Any:
        return json.loads(load_prompt(self.name))


__all__ = ["load_prompt", "render_prompt", "PromptText", "PromptJSON"]

"""Small filesystem helpers shared across the platform's domains."""

from __future__ import annotations

import os
import re
from pathlib import Path


DATASHEET_RUNTIME_ROOT = (
    Path(__file__).resolve().parents[3]
    / "07_TEMP"
    / "datasheet_automation"
)


def runtime_output_path(path: str | Path) -> Path:
    """Resolve one datasheet output inside its approved 07_TEMP boundary."""

    requested = Path(path).expanduser()
    resolved = (
        requested.resolve()
        if requested.is_absolute()
        else (DATASHEET_RUNTIME_ROOT / requested).resolve()
    )
    root = DATASHEET_RUNTIME_ROOT.resolve()
    roots = [root]
    configured = os.getenv("PCBAGENT_DATASHEET_OUTPUT", "").strip()
    if configured:
        additional = Path(configured).expanduser().resolve()
        temporary = root.parent
        if additional == temporary or not additional.is_relative_to(temporary):
            raise ValueError("PCBAGENT_DATASHEET_OUTPUT must name a component directory inside 07_TEMP")
        roots.append(additional)
    if not any(resolved == allowed or resolved.is_relative_to(allowed) for allowed in roots):
        raise ValueError(
            "datasheet runtime output must stay inside "
            f"{DATASHEET_RUNTIME_ROOT}"
        )
    return resolved


def ensure_dir(path: str | Path) -> Path:
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def write_text(path: str | Path, text: str) -> Path:
    path = Path(path)
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def safe_filename(value: str, default: str = "device") -> str:
    value = str(value or "").strip()
    value = re.sub(r"[\\/:*?\"<>|\s]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("._")
    return value or default


def normalize_model(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def path_from_env_or_default(name: str, default: str | Path) -> Path:
    return Path(os.getenv(name, str(default))).expanduser().resolve()

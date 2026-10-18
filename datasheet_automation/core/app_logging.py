"""Standard-library logging setup: a rotating file handler, zero new deps.

The platform keeps ``raise`` for control flow; logging is the observability layer
beside it (log then raise at boundaries). Configured once from the ``logging``
block of the request config; a run with no file just logs to stderr at WARNING.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Mapping

from datasheet_automation.core.io import ensure_dir, runtime_output_path


LOGGER_NAME = "datasheet_automation"
_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 3


def configure(settings: Mapping[str, Any] | None = None) -> logging.Logger:
    """Configure and return the platform logger.

    ``settings`` keys: ``level`` (default INFO) and ``file`` (optional path to a
    rotating log). Idempotent -- re-configuring replaces the platform's handlers
    rather than stacking them, so repeated ``run`` calls don't duplicate lines.
    """

    settings = settings or {}
    level = _resolve_level(settings.get("level"))
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT)
    log_file = settings.get("file")
    if log_file:
        path = runtime_output_path(log_file)
        ensure_dir(path.parent)
        handler: logging.Handler = RotatingFileHandler(
            path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
        )
        path.chmod(0o600)
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.setLevel(level)
    logger.addHandler(handler)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def _resolve_level(value: Any) -> int:
    if value is None:
        return logging.INFO
    if isinstance(value, int):
        return value
    return logging.getLevelName(str(value).strip().upper()) if str(value).strip() else logging.INFO

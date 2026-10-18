#!/usr/bin/env bash
set -euo pipefail
STUDIO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PHASE2_PYTHON="${STUDIO_PYTHON:-$STUDIO_ROOT/../.venv/bin/python}"
cd -- "$STUDIO_ROOT"
exec "$PHASE2_PYTHON" -X utf8 scripts/phase2_dev.py "$@"

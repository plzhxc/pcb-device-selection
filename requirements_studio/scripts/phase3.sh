#!/usr/bin/env bash
set -euo pipefail
STUDIO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$STUDIO_ROOT/../.venv/bin/python" -X utf8 "$STUDIO_ROOT/scripts/phase3_dev.py" "$@"

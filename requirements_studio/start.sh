#!/usr/bin/env bash
set -euo pipefail
studio_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
studio_python="${STUDIO_PYTHON:-$studio_root/../.venv/bin/python}"
if [[ ! -x "$studio_python" ]]; then
  printf 'Python 环境不可用。请设置 STUDIO_PYTHON 为已安装 requirements.txt 的 Python 路径。\n' >&2
  exit 1
fi
exec "$studio_python" "$studio_root/scripts/manage.py" "$@"

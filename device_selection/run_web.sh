#!/usr/bin/env bash
# 启动器件选型网页端。
#
#   ./run_web.sh              用默认示例需求清单，端口 8899
#   ./run_web.sh 8901         换端口
#   SPEC=specs/xxx.json ./run_web.sh
#
# 数据库默认走本机 socket（无需密码）。要用远端共享账号，先设置：
#   export DEVICE_SELECTION_DB_MODE=tcp
#   export DEVICE_SELECTION_DB_HOST=10.131.254.102
#   export DEVICE_SELECTION_DB_USER=datas_rw
#   export DEVICE_SELECTION_DB_PASSWORD='...'

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-8899}"
SPEC="${SPEC:-$HERE/specs/example_power.json}"

cd "$HERE"
exec python3 -u web/server.py --port "$PORT" --spec "$SPEC"

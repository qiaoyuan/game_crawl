#!/bin/bash
# 预览：bash cleanup_logs.sh --dry-run；参数说明：bash cleanup_logs.sh --help
set -euo pipefail
PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PYTHON="$PROJECT_DIR/venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    PYTHON=python3
fi
exec "$PYTHON" "$PROJECT_DIR/tools/cleanup_logs.py" "$@"

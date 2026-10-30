#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -x "$ROOT_DIR/.runtime/bin/python" ]; then
  echo "尚未安装运行时，请先执行：$ROOT_DIR/install-offline.sh" >&2
  exit 1
fi
cd "$ROOT_DIR"
exec "$ROOT_DIR/.runtime/bin/python" -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8765}"

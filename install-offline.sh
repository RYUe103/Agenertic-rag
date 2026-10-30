#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime"

if [ "$(uname -m)" != "x86_64" ]; then
  echo "仅支持 x86_64 Linux；当前架构：$(uname -m)" >&2
  exit 1
fi

if [ ! -x "$RUNTIME_DIR/bin/python" ]; then
  echo "安装内置 Python 运行时..."
  bash "$ROOT_DIR/python-linux-x86_64-installer.sh" -b -p "$RUNTIME_DIR"
fi

echo "离线安装 Python 依赖..."
"$RUNTIME_DIR/bin/python" -m pip install --no-index --find-links "$ROOT_DIR/wheels" -r "$ROOT_DIR/requirements-linux-amd64-py312.txt"

if [ ! -f "$ROOT_DIR/.env" ]; then
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
fi

echo "安装完成。启动命令："
echo "  $ROOT_DIR/run.sh"

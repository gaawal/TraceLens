#!/usr/bin/env bash
# 共享环境变量：所有 sim_* 脚本都通过 source 本文件获得路径与运行时。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/frontend"
RUN_DIR="$PROJECT_DIR/scripts/run"
mkdir -p "$RUN_DIR"

PY="$BACKEND_DIR/.venv/bin/python"
UV="$HOME/.local/bin/uv"
MANAGED_NODE_DIR="$HOME/.workbuddy/binaries/node/versions/22.22.2-3/bin"

resolve_node() {
  if command -v node >/dev/null 2>&1; then
    command -v node
  elif [ -x "$MANAGED_NODE_DIR/node" ]; then
    echo "$MANAGED_NODE_DIR/node"
  else
    echo ""
  fi
}

resolve_npm() {
  if command -v npm >/dev/null 2>&1; then
    command -v npm
  elif [ -x "$MANAGED_NODE_DIR/npm" ]; then
    echo "$MANAGED_NODE_DIR/npm"
  else
    echo ""
  fi
}

resolve_redis_server() {
  if command -v redis-server >/dev/null 2>&1; then
    command -v redis-server
  elif [ -x /opt/homebrew/bin/redis-server ]; then
    echo /opt/homebrew/bin/redis-server
  else
    echo ""
  fi
}

resolve_redis_cli() {
  if command -v redis-cli >/dev/null 2>&1; then
    command -v redis-cli
  elif [ -x /opt/homebrew/bin/redis-cli ]; then
    echo /opt/homebrew/bin/redis-cli
  else
    echo ""
  fi
}

wait_port() {
  local host="$1" port="$2" timeout="${3:-30}" waited=0
  while [ "$waited" -lt "$timeout" ]; do
    if "$PY" - "$host" "$port" <<'PY' >/dev/null 2>&1
import socket, sys
sock = socket.socket()
sock.settimeout(0.5)
try:
    sock.connect((sys.argv[1], int(sys.argv[2])))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
    then
      return 0
    fi
    sleep 0.5
    waited=$((waited + 1))
  done
  return 1
}

pids_of() {
  pgrep -f "$1" 2>/dev/null || true
}

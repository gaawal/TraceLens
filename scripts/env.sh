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

# ---------------------------------------------------------------- 状态探测
# 判断服务是否在跑一律**以端口为准**：pidfile 里的进程可能早就被回收，
# 只看 pidfile 会把"服务其实活着"误判成"可以再起一个"。
port_open() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

# 服务是否**真的**能在某个具体地址上连上。
# 不要用 lsof 的 `-i@host` 过滤：多个 `-i` 是"或"，`-a` 只把进程选择和文件选择相与，
# 实测会把同端口、别的地址上的无关进程（甚至别的程序）一起列出来，判断不了绑定。
# 直接做一次 TCP 试探最可靠 —— 这也正是"能通信"的定义。
port_open_on() {
  local host="$1" port="$2"
  "$PY" - "$host" "$port" <<'PY' >/dev/null 2>&1
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
}

# 该端口当前绑定在哪些本地地址上（逗号分隔，已去重）。
# 用来戳穿"配置文件里写了网络 IP，进程其实只绑了回环"这种假象。
listen_addrs() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null \
    | awk 'NR > 1 && $NF == "(LISTEN)" { print $(NF-1) }' \
    | sed 's/:[0-9]*$//' \
    | sort -u | paste -sd, -
}

port_pid() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1
}

pid_alive() {
  [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null
}

read_pid() {
  [ -f "${1:-}" ] && tr -d '[:space:]' <"$1" || true
}

# 按「模块名.pid 记账 + 端口兜底」判断由 simremote CLI 托管的长跑服务。
cli_running() {
  local pid
  pid="$(read_pid "$BACKEND_DIR/simremote/run/${1}.pid")"
  if pid_alive "$pid"; then
    return 0
  fi
  pgrep -f "simremote[.]cli[[:space:]]+${1}([[:space:]]|\$)" >/dev/null 2>&1
}

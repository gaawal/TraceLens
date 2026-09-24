#!/usr/bin/env bash
# 停止 TraceLens 本地演示环境：前端 → 后端 → 模拟机群。
# 默认不停止 Redis（系统里可能已有其它用途），需要时加 --redis。
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

info() { printf '\033[36m[sim_down]\033[0m %s\n' "$*"; }

stop_pattern() {
  local pattern="$1" label="$2"
  local pids
  pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
  if [ -z "$pids" ]; then
    info "$label 未在运行"
    return 0
  fi
  info "停止 $label (pid: $pids)"
  kill $pids 2>/dev/null || true
  sleep 1
  pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
  [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
  return 0
}

stop_pattern "vite" "Vite 前端"
stop_pattern "uvicorn config.asgi" "Django 后端"
stop_pattern "start_stt.py" "本地 STT 服务"
(cd "$BACKEND_DIR" && "$PY" -m simremote.cli stream-stop) || info "实时日志源停止未完成，请检查上方提示"
(cd "$BACKEND_DIR" && "$PY" -m simremote.cli stop) || info "机群停止未完成，请检查上方提示"
(cd "$BACKEND_DIR" && "$PY" -m simremote.cli site-stop) || info "报告站停止未完成，请检查上方提示"

if [ "${1:-}" = "--redis" ]; then
  if [ -f "$RUN_DIR/redis.pid" ]; then
    info "停止 Redis (pid: $(cat "$RUN_DIR/redis.pid"))"
    kill "$(cat "$RUN_DIR/redis.pid")" 2>/dev/null || true
    rm -f "$RUN_DIR/redis.pid"
  else
    info "Redis 不是由 sim_up.sh 启动的，保持运行"
  fi
fi
info "完成"

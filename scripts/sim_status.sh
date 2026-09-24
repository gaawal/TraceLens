#!/usr/bin/env bash
# 查看本地演示环境状态：Redis / 模拟机群 / 报告站 / 实时日志源 / 后端 / 前端 / STT
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

line() {
  local name="$1" port="$2"
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    printf '  %-14s \033[32m运行中\033[0m  %s\n' "$name" "port $port"
  else
    printf '  %-14s \033[31m未运行\033[0m  %s\n' "$name" "port $port"
  fi
}

# 实时日志源不监听端口，只能按进程名判断
stream_line() {
  if pgrep -f "simremote.cli stream" >/dev/null 2>&1; then
    printf '  %-14s \033[32m运行中\033[0m  %s\n' "实时日志源" "持续追加 <fm>.log"
  else
    printf '  %-14s \033[31m未运行\033[0m  %s\n' "实时日志源" "scripts/sim_up.sh 可拉起"
  fi
}

echo "TraceLens 本地模拟环境状态"
line "Redis 7" 6379
line "上位机 SSH" 2222
line "下位机 SSH" 2223
line "模拟报告站" 8901
line "Django API" 8000
line "本地 STT" 8001
line "Vite 前端" 5173
stream_line

echo
(cd "$BACKEND_DIR" && "$PY" -m simremote.cli status) || true

echo
echo "后端日志  : $BACKEND_DIR/runtime/tracelens.log"
echo "机群日志  : $BACKEND_DIR/simremote/run/fleet.log"
echo "报告站日志: $RUN_DIR/site.out"
echo "日志源日志: $RUN_DIR/stream.out"
echo "前端日志  : $RUN_DIR/frontend.out"

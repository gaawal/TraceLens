#!/usr/bin/env bash
# Redis 7 一键重启（在项目虚拟环境 backend/.venv 中执行健康检查）
#
# 用法：
#   scripts/redis_restart.sh            # 重启（默认）
#   scripts/redis_restart.sh restart    # 重启
#   scripts/redis_restart.sh stop       # 只停
#   scripts/redis_restart.sh start      # 只启
#   scripts/redis_restart.sh status     # 只看状态（健康检查）
#   scripts/redis_restart.sh restore-launchd   # 交还给 launchd 托管（撤销卸载前状态）
#
# 选项：
#   --keep-launchd   不卸载 launchd 的 homebrew.mxcl.redis（默认会卸载，
#                    否则 KeepAlive 会立刻把 Redis 拉回来，重启等于没重启）
#   --keep-venv      不 source 虚拟环境，用系统 python 做检查
#
# 背景：本机 Redis 由 ~/Library/LaunchAgents/homebrew.mxcl.redis.plist 托管
#       (KeepAlive=true)，且 `brew services` 在当前 macOS 上已报错不可用，
#       所以停止动作直接走 launchctl bootout/unload。
#
# 启动参数与 scripts/sim.sh、docker-compose 保持一致：
#       无持久化 + allkeys-lru + 2gb 上限；pidfile / 日志落在 scripts/run/。
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${REDIS_PORT:-6379}"
REDIS_MAXMEMORY="${REDIS_MAXMEMORY:-2gb}"
REDIS_POLICY="${REDIS_POLICY:-allkeys-lru}"
LAUNCHD_LABEL="homebrew.mxcl.redis"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"
PIDFILE="$RUN_DIR/redis.pid"
LOGFILE="$RUN_DIR/redis.log"

KEEP_LAUNCHD=0
KEEP_VENV=0
ACTION="restart"
for arg in "$@"; do
  case "$arg" in
    restart|stop|start|status|restore-launchd) ACTION="$arg" ;;
    --keep-launchd) KEEP_LAUNCHD=1 ;;
    --keep-venv) KEEP_VENV=1 ;;
    -h|--help) sed -n '2,/^set -/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' | sed '$d'; exit 0 ;;
    *) printf '\033[31m[redis]\033[0m 未知参数: %s（-h 查看用法）\n' "$arg" >&2; exit 2 ;;
  esac
done

info() { printf '\033[36m[redis]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[redis]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[redis]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 虚拟环境
enter_venv() {
  if [ "$KEEP_VENV" = "1" ]; then
    warn "按 --keep-venv 跳过虚拟环境，使用系统 python"
    return 0
  fi
  if [ -f "$BACKEND_DIR/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$BACKEND_DIR/.venv/bin/activate"
    info "已进入虚拟环境 backend/.venv（$(python -V 2>&1)）"
  else
    warn "未找到 backend/.venv，改用系统 python 做健康检查"
  fi
}

# 用 redis-py（虚拟环境里已装）做一次真实 PING
redis_ping() {
  "$PY" - "$REDIS_HOST" "$REDIS_PORT" <<'PY' >/dev/null 2>&1
import sys
import redis
try:
    client = redis.Redis(host=sys.argv[1], port=int(sys.argv[2]),
                         socket_connect_timeout=1, socket_timeout=1)
    raise SystemExit(0 if client.ping() else 1)
except Exception:
    raise SystemExit(1)
PY
}

port_pids() {
  lsof -nP -iTCP:"$REDIS_PORT" -sTCP:LISTEN -t 2>/dev/null || true
}

show_health() {
  "$PY" - "$REDIS_HOST" "$REDIS_PORT" "$PIDFILE" "$LOGFILE" <<'PY'
import sys
import redis

host, port, pidfile = sys.argv[1], int(sys.argv[2]), sys.argv[3]
client = redis.Redis(host=host, port=port, socket_connect_timeout=2, socket_timeout=2)
try:
    info = client.info()
except Exception as exc:  # noqa: BLE001
    print(f"  连接失败: {exc}")
    raise SystemExit(1)

ver = info.get("redis_version")
try:
    save_conf = client.config_get("save").get("save") or ""
except Exception:  # noqa: BLE001
    save_conf = ""
mode = "已禁用持久化" if not save_conf.strip() else f"RDB {save_conf}"
if info.get("loading"):
    mode = "正在加载数据"
try:
    with open(pidfile, encoding="utf-8") as handle:
        pid = handle.read().strip()
except OSError:
    pid = "（非本脚本启动，无 pidfile）"

print(f"  地址        : redis://{host}:{port}")
print(f"  版本        : {ver}")
print(f"  运行模式    : {mode}  AOF={'开' if info.get('aof_enabled') else '关'}")
print(f"  内存        : 已用 {info.get('used_memory_human')} / 上限 {info.get('maxmemory_human') or '未设'}")
print(f"  淘汰策略    : {info.get('maxmemory_policy')}")
print(f"  当前库键数  : {client.dbsize()}")
print(f"  连接客户端  : {info.get('connected_clients')}")
print(f"  进程 PID    : {pid}")
print(f"  日志        : {sys.argv[4] if len(sys.argv) > 4 else ''}")
PY
}

# ---------------------------------------------------------------- 停止
# 卸载 launchd 服务（KeepAlive=true 时，不卸载就会被立刻拉回）。
# 返回 0 = 确实处于加载态且已卸载；返回 1 = 未加载或当前 shell 看不到该 domain。
unload_launchd() {
  if launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1 \
      || launchctl unload "$LAUNCHD_PLIST" >/dev/null 2>&1
    return 0
  fi
  return 1
}

stop_redis() {
  local changed=0 rounds=0 pids

  # 1) launchd 托管（KeepAlive=true）：先卸载，否则 kill 完会被立刻拉回
  if [ "$KEEP_LAUNCHD" = "1" ]; then
    warn "--keep-launchd：保留 launchd 服务（Redis 可能在 kill 后被自动拉回）"
  elif unload_launchd; then
    info "已卸载 launchd 服务 ${LAUNCHD_LABEL}（KeepAlive 会阻止真正停止）"
    changed=1
  elif [ -f "$LAUNCHD_PLIST" ]; then
    info "launchd 服务未加载（或当前 shell 不在 GUI 会话中），直接按进程停止"
  fi

  # 2) 本脚本启动时留下的 pidfile
  if [ -f "$PIDFILE" ]; then
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      info "停止 pidfile 记录的 Redis (pid $pid)"
      kill "$pid" 2>/dev/null || true
      changed=1
    fi
    rm -f "$PIDFILE"
  fi

  # 3) 兜底：谁占用端口就停谁。若被 KeepAlive 拉回，最多再重试 2 轮。
  while [ "$rounds" -lt 3 ]; do
    pids="$(port_pids)"
    [ -z "$pids" ] && break

    if [ "$rounds" -eq 0 ]; then
      info "停止占用 ${REDIS_PORT} 端口的 Redis (pid: $(echo "$pids" | tr '\n' ' '))"
    else
      warn "Redis 被自动拉起，再次停止 (pid: $(echo "$pids" | tr '\n' ' '))"
      unload_launchd >/dev/null 2>&1 || true
    fi
    kill $pids 2>/dev/null || true
    changed=1

    # 给优雅退出留时间；仍在就强杀
    local waited=0
    while [ "$waited" -lt 10 ] && [ -n "$(port_pids)" ]; do
      sleep 0.5
      waited=$((waited + 1))
    done
    pids="$(port_pids)"
    if [ -n "$pids" ]; then
      warn "优雅退出超时，强制结束 (pid: $(echo "$pids" | tr '\n' ' '))"
      kill -9 $pids 2>/dev/null || true
      sleep 1
    fi
    rounds=$((rounds + 1))
  done

  if [ -n "$(port_pids)" ]; then
    fail "无法释放端口 ${REDIS_PORT}，请手动检查：lsof -nP -iTCP:${REDIS_PORT} -sTCP:LISTEN"
  fi

  if [ "$changed" = "1" ]; then
    info "Redis 已停止"
  else
    info "Redis 未在运行（无需停止）"
  fi
}

# ---------------------------------------------------------------- 启动
start_redis() {
  local server
  server="$(resolve_redis_server)"
  [ -n "$server" ] || fail "未找到 redis-server，请先安装：brew install redis"

  if redis_ping; then
    info "Redis 已在 $REDIS_HOST:$REDIS_PORT 运行，跳过启动"
    return 0
  fi

  info "启动 Redis ${REDIS_PORT}（无持久化 + ${REDIS_POLICY} + ${REDIS_MAXMEMORY}）"
  "$server" \
    --port "$REDIS_PORT" \
    --bind "$REDIS_HOST" \
    --save "" \
    --appendonly no \
    --maxmemory "$REDIS_MAXMEMORY" \
    --maxmemory-policy "$REDIS_POLICY" \
    --daemonize yes \
    --pidfile "$PIDFILE" \
    --logfile "$LOGFILE" \
    || fail "redis-server 启动失败，见 $LOGFILE"

  wait_port "$REDIS_HOST" "$REDIS_PORT" 20 || fail "Redis 启动超时，见 $LOGFILE"
  sleep 0.2
  info "Redis 已启动（pid $(cat "$PIDFILE" 2>/dev/null || echo '?')）"
}

# ------------------------------------------------ 交还给 launchd（撤销卸载）
restore_launchd() {
  [ -f "$LAUNCHD_PLIST" ] || fail "找不到 ${LAUNCHD_PLIST}，无法恢复 launchd 托管"

  if launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; then
    info "launchd 服务 ${LAUNCHD_LABEL} 已在运行，无需恢复"
    return 0
  fi

  info "停止本脚本启动的 Redis 实例"
  stop_redis

  info "重新加载 ${LAUNCHD_LABEL}（恢复开机自启与 KeepAlive）"
  launchctl load -w "$LAUNCHD_PLIST" >/dev/null 2>&1 || true
  launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1 \
    || launchctl bootstrap "gui/$(id -u)" "$LAUNCHD_PLIST" >/dev/null 2>&1 \
    || true

  # 老 plist（含 LimitLoadToSessionType）在新 macOS 上可能加载失败，
  # 这里用一份现代格式的 plist 兜底：只写到 scripts/run/，不改动用户原文件。
  if ! launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; then
    local fallback="$RUN_DIR/redis-launchd.plist"
    local server
    server="$(resolve_redis_server)"
    info "原 plist 加载失败，改用现代格式 plist 兜底：$fallback"
    cat > "$fallback" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>KeepAlive</key>
	<true/>
	<key>Label</key>
	<string>${LAUNCHD_LABEL}</string>
	<key>ProgramArguments</key>
	<array>
		<string>${server}</string>
		<string>/opt/homebrew/etc/redis.conf</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>StandardErrorPath</key>
	<string>/opt/homebrew/var/log/redis.log</string>
	<key>StandardOutPath</key>
	<string>/opt/homebrew/var/log/redis.log</string>
	<key>WorkingDirectory</key>
	<string>/opt/homebrew/var</string>
</dict>
</plist>
PLIST
    launchctl bootstrap "gui/$(id -u)" "$fallback" >/dev/null 2>&1 || true
  fi

  if ! launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; then
    fail "launchctl 加载失败：当前 shell 看不到 launchd 用户域（launchctl list 为空），
      通常是因为不在 GUI 登录会话中。请在 Terminal.app 里手动执行：
        launchctl load -w ${LAUNCHD_PLIST}
      或者继续用本脚本自己管理（参数与模拟环境一致）：
        scripts/redis_restart.sh start"
  fi

  wait_port "$REDIS_HOST" "$REDIS_PORT" 20 || fail "launchd 托管启动超时，见 /opt/homebrew/var/log/redis.log"
  info "已交还 launchd 托管（使用 /opt/homebrew/etc/redis.conf，带 RDB 持久化）"
}

# ---------------------------------------------------------------- 主流程
echo "============================================================"
echo " TraceLens · Redis $REDIS_PORT 管理"
echo "============================================================"

case "$ACTION" in
  restart)
    enter_venv
    stop_redis
    start_redis
    ;;
  stop)
    stop_redis
    exit 0
    ;;
  start)
    enter_venv
    start_redis
    ;;
  status)
    enter_venv
    if ! redis_ping; then
      warn "Redis 未在 $REDIS_HOST:$REDIS_PORT 响应 PING"
      exit 1
    fi
    info "运行正常（PING → PONG）"
    ;;
  restore-launchd)
    enter_venv
    restore_launchd
    ;;
esac

echo "------------------------------------------------------------"
show_health || true
echo "------------------------------------------------------------"
echo " 重启整个模拟环境：scripts/sim.sh restart"
echo "============================================================"

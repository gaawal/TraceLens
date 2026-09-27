#!/usr/bin/env bash
# =============================================================================
# TraceLens 本地仿真环境 · 单一入口
# =============================================================================
# 一条命令拉起 / 停止 / 查看整个仿真栈，取代原来的 sim_up.sh / sim_down.sh /
# sim_status.sh。
#
#   scripts/sim.sh start   [组件...]   拉起（默认全部；已在跑的自动跳过）
#   scripts/sim.sh stop    [组件...]   停止（默认全部；Redis 非本脚本拉起的会跳过）
#   scripts/sim.sh restart [组件...]   重启
#   scripts/sim.sh status             状态总览
#   scripts/sim.sh deploy [--no-seed] 自动化模拟部署：上下位机互信 + 分步日志
#   scripts/sim.sh hosts              打印两台机器当前分配的地址
#   scripts/sim.sh alias              打印/检查「两个独立 IP」的 lo0 别名准备
#   scripts/sim.sh logs <组件> [-n N] 跟踪某个组件的日志
#   scripts/sim.sh init   [--fresh]   强制重建模拟资产（日志树 / CPD / ATLog 站）
#   scripts/sim.sh realign            把模拟日志的时间锚点对齐到当前时刻
#   scripts/sim.sh selftest           端到端自检
#
# 组件名（可任意挑选、顺序无关，脚本内部按依赖顺序执行）：
#   redis      Redis 7                     127.0.0.1:6379
#   assets     模拟资产（非守护进程，缺了才生成：日志树 + CPD + ATLog 用例站）
#   fleet      假 SSH/SFTP 机群            上位机与下位机**各自一个地址**（见下）
#   site       模拟 ATLog/CPD 报告站       网络IP:8901（同时绑 127.0.0.1）
#   stream     实时日志源（tail -f 效果）  每条流 0.5s 一行，2000 行滑动窗口
#   backend    Django API                  127.0.0.1:8000
#   watcher    实时监听/采集 worker         无端口，常驻 claim 监视器并写命中
#   frontend   Vite 前端                   127.0.0.1:5173
#   stt        本地语音转文字（可选，不在默认集合里）127.0.0.1:8001
#
# 关于"两个 IP"：上位机与下位机是两个不同的地址（互信模拟的前提）。
# 地址来源按优先级：环境变量 SIM_UPPER_HOST / SIM_LOWER_HOST →
# lo0 别名（127.0.0.2 / 127.0.0.3，需 sudo ifconfig lo0 alias … up）→
# 兜底为"局域网 IP + 127.0.0.1"。当前分配用 `scripts/sim.sh hosts` 查看。
#
# 选项：
#   --keep-redis    stop 时保留 Redis
#   --force-deps    强制重装前端依赖（node_modules 被裁剪/半损坏时用）
#   --force         stop redis 时忽略"不是本脚本拉起的"保护（慎用）
#   --no-seed       deploy 时只做互信，不写入环境资源（不动数据库）
#
# 例：
#   scripts/sim.sh start                    # 全栈
#   scripts/sim.sh start backend frontend   # 只起后端 + 前端
#   scripts/sim.sh restart stream           # 只重启实时日志源
#   scripts/sim.sh deploy                   # 跑一遍互信部署并打印每步日志
#   scripts/sim.sh stop --keep-redis        # 停仿真与前后端，留着 Redis
#   scripts/sim.sh logs stream
# =============================================================================
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
# 上游 env.sh 开了 -e；本脚本是编排器，必须容忍"该组件已在运行"这类非致命分支。
set +e

SCRIPTS_DIR="$PROJECT_DIR/scripts"
SPAWN="$SCRIPTS_DIR/_spawn.py"

info() { printf '\033[36m[sim]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m[sim]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[sim]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[sim]\033[0m %s\n' "$*" >&2; exit 1; }

KNOWN_COMPONENTS="redis assets fleet site stream backend watcher frontend stt"
DEFAULT_START="redis assets fleet site stream backend frontend"
DEFAULT_STOP="frontend backend stream site fleet redis"
# 启动顺序即依赖顺序：Redis → 资产 → 机群 → 报告站 → 日志源 → 后端 → 前端 → STT
START_ORDER="redis assets fleet site stream backend watcher frontend stt"
STOP_ORDER="stt frontend backend stream site fleet redis"

usage() { awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "${BASH_SOURCE[0]}"; }

# ---------------------------------------------------------------- 通用工具

ensure_venv() {
  [ -x "$PY" ] && return 0
  warn "后端虚拟环境缺失，自动创建：$BACKEND_DIR/.venv"
  [ -x "$UV" ] || fail "未找到 uv（${UV}）。请先运行 scripts/sim_setup.sh"
  local py
  py="$("$UV" python find 3.14 2>/dev/null || true)"
  if [ -z "$py" ]; then
    info "用 uv 下载 Python 3.14…"
    "$UV" python install 3.14 >/dev/null || fail "Python 3.14 下载失败"
    py="$("$UV" python find 3.14)"
  fi
  "$py" -m venv "$BACKEND_DIR/.venv" || fail "创建虚拟环境失败"
  info "安装后端依赖（首次较慢，请稍等）"
  # venv 里不一定有 pip，统一用 uv 往指定解释器装。
  "$UV" pip install --python "$PY" -r "$BACKEND_DIR/requirements.txt" >"$RUN_DIR/pip.log" 2>&1 \
    || fail "依赖安装失败，见 $RUN_DIR/pip.log"
  ok "后端环境就绪：$("$PY" -V)"
}

cli() { (cd "$BACKEND_DIR" && "$PY" -u -m simremote.cli "$@"); }

# 按命令行匹配停进程（守护型服务没有端口可用，例如实时监听 worker）。
stop_by_match() {
  local label="$1" pattern="$2"
  local pids
  pids="$(pgrep -f "$pattern" 2>/dev/null | tr '\n' ' ')"
  if [ -z "${pids// /}" ]; then
    info "$label 未在运行（跳过）"
    return 0
  fi
  info "停止 ${label}（pid ${pids// /,}）"
  for pid in $pids; do kill_tree "$pid"; done
  sleep 1
  pgrep -f "$pattern" >/dev/null 2>&1 && warn "$label 仍在运行"
  return 0
}

# 模拟机的**网络 IP**（对外服务绑它，用户也会照它访问）。
# 探测逻辑在 backend/simremote/fleet.public_host()：局域网 IP 优先，回环兜底；
# 不是 sim-upper.localhost 这类字符串主机名 —— 后端 ATLog 的 _is_private_host
# 会校验它是不是内网 IP，字符串或公网地址都会被打回。
machine_ip() {
  local ip=""
  if [ -x "$PY" ]; then
    ip="$( (cd "$BACKEND_DIR" && "$PY" -c 'from simremote import fleet; print(fleet.public_host())' 2>/dev/null) )"
  fi
  [ -n "$ip" ] || ip="127.0.0.1"
  printf '%s' "$ip"
}

# 两台机器各自的「key 地址 端口 名字」，一行一台。
# 上位机与下位机是**两个不同的 IP**（互信模拟的前提），所以探测、等待、打印
# 都要按各自的地址来 —— 共用一个 MACHINE_HOST 会让"下位机未就绪"这种误判出现。
machine_endpoints() {
  if [ -x "$PY" ]; then
    (cd "$BACKEND_DIR" && "$PY" -c '
from simremote import fleet
for spec in fleet.FLEET:
    print(spec.key, spec.host, spec.ssh_port, spec.name)
' 2>/dev/null)
  fi
}

# 把长跑进程放进独立会话启动（macOS 无 setsid，nohup 会被调用方进程组回收）。
spawn() {
  local name="$1" log="$2" cwd="$3"
  shift 3
  local pid
  if ! pid="$("$PY" -u "$SPAWN" --cwd "$cwd" --log "$log" -- "$@" 2>&1)"; then
    fail "$name 启动失败：$pid"
  fi
  printf '%s' "$pid"
}

# 杀掉进程及其直接子进程（vite 会派生 esbuild，只杀父进程会留孤儿）。
kill_tree() {
  local pid="$1"
  [ -n "$pid" ] || return 0
  pkill -TERM -P "$pid" 2>/dev/null
  kill -TERM "$pid" 2>/dev/null
  local i=0
  while [ "$i" -lt 25 ]; do
    pid_alive "$pid" || return 0
    sleep 0.2
    i=$((i + 1))
  done
  pkill -KILL -P "$pid" 2>/dev/null
  kill -KILL "$pid" 2>/dev/null
  return 0
}

stop_by_port() {
  local label="$1" port="$2"
  local pid
  pid="$(port_pid "$port")"
  if [ -z "$pid" ]; then
    info "$label 未在运行（跳过）"
    return 0
  fi
  info "停止 ${label}（pid ${pid}）"
  kill_tree "$pid"
  if port_open "$port"; then
    warn "$label 仍在监听 $port"
  fi
}

# ---------------------------------------------------------------- 启动各组件

c_start_redis() {
  if port_open 6379; then
    info "Redis 已在 6379 运行（跳过）"
    return 0
  fi
  local server
  server="$(resolve_redis_server)"
  [ -n "$server" ] || fail "未找到 redis-server，请先安装 Redis 7"
  info "启动 Redis 7（无持久化 + allkeys-lru，与 docker-compose 一致）"
  "$server" --port 6379 --save "" --appendonly no \
    --maxmemory 2gb --maxmemory-policy allkeys-lru \
    --daemonize yes --pidfile "$RUN_DIR/redis.pid" --logfile "$RUN_DIR/redis.log"
  wait_port 127.0.0.1 6379 20 || fail "Redis 启动超时，见 $RUN_DIR/redis.log"
  ok "Redis 就绪 127.0.0.1:6379"
}

c_start_assets() {
  if [ -d "$BACKEND_DIR/simremote/remote_fs" ]; then
    info "模拟资产已存在（要重建用 scripts/sim.sh init）"
    return 0
  fi
  info "生成模拟资产：日志树 + CPD 测校 + ATLog 用例站"
  cli init >"$RUN_DIR/init.out" 2>&1 || fail "资产生成失败，见 $RUN_DIR/init.out"
  ok "模拟资产就绪"
}

c_start_fleet() {
  local endpoints all_up=1 key host port name
  endpoints="$(machine_endpoints)"
  [ -n "$endpoints" ] || fail "取不到机器地址，先确认 backend/.venv 可用"

  # 每台机器都真的能在**自己的地址**上连上，才算已就绪。
  while read -r key host port name; do
    [ -n "$key" ] || continue
    port_open_on "$host" "$port" || all_up=0
  done <<<"$endpoints"
  if [ "$all_up" = 1 ]; then
    info "模拟机群已在运行（跳过）"
    return 0
  fi

  if port_open 2222 || port_open 2223; then
    # 端口开着但地址对不上：旧进程只绑了回环，或者两台机器共用了同一个地址。
    # 不重启的话，后端 SSH 会报 Unable to connect to port 22xx on <ip>。
    warn "机群在运行但地址不匹配（可能只绑了回环）；重启以绑好两个地址"
    stop_by_port "模拟机群" 2222
    stop_by_port "模拟机群" 2223
    sleep 0.5
  fi

  info "启动假 SSH/SFTP 机群（上位机与下位机各一个地址）"
  spawn fleet "$RUN_DIR/fleet.out" "$BACKEND_DIR" "$PY" -u -m simremote.cli serve >/dev/null
  while read -r key host port name; do
    [ -n "$key" ] || continue
    wait_port "$host" "$port" 30 || fail "$name SSH 未就绪（${host}:${port}），见 $RUN_DIR/fleet.out"
    ok "$name 就绪 ssh tracepilot@${host}:${port}  密码 tracelens"
  done <<<"$endpoints"
}

c_start_site() {
  local host
  host="$(machine_ip)"
  if port_open 8901; then
    if port_open_on "$host" 8901; then
      info "模拟报告站已在 ${host}:8901 运行（跳过）"
      return 0
    fi
    warn "模拟报告站只在回环地址上，未监听 ${host}；重启以绑定网络 IP"
    stop_by_port "模拟报告站" 8901
    sleep 0.5
  fi
  info "启动模拟报告站（ATLog 用例 + CPD 报告/数据表格）"
  spawn site "$RUN_DIR/site.out" "$BACKEND_DIR" "$PY" -u -m simremote.cli site-serve >/dev/null
  wait_port "$host" 8901 30 || fail "报告站未就绪（${host}:8901），见 $RUN_DIR/site.out"
  ok "报告站就绪 http://${host}:8901/"
}

c_start_stream() {
  if cli_running stream; then
    info "实时日志源已在运行（跳过）"
    return 0
  fi
  info "启动实时日志源（spwsp/wsp/mecore/cpfr/sil，每条流 0.5s 一行，2000 行滑动窗口）"
  spawn stream "$RUN_DIR/stream.out" "$BACKEND_DIR" \
    "$PY" -u -m simremote.cli stream --interval 0.5 >/dev/null
  sleep 2
  cli_running stream || fail "日志源未存活，见 $RUN_DIR/stream.out"
  ok "实时日志源就绪（scripts/sim.sh logs stream 可跟踪）"
}

c_start_backend() {
  if port_open 8000; then
    info "Django 后端已在 8000 运行（跳过）"
    return 0
  fi
  info "执行数据库迁移"
  (cd "$BACKEND_DIR" && "$PY" -u manage.py migrate --noinput >"$RUN_DIR/migrate.log" 2>&1) \
    || fail "迁移失败，见 $RUN_DIR/migrate.log"

  info "写入模拟环境资源（上位机 / 下位机 / 日志字典）"
  cli seed >"$RUN_DIR/seed.out" 2>&1 || fail "资源写入失败，见 $RUN_DIR/seed.out"
  # seed 会顺带打一堆 Django 日志，这里只挑 `  xxx : yyy` 形态的摘要行显示。
  grep -E '^  [^[:space:]]' "$RUN_DIR/seed.out" | sed 's/^/  /'

  info "启动 Django 后端 http://127.0.0.1:8000"
  spawn backend "$RUN_DIR/backend.out" "$BACKEND_DIR" \
    "$PY" -u -m uvicorn config.asgi:application \
    --host 127.0.0.1 --port 8000 --no-access-log >/dev/null
  wait_port 127.0.0.1 8000 60 || fail "后端启动超时，见 $RUN_DIR/backend.out"
  ok "后端就绪 http://127.0.0.1:8000/api/"
}

# 实时监听/采集的执行者：watch_worker 才是真正去 tail 机器日志、写 LogWatchHit 的进程。
# 少了它，前端「实时监听」看着一切正常（SSE 连着、任务在跑），但服务端一条命中都不会产生
# —— 采集面板永远是 0 条。它不是 Web 进程的一部分，必须单独常驻。
c_start_watcher() {
  if pgrep -f "manage.py watch_worker" >/dev/null 2>&1; then
    info "实时监听 worker 已在运行（跳过）"
    return 0
  fi
  info "启动实时监听 worker（claim 监视器 → tail 日志 → 写命中）"
  spawn watcher "$RUN_DIR/watch_worker.out" "$BACKEND_DIR" \
    "$PY" -u manage.py watch_worker --poll-seconds 1 >/dev/null
  sleep 2
  pgrep -f "manage.py watch_worker" >/dev/null 2>&1 \
    || fail "实时监听 worker 未存活，见 $RUN_DIR/watch_worker.out"
  ok "实时监听 worker 就绪（scripts/sim.sh logs watcher 可跟踪）"
}

# 前端依赖完整性：node_modules 有可能被"裁剪"成半成品（只少包、不报错），
# 这时 Vite 会在浏览器里报 Failed to resolve import。用 npm ls 兜住缺包的情况；
# 文件级损坏（包在但 dist 缺文件）用 --force-deps 强制重装。
ensure_frontend_deps() {
  local npm="$1" need=0
  if [ "$FORCE_DEPS" = "1" ]; then
    info "强制重装前端依赖"
    need=1
  elif [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    info "安装前端依赖（首次较慢）"
    need=1
  elif ! (cd "$FRONTEND_DIR" && "$npm" ls --depth=0 >/dev/null 2>&1); then
    warn "前端依赖不完整（node_modules 疑似被裁剪过），重新安装"
    need=1
  fi
  [ "$need" = "1" ] || return 0
  (cd "$FRONTEND_DIR" && "$npm" install --no-audit --no-fund >"$RUN_DIR/npm.log" 2>&1) \
    || fail "npm install 失败，见 $RUN_DIR/npm.log"
}

# Vite 端口通了不代表它健康：进程如果在 node_modules 被替换**之前**启动，
# 模块图还指着已删除的旧依赖，只在浏览器里报 Failed to resolve import。
frontend_serves() {
  curl -fsS -o /dev/null --max-time 5 http://127.0.0.1:5173/src/main.tsx 2>/dev/null
}

start_frontend_process() {
  local npm="$1"
  info "启动 Vite 前端 http://127.0.0.1:5173"
  spawn frontend "$RUN_DIR/frontend.out" "$FRONTEND_DIR" "$npm" run dev >/dev/null
  wait_port 127.0.0.1 5173 60 || fail "前端启动超时，见 $RUN_DIR/frontend.out"
  if frontend_serves; then
    ok "前端就绪 http://127.0.0.1:5173"
  else
    warn "前端端口通了，但 /src/main.tsx 解析失败。若是刚换过 node_modules，可跑"
    warn "  scripts/sim.sh restart frontend --force-deps"
  fi
}

c_start_frontend() {
  local npm
  npm="$(resolve_npm)"
  [ -n "$npm" ] || fail "未找到 npm"
  ensure_frontend_deps "$npm"

  if port_open 5173; then
    if frontend_serves; then
      info "Vite 前端已在 5173 运行（跳过）"
    else
      warn "5173 上的 Vite 响应异常（多半是跑在被替换掉的 node_modules 上），重启它"
      stop_by_port "Vite 前端" 5173
      start_frontend_process "$npm"
    fi
    return 0
  fi
  start_frontend_process "$npm"
}

c_start_stt() {
  if port_open 8001; then
    info "本地 STT 已在 8001 运行（跳过）"
    return 0
  fi
  [ -x "$BACKEND_DIR/.venv-stt/bin/python" ] || fail "未安装本地 STT，可先运行 scripts/stt_up.sh"
  info "启动本地语音转文字服务 http://127.0.0.1:8001"
  spawn stt "$RUN_DIR/stt.out" "$BACKEND_DIR" \
    "$BACKEND_DIR/.venv-stt/bin/python" -u start_stt.py --no-preload >/dev/null
  wait_port 127.0.0.1 8001 60 || warn "STT 未在 60s 内就绪，见 $RUN_DIR/stt.out"
}

# ---------------------------------------------------------------- 停止各组件

c_stop_redis() {
  if ! port_open 6379; then
    info "Redis 未在运行（跳过）"
    return 0
  fi
  local pid listener
  pid="$(read_pid "$RUN_DIR/redis.pid")"
  listener="$(port_pid 6379)"
  if [ "$FORCE" != "1" ] && { [ -z "$pid" ] || [ "$pid" != "$listener" ]; }; then
    warn "6379 上的 Redis（pid ${listener:-?}）不是本脚本拉起的，保持运行"
    warn "  确实要停：scripts/sim.sh stop redis --force"
    return 0
  fi
  info "停止 Redis（pid ${listener}）"
  kill "$listener" 2>/dev/null
  local i=0
  while [ "$i" -lt 25 ] && port_open 6379; do sleep 0.2; i=$((i + 1)); done
  rm -f "$RUN_DIR/redis.pid"
  port_open 6379 && warn "Redis 仍在监听 6379" || ok "Redis 已停止"
}

c_stop_fleet()  { cli stop        || warn "机群停止未完成，见上方提示"; }
c_stop_site()   { cli site-stop   || warn "报告站停止未完成，见上方提示"; }
c_stop_stream() { cli stream-stop || warn "日志源停止未完成，见上方提示"; }
c_stop_backend()  { stop_by_port "Django 后端" 8000; }
c_stop_watcher()  { stop_by_match "实时监听 worker" "manage.py watch_worker"; }
c_stop_frontend() { stop_by_port "Vite 前端" 5173; }
c_stop_stt()      { stop_by_port "本地 STT" 8001; }

start_component() {
  case "$1" in
    redis)    c_start_redis ;;
    assets)   c_start_assets ;;
    fleet)    c_start_fleet ;;
    site)     c_start_site ;;
    stream)   c_start_stream ;;
    backend)  c_start_backend ;;
    watcher)  c_start_watcher ;;
    frontend) c_start_frontend ;;
    stt)      c_start_stt ;;
  esac
}

stop_component() {
  case "$1" in
    assets)   return 0 ;;
    redis)    c_stop_redis ;;
    fleet)    c_stop_fleet ;;
    site)     c_stop_site ;;
    stream)   c_stop_stream ;;
    backend)  c_stop_backend ;;
    frontend) c_stop_frontend ;;
    stt)      c_stop_stt ;;
  esac
}

# ---------------------------------------------------------------- 收尾横幅

# 某台机器的「地址:端口」（供横幅与提示使用）。
machine_addr() {
  machine_endpoints | awk -v key="$1" '$1 == key { print $2":"$3; exit }'
}

# lo0 上已有的额外别名地址（没有则输出空）。
loopback_aliases() {
  if [ -x "$PY" ]; then
    (cd "$BACKEND_DIR" && "$PY" -c \
      'from simremote import fleet; print(" ".join(fleet.loopback_aliases()))' 2>/dev/null)
  fi
}

# 两个 IP 的准备提示。本机回环网段默认只有 127.0.0.1，加别名需要管理员权限，
# 所以这里只**引导**不代劳；加完之后两台机器就会各拿一个独立地址。
alias_hint() {
  local aliases
  aliases="$(loopback_aliases)"
  if [ -n "$aliases" ]; then
    ok "lo0 已有别名地址：${aliases}（上下位机各用一个）"
    return 0
  fi
  cat <<EOF

  提示：lo0 目前只有 127.0.0.1，两台模拟机只能**共用网卡地址**（靠端口区分）。
        想让上位机与下位机各拿一个**独立 IP**（互信更贴近真实机台），执行一次：

            sudo ifconfig lo0 alias 127.0.0.2 up
            sudo ifconfig lo0 alias 127.0.0.3 up

        然后重启机群：scripts/sim.sh restart fleet
        （不想加也行，功能不受影响：scripts/sim.sh deploy 照常可跑）

EOF
}

print_summary() {
  local host upper lower
  host="$(machine_ip)"
  upper="$(machine_addr upper)"
  lower="$(machine_addr lower1)"
  [ -n "$upper" ] || upper="${host}:2222"
  [ -n "$lower" ] || lower="${host}:2223"
  cat <<EOF

============================================================
 TraceLens 本地仿真环境
============================================================
 前端界面    http://127.0.0.1:5173
 后端 API    http://127.0.0.1:8000/api/
 接口文档    http://127.0.0.1:8000/api/docs/
 模拟报告站  http://${host}:8901/
 模拟上位机  ssh tracepilot@${upper}  密码 tracelens
 模拟下位机  ssh tracepilot@${lower}  密码 tracelens
             （两台机器各有独立 IP；互信部署：scripts/sim.sh deploy）

 日志：
   Redis      $RUN_DIR/redis.log
   机群       $RUN_DIR/fleet.out
   报告站     $RUN_DIR/site.out
   实时日志源 $RUN_DIR/stream.out
   后端       $RUN_DIR/backend.out
   前端       $RUN_DIR/frontend.out
   （跟踪：scripts/sim.sh logs <redis|fleet|site|stream|backend|frontend>）
EOF

  # 两台机器共用地址时提示一下：这是默认情形（lo0 没加别名），不是故障。
  if [ "${upper%%:*}" = "${lower%%:*}" ]; then
    printf '\n 提示：上位机与下位机共用 %s（靠端口区分）。想要两个独立 IP：scripts/sim.sh alias\n' \
      "${upper%%:*}"
  fi

  "$PY" - "$RUN_DIR/seed.out" "$BACKEND_DIR" <<'PY'
import re, sys
try:
    text = open(sys.argv[1], encoding="utf-8").read()
except OSError:
    print(" 环境名：SIM-EUV-01")
else:
    match = re.search(r"环境\s*:\s*(\S+)", text)
    print(f" 环境名：{match.group(1) if match else 'SIM-EUV-01'}")

# 流清单**从代码里取**，不写死在提示语里 —— 加一条流时这里自动跟着变，
# 否则提示会让用户去订阅一个已经不存在的模块（或漏掉新模块）。
sys.path.insert(0, sys.argv[2])
try:
    from simremote import livesim

    keys = " / ".join(item.module for item in livesim.TARGETS)
    count = len(livesim.TARGETS)
    every = f"{livesim.DEFAULT_INTERVAL_SECONDS:g}"
    cap = livesim.MAX_LIVE_LINES
    full_minutes = cap * livesim.DEFAULT_INTERVAL_SECONDS / 60
except Exception:  # noqa: BLE001 - 提示语而已，取不到就用兜底文案
    keys, count, every, cap, full_minutes = "spwsp / wsp / mecore / cpfr / sil", 5, "0.5", 2000, 16.7

print(f"""
 怎么找日志：
   前端 → 环境资源 → 远程日志查询，时间窗口选「最近 3 小时」即可命中
   实时日志：选 {keys} 任一模块后打开「实时监听」，
             日志会一行行滚出来（**每条流 {every}s 一行**，{count} 条流每 tick 各写一行，
             合计约 {count / float(every):g} 行/秒）
             每条流写满 {cap} 行（约 {full_minutes:.0f} 分钟）就滑动一格：整段收档、
             重建空文件，旧归档挪进回收站固定槽位覆盖 —— 日志量恒定有界，
             挂多久都不会把磁盘（以及读它的进程内存）堆满。
             注意头一行要等十几秒才出现：远端 tail -F 的 stdout 是管道（全缓冲），
             要攒满几 KB 才 flush 一次。真实机台同样如此，不是模拟器卡住了。
   点位日志：wsp 是工件台点位组件，日志正文是固定的六自由度点位行
             move absolute {{ "x":…, "y":…, "z":…, "rx":…, "ry":…, "rz":…, "status":"settled" }}
             花括号里是**合法 JSON**（键与字符串值都带双引号），
             整段 json.loads 就能取回字典；点位名在 "point" 字段里。
             （每个点位各是一次 MoveAbsolute 调用，三行一组：
               MoveAbsolute() >() enter … point=spiral_nn …
               MoveAbsolute() move absolute {{ … "point":"spiral_nn" … }}
               MoveAbsolute() <() leave … point=spiral_nn elapsed=… status=ok）
   CPD 测校：环境资源 → CPD 测校报告，选子系统 / 模块
   用例分析：ATLog 用例分析页粘贴下面的用例 URL""")
PY

  "$PY" - "$BACKEND_DIR" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
try:
    from simremote import atlog_site
except Exception as exc:  # 资产还没生成时不要挡住启动流程
    print(f" （暂无可粘贴 URL：{exc}）")
    raise SystemExit

print("  可直接粘贴的模拟 URL")
for item in atlog_site.case_urls():
    print(f"    用例[{item['status']:6s}] {item['url']}")
from simremote import fleet

if fleet.LOCAL_ROOT.exists():
    for item in atlog_site.cpd_urls()[:2]:
        print(f"    CPD 报告    {item['report_url']}")
        print(f"    CPD 表格    {item['data_url']}")
print(f"  报告站根目录 http://{fleet.public_host()}:8901/ （cpd/ 下是测校报告与数据表）")
PY

  # 时间锚点提醒：模拟日志的时间是「生成时刻」，放久了前端查最近窗口会空。
  "$PY" - "$BACKEND_DIR" <<'PY'
import sys
from datetime import datetime, timedelta

sys.path.insert(0, sys.argv[1])
try:
    from simremote import fleet

    path = fleet.remote_to_local(fleet.UPPER, f"{fleet.UPPER.debug_root}/spwsp/spwsp.log")
    with open(path, encoding="utf-8") as handle:
        stamps = [line[1:20] for line in handle if line.startswith("[")]
    last = datetime.fromisoformat(stamps[-1])
except Exception:
    raise SystemExit

lag = datetime.now() - last
if lag > timedelta(hours=6):
    hours = int(lag.total_seconds() // 3600)
    print(f" \033[33m注意\033[0m 模拟日志最新一条是 {last:%Y-%m-%d %H:%M}，已滞后 {hours} 小时。")
    print("      对齐时间线：scripts/sim.sh realign（实时日志源在跑的话不会滞后）")
PY

  echo "============================================================"
  echo " 状态：scripts/sim.sh status ｜ 停止：scripts/sim.sh stop"
  echo "============================================================"
}

print_status() {
  local host
  host="$(machine_ip)"
  # 中日韩字符占 2 列但算 3 字节，printf 的 %-Ns 按字节补空格会让表格参差不齐，
  # 这里自己算显示宽度：显示列数 = 字节数 - 宽字节数/3。
  # 列宽 26 是按最长的绑定地址留的：`127.0.0.1,192.168.1.10` 正好 22 列，
  # 双绑时不给够宽会把状态列直接顶到地址后面（连一个空格都没有）。
  local name_w=14 addr_w=26
  pad() {
    local text="$1" width="$2" bytes wide cols gap
    bytes=$(printf '%s' "$text" | wc -c)
    wide=$(printf '%s' "$text" | LC_ALL=C tr -cd '\200-\377' | wc -c)
    cols=$((bytes - wide / 3))
    printf '%s' "$text"
    gap=$((width - cols))
    while [ "$gap" -gt 0 ]; do printf ' '; gap=$((gap - 1)); done
  }

  row() {
    local name="$1" where="$2" port="$3"
    if port_open "$port"; then
      pad "$name" "$name_w"; pad "$where" "$addr_w"
      printf '\033[32m● 运行中\033[0m  pid %s\n' "$(port_pid "$port")"
    else
      pad "$name" "$name_w"; pad "$where" "$addr_w"
      printf '\033[31m○ 未运行\033[0m\n'
    fi
  }

  # 展示**实际绑定地址**而不是配置里的地址 —— 否则"配置写了网络 IP、进程只绑了
  # 回环"会被显示成一切正常，而前端用 IP 去连的时候才报错。
  row_bound() {
    local name="$1" port="$2" host="$3" addrs
    addrs="$(listen_addrs "$port")"
    pad "$name" "$name_w"
    if [ -z "$addrs" ]; then
      pad "-" "$addr_w"
      printf '\033[31m○ 未运行\033[0m\n'
      return 0
    fi
    pad "$addrs" "$addr_w"
    printf '\033[32m● 运行中\033[0m  pid %s' "$(port_pid "$port")"
    case ",${addrs}," in
      *",${host},"*) printf '\n' ;;
      *) printf '  \033[33m⚠ 未绑 %s\033[0m\n' "$host" ;;
    esac
  }

  pad "组件" "$name_w"; pad "绑定地址" "$addr_w"; printf '状态\n'
  row Redis 127.0.0.1:6379 6379
  row_bound 上位机SSH 2222 "$host"
  row_bound 下位机SSH 2223 "$host"
  row_bound 报告站 8901 "$host"
  row Django 127.0.0.1:8000 8000
  row STT 127.0.0.1:8001 8001
  row Vite 127.0.0.1:5173 5173
  # 日志源不监听端口，只能按 pidfile / 进程判断
  pad "实时日志源" "$name_w"; pad "持续追加 <fm>.log" "$addr_w"
  if cli_running stream; then
    printf '\033[32m● 运行中\033[0m  pid %s\n' \
      "$(read_pid "$BACKEND_DIR/simremote/run/stream.pid")"
  else
    printf '\033[31m○ 未运行\033[0m\n'
  fi
  # 实时监听/采集 worker：没有它，前端「实时监听」一切正常但服务端一条命中都不产生
  pad "实时监听worker" "$name_w"; pad "无端口（tail 机群日志）" "$addr_w"
  local watcher_pid
  watcher_pid="$(pgrep -f "manage.py watch_worker" 2>/dev/null | head -1)"
  if [ -n "$watcher_pid" ]; then
    printf '\033[32m● 运行中\033[0m  pid %s\n' "$watcher_pid"
  else
    printf '\033[31m○ 未运行（实时采集会一直 0 条）\033[0m\n'
  fi

  echo
  if [ -x "$PY" ]; then
    cli status || warn "simremote.cli status 执行失败"
  else
    warn "后端虚拟环境不存在（${PY}），跳过 simremote 明细。scripts/sim.sh start 会自动创建"
  fi
}

log_path_for() {
  case "$1" in
    redis)    echo "$RUN_DIR/redis.log" ;;
    fleet)    echo "$RUN_DIR/fleet.out" ;;
    site)     echo "$RUN_DIR/site.out" ;;
    stream)   echo "$RUN_DIR/stream.out" ;;
    backend)  echo "$RUN_DIR/backend.out" ;;
    watcher)  echo "$RUN_DIR/watch_worker.out" ;;
    frontend) echo "$RUN_DIR/frontend.out" ;;
    stt)      echo "$RUN_DIR/stt.out" ;;
    init)     echo "$RUN_DIR/init.out" ;;
    migrate)  echo "$RUN_DIR/migrate.log" ;;
    seed)     echo "$RUN_DIR/seed.out" ;;
    *)        return 1 ;;
  esac
}

show_logs() {
  local name="$1"; shift
  local target
  target="$(log_path_for "$name")" \
    || fail "未知日志：${name}（可选：redis fleet site stream backend watcher frontend stt init migrate seed）"
  [ -f "$target" ] || fail "日志还不存在：${target}"
  info "跟踪 ${target}（Ctrl-C 退出）"
  if [ "${#TAIL_ARGS[@]}" -gt 0 ]; then
    tail "${TAIL_ARGS[@]}" -f "$target"
  else
    tail -n 40 -f "$target"
  fi
}

# ---------------------------------------------------------------- 主流程

COMMAND=""
COMPONENTS=()
FORCE=0
FORCE_DEPS=0
KEEP_REDIS=0
FRESH=0
NO_SEED=0
TAIL_ARGS=()
SEED_ARG=""

while [ $# -gt 0 ]; do
  case "$1" in
    start|up|stop|down|restart|status|logs|init|realign|selftest|deploy|hosts|alias)
      [ -z "$COMMAND" ] || fail "只接受一个子命令，收到 $COMMAND 和 $1"
      COMMAND="$1"
      ;;
    --force) FORCE=1 ;;
    --force-deps) FORCE_DEPS=1 ;;
    --keep-redis) KEEP_REDIS=1 ;;
    --fresh) FRESH=1 ;;
    --no-seed) NO_SEED=1 ;;
    -h|--help) usage; exit 0 ;;
    -n) shift; TAIL_ARGS=(-n "${1:-40}") ;;
    -*) fail "未知选项：$1（用 -h 看用法）" ;;
    *) COMPONENTS+=("$1") ;;
  esac
  shift
done

[ -n "$COMMAND" ] || { usage; exit 0; }
[ "$COMMAND" = "up" ] && COMMAND=start
[ "$COMMAND" = "down" ] && COMMAND=stop

# 校验组件名
if [ "${#COMPONENTS[@]}" -gt 0 ]; then
  for item in "${COMPONENTS[@]}"; do
    case " $KNOWN_COMPONENTS " in
      *" $item "*) ;;
      *) fail "未知组件：${item}（可选：${KNOWN_COMPONENTS}）" ;;
    esac
  done
fi

# 只挑出用户点名的组件；内部一律按 START_ORDER / STOP_ORDER 的依赖顺序执行。
selected() {
  local item="$1" one
  for one in "${SELECTED[@]}"; do
    [ "$one" = "$item" ] && return 0
  done
  return 1
}

# 组件选择：给了参数就用参数，没给用该子命令的默认集合。
resolve_selection() {
  if [ "${#COMPONENTS[@]}" -gt 0 ]; then
    SELECTED=("${COMPONENTS[@]}")
  else
    # shellcheck disable=SC2206
    SELECTED=($1)
  fi
}

case "$COMMAND" in
  start)
    resolve_selection "$DEFAULT_START"
    [ -x "$PY" ] || ensure_venv
    for item in $START_ORDER; do
      selected "$item" || continue
      echo
      start_component "$item"
    done
    echo
    print_summary
    ;;

  stop)
    resolve_selection "$DEFAULT_STOP"
    if [ "$KEEP_REDIS" = "1" ]; then
      SELECTED=($(printf '%s\n' "${SELECTED[@]}" | grep -vx redis))
    fi
    for item in $STOP_ORDER; do
      selected "$item" || continue
      stop_component "$item"
    done
    ok "完成"
    ;;

  restart)
    resolve_selection "$DEFAULT_START"
    for item in $STOP_ORDER; do
      selected "$item" || continue
      stop_component "$item"
    done
    echo
    [ -x "$PY" ] || ensure_venv
    for item in $START_ORDER; do
      selected "$item" || continue
      echo
      start_component "$item"
    done
    echo
    print_summary
    ;;

  status)
    print_status
    ;;

  logs)
    [ "${#COMPONENTS[@]}" -gt 0 ] || fail "用法：scripts/sim.sh logs <组件> [-n N]"
    show_logs "${COMPONENTS[0]}"
    ;;

  init)
    [ -x "$PY" ] || ensure_venv
    if [ "$FRESH" = "1" ]; then
      info "清空并重建模拟文件系统"
      cli init --fresh || fail "资产生成失败"
    else
      info "重建模拟资产（日志树 / CPD / ATLog 站）"
      cli init || fail "资产生成失败"
    fi
    ok "完成"
    ;;

  realign)
    [ -x "$PY" ] || fail "后端虚拟环境不存在：$PY"
    exec "$SCRIPTS_DIR/sim_realign.sh"
    ;;

  hosts)
    [ -x "$PY" ] || fail "后端虚拟环境不存在：$PY"
    cli hosts
    ;;

  alias)
    [ -x "$PY" ] || fail "后端虚拟环境不存在：$PY"
    alias_hint
    ;;

  deploy)
    [ -x "$PY" ] || fail "后端虚拟环境不存在：$PY"
    if ! cli_running serve; then
      fail "模拟机群没在运行，先执行：scripts/sim.sh start fleet"
    fi
    echo
    info "自动化模拟部署（互信 + 分步日志）：上位机 ⇄ 下位机"
    echo
    if [ "$NO_SEED" = "1" ]; then
      cli deploy --no-seed
    else
      cli deploy
    fi
    echo
    ok "部署流程结束"
    ;;

  selftest)
    [ -x "$PY" ] || fail "后端虚拟环境不存在：$PY"
    cli selftest
    ;;
esac

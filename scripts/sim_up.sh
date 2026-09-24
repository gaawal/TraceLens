#!/usr/bin/env bash
# 一键拉起本地的 TraceLens 全栈演示环境：
#   Redis 7 → 模拟上下位机机群（假 SSH/SFTP）→ Django 后端 → Vite 前端
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

info() { printf '\033[36m[sim_up]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[sim_up]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[sim_up]\033[0m %s\n' "$*" >&2; exit 1; }

[ -x "$PY" ] || fail "未找到后端虚拟环境：$PY（先运行 scripts/sim_setup.sh）"

# ---------------------------------------------------------------- Redis 7
REDIS_CLI="$(resolve_redis_cli)"
REDIS_SERVER="$(resolve_redis_server)"
if "$REDIS_CLI" -p 6379 ping >/dev/null 2>&1; then
  info "Redis 已在 127.0.0.1:6379 运行（$( ("$REDIS_SERVER" --version 2>/dev/null || echo redis) | head -1)）"
else
  [ -n "$REDIS_SERVER" ] || fail "未找到 redis-server，请先安装 Redis 7"
  info "启动 Redis 7（无持久化 + allkeys-lru，与 docker-compose 配置一致）"
  "$REDIS_SERVER" --port 6379 --save "" --appendonly no \
    --maxmemory 2gb --maxmemory-policy allkeys-lru \
    --daemonize yes --pidfile "$RUN_DIR/redis.pid" --logfile "$RUN_DIR/redis.log"
  wait_port 127.0.0.1 6379 20 || fail "Redis 启动超时"
fi

# ------------------------------------------------- 模拟远程上下位机机群
if pgrep -f "simremote.cli serve" >/dev/null 2>&1; then
  info "模拟机群已在运行"
else
  info "生成模拟远程日志与资产"
  (cd "$BACKEND_DIR" && "$PY" -m simremote.cli init)
  info "启动假 SSH/SFTP 机群（上位机 2222 / 下位机 2223）"
  (cd "$BACKEND_DIR" && nohup "$PY" -m simremote.cli serve >"$RUN_DIR/fleet.out" 2>&1 &)
  wait_port 127.0.0.1 2222 30 || fail "模拟上位机 SSH 未就绪"
  wait_port 127.0.0.1 2223 30 || fail "模拟下位机 SSH 未就绪"
fi

# --------------------------------------------- 模拟 ATLog 报告站（HTTP）
if lsof -nP -iTCP:8901 -sTCP:LISTEN >/dev/null 2>&1; then
  info "模拟报告站已在 8901 端口运行"
else
  info "启动模拟报告站（ATLog 用例 + CPD 报告/数据表格）"
  (cd "$BACKEND_DIR" && nohup "$PY" -m simremote.cli site-serve >"$RUN_DIR/site.out" 2>&1 &)
  wait_port 127.0.0.1 8901 30 || fail "模拟报告站未就绪，见 $RUN_DIR/site.out"
fi

# ---------------------------------------------------------------- 后端
info "执行数据库迁移"
(cd "$BACKEND_DIR" && "$PY" manage.py migrate --noinput >"$RUN_DIR/migrate.log" 2>&1) || fail "迁移失败，见 $RUN_DIR/migrate.log"

info "写入模拟环境资源（上位机/下位机/日志字典）"
(cd "$BACKEND_DIR" && "$PY" -m simremote.cli seed >"$RUN_DIR/seed.json" 2>&1) || fail "资源写入失败，见 $RUN_DIR/seed.json"
"$PY" - "$RUN_DIR/seed.json" <<'PY'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as handle:
    text = handle.read()
start = text.find("{")
payload = json.loads(text[start:]) if start >= 0 else {}
print(f"  环境      : {payload.get('environment')} (#{payload.get('environment_id')}) 状态={payload.get('status_label')}")
print(f"  软件版本  : {payload.get('software_version')}")
print(f"  上位机    : {payload.get('upper')}")
for item in payload.get("lowers") or []:
    print(f"  下位机    : {item}")
print(f"  子系统/模块: {', '.join(payload.get('catalog_global') or []) or '（见日志）'}")
PY

if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  info "后端已在 8000 端口运行"
else
  info "启动 Django 后端 http://127.0.0.1:8000"
  (cd "$BACKEND_DIR" && nohup "$PY" -m uvicorn config.asgi:application \
      --host 127.0.0.1 --port 8000 --no-access-log >"$RUN_DIR/backend.out" 2>&1 &)
  wait_port 127.0.0.1 8000 40 || fail "后端启动超时，见 $RUN_DIR/backend.out"
fi

# ---------------------------------------------------------------- 前端
NPM="$(resolve_npm)"
[ -n "$NPM" ] || fail "未找到 npm"
if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  info "安装前端依赖（首次较慢）"
  (cd "$FRONTEND_DIR" && "$NPM" install --no-audit --no-fund >"$RUN_DIR/npm.log" 2>&1) || fail "npm install 失败，见 $RUN_DIR/npm.log"
fi
if lsof -nP -iTCP:5173 -sTCP:LISTEN >/dev/null 2>&1; then
  info "前端已在 5173 端口运行"
else
  info "启动 Vite 前端 http://127.0.0.1:5173"
  (cd "$FRONTEND_DIR" && nohup "$NPM" run dev >"$RUN_DIR/frontend.out" 2>&1 &)
  wait_port 127.0.0.1 5173 60 || fail "前端启动超时，见 $RUN_DIR/frontend.out"
fi

# ------------------------------------------------- 实时日志源（tail -f 型）
# 往 4 条 <fm>.log 持续追加合规日志，写满 1000 行就轮转重来，前端「实时监听」
# 直接就能看到日志一行行滚出来。必须用「子 shell + nohup」这种形态启动：
# 日志源是长跑进程，挂在调用方的进程组里会被一起回收（macOS 没有 setsid）。
if pgrep -f "simremote.cli stream" >/dev/null 2>&1; then
  info "实时日志源已在运行"
else
  info "启动实时日志源（spwsp / mecore / cpfr / sil，0.5s 一行，1000 行轮转）"
  (cd "$BACKEND_DIR" && nohup "$PY" -m simremote.cli stream --interval 0.5 >"$RUN_DIR/stream.out" 2>&1 &)
  sleep 2
  if pgrep -f "simremote.cli stream" >/dev/null 2>&1; then
    info "实时日志源已就绪，状态见：$PY -m simremote.cli stream-status"
  else
    warn "实时日志源启动失败，见 $RUN_DIR/stream.out"
  fi
fi

# ---------------------------------------------------------------- STT（可选）
if pgrep -f "start_stt.py" >/dev/null 2>&1; then
  info "本地语音转文字服务已在 8001 端口运行"
elif [ -x "$BACKEND_DIR/.venv-stt/bin/python" ]; then
  info "启动本地语音转文字服务 http://127.0.0.1:8001"
  (cd "$BACKEND_DIR" && nohup .venv-stt/bin/python start_stt.py --no-preload >"$RUN_DIR/stt.out" 2>&1 &)
else
  warn "未安装本地 STT（可选）：需要时运行 scripts/stt_up.sh"
fi

cat <<EOF

============================================================
 TraceLens 本地模拟环境已就绪
============================================================
 前端界面    http://127.0.0.1:5173
 后端 API    http://127.0.0.1:8000/api/
 接口文档    http://127.0.0.1:8000/api/docs/
 模拟报告站  http://127.0.0.1:8901/
 模拟上位机  ssh tracepilot@sim-upper.localhost:2222  密码 tracelens
 模拟下位机  ssh tracepilot@sim-lower1.localhost:2223 密码 tracelens
 后端运行日志 backend/runtime/tracelens.log
 机群运行日志 backend/simremote/run/fleet.log
 报告站日志  scripts/run/site.out
 实时日志源  scripts/run/stream.out（CLI: simremote.cli stream-status）

 查找日志：打开前端 → 环境资源选择 "$( "$PY" - "$RUN_DIR/seed.json" <<'PY'
import json, sys
text = open(sys.argv[1], encoding="utf-8").read()
start = text.find("{")
print(json.loads(text[start:]).get("environment", "SIM-EUV-01") if start >= 0 else "SIM-EUV-01")
PY
)" → 远程日志查询，时间窗口选最近 3 小时即可命中模拟日志。
 CPD 测校报告：环境资源 → CPD 测校报告，选子系统/模块即可看到报告与数据表格。
 用例 URL 分析：ATLog 用例分析页粘贴下面的用例 URL（或任意报告文件 URL）即可解析。
 验证实时日志：远程日志查询里选 spwsp / mecore / cpfr / sil 任一模块后开「实时监听」，
               日志会一行行滚出来（每 0.5s 一行，约 8 分钟写满 1000 行后自动轮转重来）。
 停止全部：scripts/sim_down.sh
 重启 Redis：scripts/redis_restart.sh
============================================================
EOF

# ------------------------------------------------ 可直接粘贴的模拟 URL
"$PY" - "$BACKEND_DIR" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from simremote import atlog_site

print("  可直接粘贴的模拟 URL")
for item in atlog_site.case_urls():
    print(f"    用例[{item['status']:6s}] {item['url']}")
cpd = atlog_site.cpd_urls()
for item in cpd[:2]:
    print(f"    CPD 报告    {item['report_url']}")
    print(f"    CPD 表格    {item['data_url']}")
print("  用例报告站根目录 http://127.0.0.1:8901/  （cpd/ 下是测校报告与数据表）")
PY

# ------------------------------------------------ 日志时间线陈旧提醒
# 日志的时间锚点是生成时刻。隔一段时间再来，<fm>.log 的末行就停在旧时间上，
# 前端选"最近 1/3 小时"会查不到东西。这里直接算滞后时长并给出补救命令。
"$PY" - <<PY
import sys
from datetime import datetime, timedelta

sys.path.insert(0, "$BACKEND_DIR")
from simremote import fleet

path = fleet.remote_to_local(fleet.UPPER, f"{fleet.UPPER.debug_root}/spwsp/spwsp.log")
try:
    with open(path, encoding="utf-8") as handle:
        stamps = [line[1:20] for line in handle if line.startswith("[")]
    last = datetime.fromisoformat(stamps[-1])
except (OSError, IndexError, ValueError):
    raise SystemExit(0) from None
lag = datetime.now() - last
if lag > timedelta(hours=6):
    hours = int(lag.total_seconds() // 3600)
    print(f"  \033[33m注意\033[0m 模拟日志最新一条是 {last:%Y-%m-%d %H:%M}，已滞后 {hours} 小时。")
    print("       对齐时间线：scripts/sim_realign.sh（实时日志源在跑的话不会滞后）")
PY

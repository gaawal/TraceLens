#!/usr/bin/env bash
# 把模拟日志的时间线重新对齐到"现在"。
#
# 为什么需要它：日志的时间锚点是生成时刻。放置一段时间后再来查询，
# `<fm>.log` 的末行就停在旧时间上，前端的"最近 1/3 小时"窗口会查不到东西。
# 本脚本按 停机群 -> 以当前时刻为锚重新生成 -> 起机群 -> 刷新后端索引
# 的顺序重建整条时间线，完成后任意时间窗都是连续覆盖的。
#
# 用法：
#   scripts/sim_realign.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

info() { printf '\033[36m[realign]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[realign]\033[0m %s\n' "$*" >&2; exit 1; }

[ -x "$PY" ] || fail "未找到后端虚拟环境：$PY（先运行 scripts/sim_setup.sh）"

info "停止模拟机群"
(cd "$BACKEND_DIR" && "$PY" -m simremote.cli stop) || true
# 等端口真正释放，避免新机群绑定时撞上还在 TIME_WAIT 的老进程。
for _ in $(seq 1 20); do
  lsof -nP -iTCP:2222 -sTCP:LISTEN >/dev/null 2>&1 || break
  sleep 0.5
done

info "以当前时刻为锚重新生成日志（覆盖写；跨天残留的旧日期文件会被分批清理）"
(cd "$BACKEND_DIR" && "$PY" -m simremote.cli init)

info "重新启动假 SSH/SFTP 机群（上位机 2222 / 下位机 2223）"
(cd "$BACKEND_DIR" && nohup "$PY" -m simremote.cli serve >"$RUN_DIR/fleet.out" 2>&1 &)
wait_port 127.0.0.1 2222 30 || fail "模拟上位机 SSH 未就绪，见 $RUN_DIR/fleet.out"
wait_port 127.0.0.1 2223 30 || fail "模拟下位机 SSH 未就绪，见 $RUN_DIR/fleet.out"

info "刷新后端日志索引"
if curl -fsS -m 3 -o /dev/null "http://127.0.0.1:8000/api/" 2>/dev/null; then
  (cd "$BACKEND_DIR" && "$PY" -m simremote.cli seed >"$RUN_DIR/seed.json" 2>&1) \
    || fail "索引刷新失败，见 $RUN_DIR/seed.json"
else
  info "后端未运行，跳过索引刷新；下次 scripts/sim_up.sh 会自动写入"
fi

info "完成：日志时间线已对齐到 $(date '+%Y-%m-%d %H:%M:%S')"

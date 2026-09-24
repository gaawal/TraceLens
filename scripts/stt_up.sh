#!/usr/bin/env bash
# 可选的本地语音转文字（STT）：独立虚拟环境 + faster-whisper 服务
# 该服务监听 127.0.0.1:8001，前端 AI 助手通过内置地址自动探测并显示麦克风。
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

info() { printf '\033[36m[stt_up]\033[0m %s\n' "$*"; }
STT_PY="$BACKEND_DIR/.venv-stt/bin/python"

if [ ! -x "$STT_PY" ]; then
  PY314="$("$UV" python find 3.14 2>/dev/null || "$PY")"
  info "创建 STT 虚拟环境 backend/.venv-stt（Python 3.14）"
  "$PY314" -m venv "$BACKEND_DIR/.venv-stt"
  info "安装 faster-whisper 依赖（首次会下载模型，需要网络）"
  "$STT_PY" -m pip install --upgrade pip -q
  "$STT_PY" -m pip install -r "$BACKEND_DIR/requirements-stt.txt"
fi

if pgrep -f "start_stt.py" >/dev/null 2>&1; then
  info "STT 已在运行"
else
  info "启动 STT 服务 http://127.0.0.1:8001（首次识别时加载模型）"
  (cd "$BACKEND_DIR" && nohup "$STT_PY" start_stt.py --no-preload >"$RUN_DIR/stt.out" 2>&1 &)
fi

for _ in $(seq 1 40); do
  # -f 必须加：没有它时 HTTP 4xx/5xx 的响应（例如反向代理返回的 502）也算 curl 成功，
  # 会把"服务根本没起来"误判为健康检查通过。
  if curl -fsS -m 1 http://127.0.0.1:8001/health >/dev/null 2>&1; then
    info "STT 健康检查通过：$(curl -fsS -m 2 http://127.0.0.1:8001/health)"
    exit 0
  fi
  sleep 1
done
echo "[stt_up] STT 未在 40s 内就绪，详见 $RUN_DIR/stt.out" >&2
exit 1

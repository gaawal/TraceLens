#!/usr/bin/env bash
# 一次性准备：Python 3.14 运行时 + 后端依赖 + 前端依赖
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

info() { printf '\033[36m[sim_setup]\033[0m %s\n' "$*"; }

# ---- Python 3.14（用 uv 的独立发行版，无需 sudo / 不污染系统 Python） ----
if [ ! -x "$UV" ]; then
  info "安装 uv（用户级）"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
PY314="$("$UV" python find 3.14 2>/dev/null || true)"
if [ -z "$PY314" ]; then
  info "下载 Python 3.14"
  "$UV" python install 3.14
  PY314="$("$UV" python find 3.14)"
fi
info "Python 3.14 路径：$PY314"

# ---- 后端虚拟环境 ----
if [ ! -x "$PY" ]; then
  info "创建后端虚拟环境 backend/.venv"
  "$PY314" -m venv "$BACKEND_DIR/.venv"
fi
info "后端 Python：$("$PY" -V)"
info "安装后端依赖（Django / DRF / paramiko / redis / langgraph / openai ...）"
"$PY" -m pip install --upgrade pip -q
"$PY" -m pip install -r "$BACKEND_DIR/requirements.txt"

# ---- 前端依赖 ----
NPM="$(resolve_npm)"
[ -n "$NPM" ] || { echo "未找到 npm，请先安装 Node.js" >&2; exit 1; }
info "安装前端依赖"
(cd "$FRONTEND_DIR" && "$NPM" install --no-audit --no-fund)

info "完成，接下来运行 scripts/sim.sh start"

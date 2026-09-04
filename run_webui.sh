#!/usr/bin/env bash
# ============================================================
#   MiniMax-H3 WebUI 一键启动脚本 (Linux / macOS)
#
#   Linux 版等价物，对齐 run_webui.bat 的启动流程:
#     [0] 若 WebUI 已在运行 -> 直接打开浏览器
#     [1] 检查 ComfyUI(8188)，未运行则后台拉起并等待就绪
#     [2] 后台启动 WebUI(8080) 并等待就绪
#     [3] 打开浏览器
#
#   用法:
#     ./run_webui.sh                       # 使用默认 ComfyUI 根目录
#     ./run_webui.sh /path/to/ComfyUI      # 指定 ComfyUI 根目录
#     ./run_webui.sh --port 9000           # 指定 WebUI 端口
#     ./run_webui.sh --stop                # 停止 WebUI 与 ComfyUI
#     ./run_webui.sh --status              # 查看运行状态
#     ./run_webui.sh --logs                # 查看日志
#
#   环境变量 (均可覆盖默认值):
#     H3_COMFY_ROOT    ComfyUI 根目录 (默认: ~/ComfyUI)
#     H3_PYTHON        Python 解释器 (默认: 自动探测 ComfyUI 的 venv / python3)
#     H3_WEBUI_HOST    WebUI 监听地址 (默认: 127.0.0.1)
#     H3_WEBUI_PORT    WebUI 端口 (默认: 8080)
#     H3_COMFY_PORT    ComfyUI 端口 (默认: 8188)
#     H3_INPUT_DIR     ComfyUI input 目录 (默认: <ComfyUI>/input)
#     H3_OUTPUT_DIR    ComfyUI output 目录 (默认: <ComfyUI>/output)
#
#   提示:
#     · 若 ComfyUI 用 conda 环境运行，请先 `conda activate <env>` 再执行，
#       或直接 `H3_PYTHON=/path/to/env/bin/python ./run_webui.sh`。
#     · 若 Triton JIT 编译报错，确认 gcc 在 PATH 中。
# ============================================================
set -uo pipefail

# ---- 默认值 ----
COMFY_ROOT="${H3_COMFY_ROOT:-$HOME/ComfyUI}"
WEBUI_HOST="${H3_WEBUI_HOST:-127.0.0.1}"
WEBUI_PORT="${H3_WEBUI_PORT:-8080}"
COMFY_PORT="${H3_COMFY_PORT:-8188}"

PID_WEBUI=/tmp/h3webui.pid
PID_COMFY=/tmp/comfyui.pid
LOG_WEBUI=/tmp/h3webui.log
LOG_COMFY=/tmp/comfyui.log

show_help() {
  sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
}

url_up() {
  local url="$1" max="${2:-2}"
  curl -fsS -m "$max" -o /dev/null "$url" >/dev/null 2>&1
}

open_browser() {
  local host="$WEBUI_HOST"; [ "$host" = "0.0.0.0" ] && host="127.0.0.1"
  local url="http://${host}:${WEBUI_PORT}"
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 &
  elif command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 &
  else
    echo "  (未找到 xdg-open/open，请手动打开 $url)"
  fi
}

do_stop() {
  echo "[stop] 停止 WebUI…"
  if [ -f "$PID_WEBUI" ]; then kill "$(cat "$PID_WEBUI")" 2>/dev/null; rm -f "$PID_WEBUI"; fi
  echo "[stop] 停止 ComfyUI…"
  if [ -f "$PID_COMFY" ]; then kill "$(cat "$PID_COMFY")" 2>/dev/null; rm -f "$PID_COMFY"; fi
  # 兜底 (仅匹配本仓库脚本拉起的进程)
  pkill -f "webui/server.py" 2>/dev/null || true
  pkill -f "main.py --listen 127.0.0.1" 2>/dev/null || true
  echo "[stop] done."
}

do_status() {
  echo "--- WebUI ---"
  if url_up "http://${WEBUI_HOST}:${WEBUI_PORT}/api/comfyui/status"; then
    echo "  运行中 http://${WEBUI_HOST}:${WEBUI_PORT}  (pid: $(cat "$PID_WEBUI" 2>/dev/null || echo '?'))"
  else
    echo "  已停止"
  fi
  echo "--- ComfyUI ---"
  if url_up "http://127.0.0.1:${COMFY_PORT}/system_stats"; then
    echo "  运行中 http://127.0.0.1:${COMFY_PORT}  (pid: $(cat "$PID_COMFY" 2>/dev/null || echo '?'))"
  else
    echo "  已停止"
  fi
}

do_logs() {
  echo "--- WebUI 日志 (末 30 行) ---"; tail -n 30 "$LOG_WEBUI" 2>/dev/null || echo "(无日志)"
  echo; echo "--- ComfyUI 日志 (末 30 行) ---"; tail -n 30 "$LOG_COMFY" 2>/dev/null || echo "(无日志)"
}

# ---- 解析参数 ----
while [ $# -gt 0 ]; do
  case "$1" in
    --port)
      if [ $# -lt 2 ]; then echo "[error] --port 需要一个值"; exit 1; fi
      WEBUI_PORT="$2"; shift 2 ;;
    --help|-h) show_help; exit 0 ;;
    --stop) do_stop; exit 0 ;;
    --status) do_status; exit 0 ;;
    --logs) do_logs; exit 0 ;;
    --*) echo "[error] 未知参数: $1"; exit 1 ;;
    *) COMFY_ROOT="$1"; shift ;;
  esac
done

# ---- 定位脚本目录 (可从任意目录运行) ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEBUI_PY="$SCRIPT_DIR/webui/server.py"

echo "============================================================"
echo "  MiniMax-H3 WebUI Launcher (Linux)"
echo "  ComfyUI root : $COMFY_ROOT"
echo "  WebUI        : http://${WEBUI_HOST}:${WEBUI_PORT}"
echo "  ComfyUI      : http://127.0.0.1:${COMFY_PORT}"
echo "============================================================"
echo

# ---- 校验 ----
if [ ! -f "$WEBUI_PY" ]; then
  echo "[error] 未找到 WebUI 脚本: $WEBUI_PY"; exit 1
fi
if [ ! -d "$COMFY_ROOT" ]; then
  echo "[error] ComfyUI 目录不存在: $COMFY_ROOT"
  echo "  请作为第一个参数传入，或设置 H3_COMFY_ROOT，例如:"
  echo "    $0 /path/to/ComfyUI"
  exit 1
fi

# ---- 定位 ComfyUI main.py 与实际 base 目录 ----
#   Linux 标准布局: <root>/main.py ; 兼容 Windows 嵌套布局: <root>/ComfyUI/main.py
if [ -f "$COMFY_ROOT/main.py" ]; then
  COMFY_BASE="$COMFY_ROOT"
elif [ -f "$COMFY_ROOT/ComfyUI/main.py" ]; then
  COMFY_BASE="$COMFY_ROOT/ComfyUI"
else
  echo "[error] 在 $COMFY_ROOT 下未找到 main.py (既不在根目录，也不在 ComfyUI/ 子目录)"
  exit 1
fi
MAIN_PY="$COMFY_BASE/main.py"

# ---- 探测 Python 解释器 ----
pick_python() {
  local c
  if [ -n "${H3_PYTHON:-}" ] && [ -x "$H3_PYTHON" ]; then echo "$H3_PYTHON"; return 0; fi
  for c in \
    "$COMFY_BASE/venv/bin/python" "$COMFY_ROOT/venv/bin/python" \
    "$COMFY_BASE/.venv/bin/python" "$COMFY_ROOT/.venv/bin/python"; do
    [ -x "$c" ] && { echo "$c"; return 0; }
  done
  command -v python3 && return 0
  command -v python && return 0
  return 1
}
PY="$(pick_python)" || {
  echo "[error] 找不到可用的 Python 解释器。"
  echo "  若 ComfyUI 用 conda 环境，请先 activate 该环境，或设置 H3_PYTHON=/path/to/python"
  exit 1
}

# ---- input/output 目录 (WebUI 会把上传图/截帧复制到这里) ----
INPUT_DIR="${H3_INPUT_DIR:-$COMFY_BASE/input}"
OUTPUT_DIR="${H3_OUTPUT_DIR:-$COMFY_BASE/output}"
mkdir -p "$INPUT_DIR" "$OUTPUT_DIR"

echo "  Python       : $PY ($($PY --version 2>&1))"
echo "  ComfyUI base : $COMFY_BASE"
echo "  input / output: $INPUT_DIR / $OUTPUT_DIR"
echo

# ---- [0] WebUI 已在运行 -> 直接开浏览器 ----
if url_up "http://${WEBUI_HOST}:${WEBUI_PORT}/"; then
  echo "  WebUI 已在运行，直接打开浏览器…"
  open_browser
  exit 0
fi

# ---- [1] 确保 ComfyUI 运行 ----
echo "[1/3] 检查 ComfyUI (127.0.0.1:${COMFY_PORT})…"
if url_up "http://127.0.0.1:${COMFY_PORT}/system_stats" 3; then
  echo "  ComfyUI 已在运行。"
else
  echo "  ComfyUI 未运行，后台启动中…"
  cd "$COMFY_BASE"
  # 把 Python 所在 bin 前置到 PATH: conda/venv 里的 gcc(供 Triton JIT 用)、ffmpeg 等才能被找到
  nohup env PATH="$(dirname "$PY"):$PATH" VHS_USE_IMAGEIO_FFMPEG=1 \
    "$PY" "$MAIN_PY" \
      --listen 127.0.0.1 --port "$COMFY_PORT" \
      --input-directory "$INPUT_DIR" \
      --output-directory "$OUTPUT_DIR" \
      --disable-auto-launch \
      > "$LOG_COMFY" 2>&1 < /dev/null &
  comfy_pid=$!
  disown "$comfy_pid" 2>/dev/null || true
  echo "$comfy_pid" > "$PID_COMFY"
  echo "  ComfyUI PID: $comfy_pid (日志: $LOG_COMFY)"

  echo "  等待 ComfyUI 就绪 (最长约 160s)…"
  tries=0; max=80
  while [ "$tries" -lt "$max" ]; do
    tries=$((tries + 1))
    if url_up "http://127.0.0.1:${COMFY_PORT}/system_stats" 2; then
      echo "  ComfyUI 就绪 (${tries} 次探测)。"; break
    fi
    if ! kill -0 "$comfy_pid" 2>/dev/null; then
      echo; echo "  [error] ComfyUI 进程已退出，日志末尾:"; tail -n 30 "$LOG_COMFY"; exit 1
    fi
    sleep 2
    printf "  ...等待 [%d/%d]\r" "$tries" "$max"
  done
  if ! url_up "http://127.0.0.1:${COMFY_PORT}/system_stats" 2; then
    echo; echo "  [error] ComfyUI 未在 ${max} 次探测内就绪。请手动启动后再运行本脚本。"; exit 1
  fi
  echo
fi

# ---- [2] 后台启动 WebUI ----
echo "[2/3] 启动 WebUI (${WEBUI_HOST}:${WEBUI_PORT})…"
cd "$SCRIPT_DIR"
nohup env PATH="$(dirname "$PY"):$PATH" \
  COMFYUI_URL="http://127.0.0.1:${COMFY_PORT}" \
  COMFYUI_INPUT="$INPUT_DIR" \
  COMFYUI_OUTPUT="$OUTPUT_DIR" \
  H3WEBUI_HOST="$WEBUI_HOST" \
  H3WEBUI_PORT="$WEBUI_PORT" \
  "$PY" "$WEBUI_PY" \
    > "$LOG_WEBUI" 2>&1 < /dev/null &
WEBUI_PID=$!
disown "$WEBUI_PID" 2>/dev/null || true
echo "$WEBUI_PID" > "$PID_WEBUI"

tries=0
while [ "$tries" -lt 30 ]; do
  tries=$((tries + 1))
  if url_up "http://${WEBUI_HOST}:${WEBUI_PORT}/api/comfyui/status" 2; then
    echo "  WebUI 就绪 (PID $WEBUI_PID, 日志: $LOG_WEBUI)。"; break
  fi
  if ! kill -0 "$WEBUI_PID" 2>/dev/null; then
    echo "  [error] WebUI 进程已退出，日志末尾:"; tail -n 30 "$LOG_WEBUI"; exit 1
  fi
  sleep 1
done
if ! url_up "http://${WEBUI_HOST}:${WEBUI_PORT}/api/comfyui/status" 2; then
  echo "  [error] WebUI 未响应，日志末尾:"; tail -n 20 "$LOG_WEBUI"; exit 1
fi

# ---- [3] 打开浏览器 ----
echo "[3/3] 打开浏览器…"
sleep 1
open_browser

echo
echo "============================================================"
echo "  WebUI   : http://${WEBUI_HOST}:${WEBUI_PORT}"
echo "  ComfyUI : http://127.0.0.1:${COMFY_PORT}"
echo
echo "  停止:      $0 --stop    (同时停止 WebUI 与 ComfyUI)"
echo "  状态:      $0 --status"
echo "  日志:      $0 --logs"
echo "  WebUI pid:  $(cat "$PID_WEBUI" 2>/dev/null || echo '?')   (日志: $LOG_WEBUI)"
echo "  ComfyUI pid: $(cat "$PID_COMFY" 2>/dev/null || echo '?')   (日志: $LOG_COMFY)"
echo "============================================================"

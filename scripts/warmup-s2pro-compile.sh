#!/usr/bin/env bash
# warmup-s2pro-compile.sh — s2-pro torch.compile 预热 (幂等, 作为流水线第一步自动调用)
#
# 背景: s2-pro 在 llama-swap config.yaml 里带 --compile。首次冷启动时 warm_up
#   里的第一次 decode 会触发 inductor 编译(几分钟), 超过 llama-swap
#   healthCheckTimeout(120s) 导致加载失败。本脚本手动跑一次完整 warmup,
#   编译产物落盘 ~/.cache/torch/inductor (fx_graph_cache=True), 之后
#   llama-swap 加载 s2-pro 秒级就绪。
#
# 幂等: marker 匹配 (torch 版本+模型路径) 则毫秒级跳过。
#   marker: ~/.cache/s2pro-compile-warmup.done
#   若手动清了 ~/.cache/torch/inductor, 删 marker 强制重新预热。
#
# GPU 占用处理: 先等 360s (llama-swap TTL=300s 会自动卸载空闲 LLM; 等待窗口
#   覆盖 OpenClaw/Claude Code agent 的在途请求, 避免 unload 打断正在进行的
#   LLM 调用); 仍占用才主动 POST /api/models/unload。
set -euo pipefail

S2PRO_DIR=/home/loomz/workspace/money_code/videotrans/s2-pro
FISH_PY="$S2PRO_DIR/fish-speech/.venv/bin/python"
PORT=5999
SWAP_BASE="${SWAP_BASE:-http://127.0.0.1:8080}"
MARKER="/home/loomz/.cache/s2pro-compile-warmup.done"
NEED_MB=20000
TIMEOUT=900  # warmup 上限 15min (冷编译通常 3-8min)

gpu_free_mb() {
  nvidia-smi --query-gpu=memory.total,memory.used --format=csv,noheader,nounits | awk -F', *' '{print $1-$2}'
}

# --- 快路径: marker 匹配 → 跳过 ---
key="$("$FISH_PY" -c 'import torch;print(torch.__version__)' 2>/dev/null || echo notorch)|$S2PRO_DIR"
if [[ -f "$MARKER" ]] && [[ "$(cat "$MARKER" 2>/dev/null)" == "$key" ]]; then
  echo "warmup: marker 匹配 (inductor 缓存已就绪), 跳过"
  exit 0
fi

# --- 等 GPU 空闲 (≥20GB) ---
free_mb=$(gpu_free_mb)
waited=0
while (( free_mb < NEED_MB && waited < 360 )); do
  sleep 10; waited=$(( waited + 10 ))
  free_mb=$(gpu_free_mb)
done
if (( free_mb < NEED_MB )); then
  echo "warmup: GPU 空闲 ${free_mb}MB < ${NEED_MB}MB, 等 360s 未释放, 主动卸载 llama-swap 全部模型"
  curl -sf -m 10 -X POST "$SWAP_BASE/api/models/unload" >/dev/null 2>&1 \
    || { echo "FATAL: unload 调用失败: $SWAP_BASE/api/models/unload" >&2; exit 1; }
  for (( i=0; i<12 && free_mb < NEED_MB; i++ )); do
    sleep 5
    free_mb=$(gpu_free_mb)
  done
fi
(( free_mb >= NEED_MB )) || { echo "FATAL: GPU 空闲 ${free_mb}MB < ${NEED_MB}MB (卸载后仍未释放, nvidia-smi 手动检查)" >&2; exit 1; }
echo "warmup: GPU 空闲 ${free_mb}MB, 开始 torch.compile 预热 (首次约 5-15min)..."

# --- 跑 warmup (独立进程组, uvicorn spawn worker 需杀整组) ---
LOG="/tmp/s2pro-warmup-$(date +%Y%m%d-%H%M%S).log"
setsid "$FISH_PY" "$S2PRO_DIR/fish-speech/tools/api_server.py" \
  --llama-checkpoint-path "$S2PRO_DIR" \
  --decoder-checkpoint-path "$S2PRO_DIR/codec.pth" \
  --half --compile \
  --listen 127.0.0.1:$PORT >"$LOG" 2>&1 &
PID=$!
ok=0
for (( t=0; t<TIMEOUT; t+=5 )); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "FATAL: api_server 提前退出, 日志末尾:" >&2
    tail -20 "$LOG" >&2
    break
  fi
  if grep -q "Models warmed up." "$LOG"; then ok=1; break; fi
  sleep 5
done
kill -- -"$PID" 2>/dev/null || kill "$PID" 2>/dev/null || true
sleep 3
kill -9 -- -"$PID" 2>/dev/null || true

if (( ok )); then
  mkdir -p "$(dirname "$MARKER")"
  echo "$key" > "$MARKER"
  echo "warmup: 完成, marker 已写入 $MARKER (inductor 缓存在 ~/.cache/torch/inductor)"
else
  echo "FATAL: ${TIMEOUT}s 内未完成预热, 详见 $LOG" >&2
  exit 1
fi

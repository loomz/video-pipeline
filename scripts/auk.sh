#!/usr/bin/env bash
# auk.sh — AuK 语音生成/编辑包装器
# 流程: 预检(ckpt/编码器/参数) → 等 GPU 空闲(必要时主动卸载 LLM) → auk-infer --cpu_offload → 校验输出
# 硬约束: --cpu_offload 必须(24GB 卡, 官方峰值 25GB, offload 后 ~17GB); 与 LLM 互斥
set -euo pipefail

AUK_DIR="/home/loomz/workspace/money_code/videotrans/AuK"
AUK_INFER="$AUK_DIR/.venv/bin/auk-infer"
SWAP_BASE="${SWAP_BASE:-http://127.0.0.1:8080}"
OUT_DIR="${AUK_OUT_DIR:-/home/loomz/视频/outputs/auk}"
NEED_MB=18000  # cpu_offload 后 ~17GB + 余量

INSTRUCTION="" AUDIO="" OUTPUT="" GEN_SECONDS="" GEN_TEXT="" REF_TEXT=""
FLASH=0 SEED=""

usage() {
  cat << 'USAGE'
用法:
  auk.sh --instruction "..." [--audio in.wav] [选项]

必选:
  --instruction TEXT   自然语言任务指令 (模板见 SKILL.md)

选项:
  --audio WAV          输入/参考音频 (TTS/编辑任务必给; Instruct TTS 省略)
  --output PATH        输出 wav (默认: $OUT_DIR/<时间戳>_<base|flash>.wav)
  --gen_seconds N      目标时长秒 (无 --audio 时必填; TTS 按文本估算, 中文约4字/秒)
  --gen_text TEXT      目标文本 (配 --ref_text 自动估时长)
  --ref_text TEXT      --audio 的文字稿 (配 --gen_text)
  --flash              用 AuK-Flash (4步, 快); 默认 AuK base (32步, 质量高)
  --seed N             随机种子
  -h, --help           帮助
USAGE
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --instruction) INSTRUCTION="$2"; shift 2 ;;
    --audio) AUDIO="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --gen_seconds) GEN_SECONDS="$2"; shift 2 ;;
    --gen_text) GEN_TEXT="$2"; shift 2 ;;
    --ref_text) REF_TEXT="$2"; shift 2 ;;
    --flash) FLASH=1; shift ;;
    --seed) SEED="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "未知选项: $1" >&2; usage ;;
  esac
done
[[ -n "$INSTRUCTION" ]] || { echo "FATAL: 缺少 --instruction" >&2; usage; }

# ---------- 预检 ----------
for c in ffprobe curl nvidia-smi; do command -v "$c" >/dev/null || { echo "FATAL: 缺少命令: $c" >&2; exit 1; }; done
[[ -x "$AUK_INFER" ]] || { echo "FATAL: 找不到 auk-infer: $AUK_INFER" >&2; exit 1; }
if [[ "$FLASH" == 1 ]]; then
  CKPT="$AUK_DIR/ckpts/AuK-Flash/auk_flash.safetensors"; MODEL=flash
else
  CKPT="$AUK_DIR/ckpts/AuK/auk_base.safetensors"; MODEL=base
fi
[[ -f "$CKPT" ]] || { echo "FATAL: 权重缺失: $CKPT" >&2; exit 1; }
QWEN="$AUK_DIR/ckpts/Qwen2.5-Omni-3B"
[[ -d "$QWEN" ]] || { echo "FATAL: Qwen2.5-Omni-3B 编码器缺失: $QWEN" >&2; exit 1; }
[[ -n "$AUDIO" || -n "$GEN_SECONDS" ]] || { echo "FATAL: 无 --audio 时必须给 --gen_seconds (Instruct TTS 需指定时长)" >&2; exit 1; }
if [[ -n "$AUDIO" && ! -f "$AUDIO" ]]; then echo "FATAL: 音频文件不存在: $AUDIO" >&2; exit 1; fi

mkdir -p "$OUT_DIR"
[[ -n "$OUTPUT" ]] || OUTPUT="$OUT_DIR/$(date +%Y%m%d_%H%M%S)_$MODEL.wav"
LOG="${OUTPUT%.wav}.log"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# 等 GPU 显存释放 (AuK cpu_offload ~17GB; LLM 23.7GB 互斥, 占卡时主动卸载)
wait_gpu_free() {
  local need_mb="$1" waited=0 free_mb
  free_mb=$(nvidia-smi --query-gpu=memory.total,memory.used --format=csv,noheader,nounits | awk -F', *' '{print $1-$2}')
  (( free_mb >= need_mb )) && return 0
  log "GPU 空闲 ${free_mb}MB < ${need_mb}MB, 等待释放 (llama-swap 空闲模型 300s 自动卸载)..."
  while (( waited < 300 )); do
    sleep 10; waited=$(( waited + 10 ))
    free_mb=$(nvidia-smi --query-gpu=memory.total,memory.used --format=csv,noheader,nounits | awk -F', *' '{print $1-$2}')
    (( free_mb >= need_mb )) && { log "GPU 已释放: 空闲 ${free_mb}MB"; return 0; }
    if (( waited == 60 )); then
      log "等待 60s 仍被占用, 主动卸载 llama-swap 全部模型"
      curl -sf -m 10 -X POST "$SWAP_BASE/api/models/unload" >/dev/null 2>&1 \
        || log "WARN: unload 调用失败, 继续等待"
    fi
  done
  echo "FATAL: GPU 显存不足: 空闲 ${free_mb}MB < ${need_mb}MB (等待 300s 超时, 请 nvidia-smi 手动检查)" >&2
  exit 1
}

wait_gpu_free "$NEED_MB"

ARGS=(--ckpt "$CKPT" --qwen_path "$QWEN" --cpu_offload --instruction "$INSTRUCTION" --output "$OUTPUT")
[[ -n "$AUDIO" ]] && ARGS+=(--audio "$AUDIO")
[[ -n "$GEN_SECONDS" ]] && ARGS+=(--gen_seconds "$GEN_SECONDS")
[[ -n "$GEN_TEXT" ]] && ARGS+=(--gen_text "$GEN_TEXT")
[[ -n "$REF_TEXT" ]] && ARGS+=(--ref_text "$REF_TEXT")
[[ -n "$SEED" ]] && ARGS+=(--seed "$SEED")

log "=== auk.sh 启动: 模型=$MODEL 音频=${AUDIO:-<无>} 输出=$OUTPUT ==="
log "指令: $INSTRUCTION"
( cd "$AUK_DIR" && "$AUK_INFER" "${ARGS[@]}" ) >>"$LOG" 2>&1 \
  || { log "FATAL: auk-infer 失败, 详见 $LOG"; exit 1; }
[[ -f "$OUTPUT" ]] || { log "FATAL: 输出文件缺失: $OUTPUT"; exit 1; }
dout=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUTPUT")
log "=== 完成: $OUTPUT (${dout}s) ==="

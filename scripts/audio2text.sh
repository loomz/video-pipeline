#!/usr/bin/env bash
# audio2text.sh — 单个音频文件: ASR(stt) → 翻译(sts) → 中文文本 (不合成视频/TTS)
# 用法: audio2text.sh <workdir> <audio_full_path> [src_lang]
#   workdir     输出目录 (en.srt / zh-cn.srt / translate.log / translate.status)
#   audio_full_path  输入音频文件 (m4a/mp3/wav/...)
#   src_lang      源语言码 (默认 en; 可选 zh-cn/yue/ko/ja/auto, 见 config.env / LANG_CODE)
# 状态: translate.status = running → done | failed; 日志写 translate.log (stdout 亦被管道捕获)
# 复用 dub.sh 的 wait_gpu_free + pyvideotrans stt/sts 调用 (config.env 提供模型/路径)
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
PIPELINE_DIR=$(dirname "$SCRIPT_DIR")
# shellcheck source=/dev/null
source "$PIPELINE_DIR/config.env"

WORKDIR="" AUDIO="" SRC="en"
if [[ $# -ge 2 ]]; then WORKDIR="$1"; AUDIO="$2"; fi
if [[ $# -ge 3 ]]; then SRC="$3"; fi
[[ -n "$WORKDIR" && -n "$AUDIO" ]] || { echo "用法: audio2text.sh <workdir> <audio_full_path> [src_lang]" >&2; exit 2; }
[[ -f "$AUDIO" ]] || { echo "FATAL: 音频文件不存在: $AUDIO" >&2; exit 2; }
mkdir -p "$WORKDIR"

LOG_FILE="$WORKDIR/translate.log"
STATUS_FILE="$WORKDIR/translate.status"
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG_FILE"; }
echo "running" > "$STATUS_FILE"

# 等 GPU 显存释放 (faster-whisper large-v3 需 ~5GB; LLM 占卡时主动卸载防 OOM)
# 与 dub.sh:86-102 相同
wait_gpu_free() {
  local need_mb="${1:-5000}" waited=0 free_mb
  free_mb=$(nvidia-smi --query-gpu=memory.total,memory.used --format=csv,noheader,nounits | awk -F', *' '{print $1-$2}')
  (( free_mb >= need_mb )) && return 0
  log "GPU 空闲 ${free_mb}MB < ${need_mb}MB, 等待释放..."
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
  log "FATAL: GPU 显存不足 (等待 300s 超时)"
  return 1
}

run_all() {
  # 1) ASR → <workdir>/<sanitized-stem>.srt (pyvideotrans 把文件名里空格/点换成 -)
  wait_gpu_free 5000
  log "  [1/2] ASR (src=$SRC, model=$ASR_MODEL) → srt"
  ( cd "$PYVIDEOTRANS_DIR" && uv run cli.py \
      --task stt \
      --name "$AUDIO" \
      --output-dir "$WORKDIR" \
      --source_language_code "$SRC" \
      --model_name "$ASR_MODEL" \
      --cuda \
      --no-clear-cache ) >>"$LOG_FILE" 2>&1
  # 此时 workdir 里恰好一个 .srt, 取它重命名成 en.srt
  local srt
  srt=$(ls "$WORKDIR"/*.srt 2>/dev/null | head -1 || true)
  [[ -n "$srt" ]] || { log "FATAL: ASR 后未生成 srt"; return 1; }
  mv "$srt" "$WORKDIR/en.srt"

  # 2) 翻译 → <workdir>/en.zh-cn.srt → zh-cn.srt (与 dub.sh:140-149 的 run_sts 相同)
  log "  [2/2] 翻译 (sts: $SRC → zh-cn, type=$TRANSLATE_TYPE)"
  ( cd "$PYVIDEOTRANS_DIR" && uv run cli.py \
      --task sts \
      --name "$WORKDIR/en.srt" \
      --output-dir "$WORKDIR" \
      --source_language_code "$SRC" \
      --target_language_code zh-cn \
      --translate_type "$TRANSLATE_TYPE" \
      --no-clear-cache ) >>"$LOG_FILE" 2>&1
  [[ -f "$WORKDIR/zh-cn.srt" ]] || mv "$WORKDIR/en.zh-cn.srt" "$WORKDIR/zh-cn.srt" 2>/dev/null
  [[ -f "$WORKDIR/zh-cn.srt" ]] || { log "FATAL: 翻译后未生成 zh-cn.srt"; return 1; }
  log "完成: en.srt + zh-cn.srt"
}

if run_all; then
  echo "done" > "$STATUS_FILE"
  log "=== 完成 ($WORKDIR) ==="
else
  echo "failed" > "$STATUS_FILE"
  log "FATAL: 失败, 详见上方日志"
  exit 1
fi

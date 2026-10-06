#!/usr/bin/env bash
# dub.sh — 视频配音流水线: ASR → 仅翻译(sts) → QC → 合并短句 → vtv(TTS+合成) → 合并
# 断点续跑: 段输出存在则跳过; en.srt 存在则跳过 ASR; zh-cn.srt 存在则跳过翻译
# 先翻译后合并(merge_srt.py): 防长段诱发 LLM 拆行错位; 合并消除逐句硬静音间隔, 减少为对齐而加速; .raw 为合并前备份
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
PIPELINE_DIR=$(dirname "$SCRIPT_DIR")
# shellcheck source=/dev/null
source "$PIPELINE_DIR/config.env"

INPUT="" OUT="" FINAL="" SEGMENTS_DIR=0 SEG_MIN="$SEGMENT_MIN"
FROM=0 TO=999999 NO_CONCAT=0

# TTS 引擎: TTS_MODEL="auk" 时用 AuK 本地 shim (模拟 fish /v1/tts); 其余值走 llama-swap
AUK_DIR="${AUK_DIR:-/home/loomz/workspace/money_code/videotrans/AuK}"
AUK_SHIM_PORT="${AUK_SHIM_PORT:-5998}"
AUK_SHIM_PID=""
# AuK 用独立 tts_type=36 (pyvideotrans 已注册, 复用 FishTTS 客户端),
# 使 TTS 缓存 key(含 tts_type) 与 s2-pro(30) 隔离, 换引擎不会命中旧引擎缓存
TTS_TYPE_EFFECTIVE="$TTS_TYPE"
if [[ "$TTS_MODEL" == "auk" ]]; then
  TTS_TYPE_EFFECTIVE=36
  if [[ "${AUK_FLASH:-0}" == "1" ]]; then
    AUK_CKPT="$AUK_DIR/ckpts/AuK-Flash/auk_flash.safetensors"
  else
    AUK_CKPT="$AUK_DIR/ckpts/AuK/auk_base.safetensors"
  fi
fi

usage() {
  cat << 'USAGE'
用法:
  dub.sh <video.mp4> [选项]                          整片: 自动切段+配音+合并
  dub.sh <segments_dir> --segments-dir [选项]        已有切分目录(seg_*.mp4)续跑

选项:
  --out DIR      输出目录 (默认: 整片=$OUTPUT_DIR/<名>; 切分目录=<目录>/out)
  --final PATH   最终合并文件 (默认: <out>/<名>_zh.mp4)
  --seg-min N    切段分钟数 (默认: config.env SEGMENT_MIN)
  --from N       从第 N 段开始 (0-based)
  --to N         到第 N 段 (含)
  --no-concat    不做最终合并
  -h, --help     帮助
USAGE
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --segments-dir) SEGMENTS_DIR=1; shift ;;
    --out) OUT="$2"; shift 2 ;;
    --final) FINAL="$2"; shift 2 ;;
    --seg-min) SEG_MIN="$2"; shift 2 ;;
    --from) FROM="$2"; shift 2 ;;
    --to) TO="$2"; shift 2 ;;
    --no-concat) NO_CONCAT=1; shift ;;
    -h|--help) usage ;;
    -*) echo "未知选项: $1" >&2; usage ;;
    *) [[ -n "$INPUT" ]] && { echo "只能给一个输入" >&2; exit 1; }; INPUT="$1"; shift ;;
  esac
done
[[ -n "$INPUT" ]] || usage

if [[ "$SEGMENTS_DIR" == 1 ]]; then
  [[ -d "$INPUT" ]] || { echo "FATAL: 切分目录不存在: $INPUT" >&2; exit 1; }
  SEG_DIR="$INPUT"
  NAME=$(basename "$INPUT")
  [[ -n "$OUT" ]] || OUT="$SEG_DIR/out"
else
  [[ -f "$INPUT" ]] || { echo "FATAL: 文件不存在: $INPUT" >&2; exit 1; }
  NAME=$(basename "$INPUT"); NAME="${NAME%.*}"
  [[ -n "$OUT" ]] || OUT="$OUTPUT_DIR/$NAME"
  SEG_DIR="$OUT/segments"
fi
mkdir -p "$OUT"
LOG_FILE="$OUT/pipeline.log"
FINAL="${FINAL:-$OUT/${NAME}_zh.mp4}"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG_FILE"; }
die() { log "FATAL: $*"; exit 1; }
dur() { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1"; }
srt_blocks() { grep -cE '^[0-9]{2}:[0-9]{2}:[0-9]{2}[,.][0-9]{3} --> ' "$1" || true; }

# 等 GPU 显存释放 (faster-whisper large-v3 需 ~5GB; LLM 占卡时主动卸载防 OOM)
wait_gpu_free() {
  local need_mb="${1:-5000}" waited=0 free_mb
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
  die "GPU 显存不足: 空闲 ${free_mb}MB < ${need_mb}MB (等待 300s 超时, 请 nvidia-smi 手动检查)"
}

# AuK TTS shim: 模拟 fish /v1/tts (启动加载一次 AuK, 请求锁串行)
# 逐段起停: 与翻译 LLM (23.7GB) 在 24GB 卡上互斥, 不能跨段常驻
start_auk_shim() {
  wait_gpu_free 18000
  log "  启动 AuK TTS shim (模型加载 ~1-3 分钟) → http://127.0.0.1:${AUK_SHIM_PORT}/v1/tts"
  ( cd "$AUK_DIR" && AUK_CKPT="$AUK_CKPT" AUK_SHIM_PORT="$AUK_SHIM_PORT" \
      exec .venv/bin/python "$SCRIPT_DIR/auk-tts-shim.py" ) >>"$OUT/auk-shim.log" 2>&1 &
  AUK_SHIM_PID=$!
  local waited=0
  while (( waited < 600 )); do
    curl -sf -m 3 "http://127.0.0.1:${AUK_SHIM_PORT}/health" >/dev/null 2>&1 \
      && { log "  AuK shim 就绪 (${waited}s)"; return 0; }
    kill -0 "$AUK_SHIM_PID" 2>/dev/null \
      || { tail -30 "$OUT/auk-shim.log" | tee -a "$LOG_FILE"; die "AuK shim 启动即退出, 详见 $OUT/auk-shim.log"; }
    sleep 5; waited=$(( waited + 5 ))
  done
  kill "$AUK_SHIM_PID" 2>/dev/null || true
  die "AuK shim 健康检查超时 (600s), 详见 $OUT/auk-shim.log"
}

stop_auk_shim() {
  [[ -n "$AUK_SHIM_PID" ]] || return 0
  log "  停止 AuK shim"
  kill "$AUK_SHIM_PID" 2>/dev/null || true
  local i
  for i in $(seq 1 30); do
    kill -0 "$AUK_SHIM_PID" 2>/dev/null || break
    sleep 1
  done
  kill -9 "$AUK_SHIM_PID" 2>/dev/null || true
  wait "$AUK_SHIM_PID" 2>/dev/null || true
  AUK_SHIM_PID=""
}
trap '[[ -n "${AUK_SHIM_PID:-}" ]] && kill "$AUK_SHIM_PID" 2>/dev/null || true' EXIT

# 翻译子任务 (sts); $1 = 段输出目录
run_sts() {
  ( cd "$PYVIDEOTRANS_DIR" && uv run cli.py \
      --task sts \
      --name "$1/en.srt" \
      --output-dir "$1" \
      --source_language_code en --target_language_code zh-cn \
      --translate_type "$TRANSLATE_TYPE" \
      --no-clear-cache ) >>"$LOG_FILE" 2>&1
  [[ -f "$1/zh-cn.srt" ]] || mv "$1/en.zh-cn.srt" "$1/zh-cn.srt" 2>/dev/null
}

# 合并前 QC: 行数 1:1 + 无空行 (抓 LLM 拆行错位/漏行); 依赖外层 $segout
qc_pass() {
  local en_n zh_n diff_n tol zh_empty
  en_n=$(srt_blocks "$segout/en.srt"); zh_n=$(srt_blocks "$segout/zh-cn.srt")
  diff_n=$(( en_n > zh_n ? en_n - zh_n : zh_n - en_n ))
  tol=$(( en_n / 100 )); (( tol < 2 )) && tol=2
  (( diff_n <= tol )) || return 1
  zh_empty=$(python3 -c "
import sys
blocks = [b for b in open(sys.argv[1], encoding='utf-8').read().split('\n\n') if '-->' in b]
print(sum(1 for b in blocks if len(b.split('\n', 2)) < 3 or not b.split('\n', 2)[2].strip()))
" "$segout/zh-cn.srt")
  (( zh_empty <= 2 )) || return 1
  return 0
}

# ---------- 预检 ----------
for c in ffmpeg ffprobe uv python3 curl; do command -v "$c" >/dev/null || die "缺少命令: $c"; done
[[ -d "$PYVIDEOTRANS_DIR" ]] || die "pyvideotrans 目录不存在: $PYVIDEOTRANS_DIR"
curl -sf "$SWAP_BASE/v1/models" >/dev/null || die "llama-swap 不可达: $SWAP_BASE"
free_g=$(df -BG "$OUT" | awk 'NR==2 {print $4}' | tr -d 'G')
(( free_g >= 10 )) || die "磁盘空间不足: ${free_g}GB < 10GB"
if [[ "$TTS_MODEL" == "auk" ]]; then
  [[ -x "$AUK_DIR/.venv/bin/python" ]] || die "AuK venv 缺失: $AUK_DIR/.venv"
  [[ -f "$AUK_CKPT" ]] || die "AuK 权重缺失: $AUK_CKPT"
  [[ -f "$AUK_DIR/ckpts/Qwen2.5-Omni-3B/config.json" ]] \
    || die "Qwen2.5-Omni-3B 编码器缺失: $AUK_DIR/ckpts/Qwen2.5-Omni-3B"
fi

# ---------- 配置断言（自愈: 防 GUI 把 cfg.json 写回默认值） ----------
assert_config() {
  SWAP_BASE="$SWAP_BASE" TRANSLATE_MODEL="$TRANSLATE_MODEL" TTS_MODEL="$TTS_MODEL" \
  ASR_MODEL="$ASR_MODEL" TRANSLATE_TYPE="$TRANSLATE_TYPE" TTS_TYPE="$TTS_TYPE_EFFECTIVE" \
  VOICE_ROLE="$VOICE_ROLE" VOICE_REF_TEXT="$VOICE_REF_TEXT" TTS_THREADS="$TTS_THREADS" \
  AUK_SHIM_PORT="$AUK_SHIM_PORT" \
  python3 - "$PYVIDEOTRANS_DIR" << 'PYEOF'
import json, os, sys
root = sys.argv[1]
e = os.environ
cfg_p, par_p = f"{root}/videotrans/cfg.json", f"{root}/videotrans/params.json"
cfg, par = json.load(open(cfg_p)), json.load(open(par_p))
changed = []
def put(d, tag, k, v):
    if d.get(k) != v:
        d[k] = v
        changed.append(f"{tag}.{k}={v}")
put(cfg, "cfg", "aisendsrt", False)
put(cfg, "cfg", "trans_thread", 5)
put(cfg, "cfg", "retry_nums", 3)
put(cfg, "cfg", "dubbing_thread", int(e["TTS_THREADS"]))
put(par, "params", "localllm_api", f"{e['SWAP_BASE']}/upstream/{e['TRANSLATE_MODEL']}/v1")
put(par, "params", "localllm_model", e["TRANSLATE_MODEL"])
put(par, "params", "localllm_max_token", 4096)
put(par, "params", "tts_type", int(e["TTS_TYPE"]))
if e["TTS_MODEL"] == "auk":
    put(par, "params", "fishtts_url", f"http://127.0.0.1:{e['AUK_SHIM_PORT']}/v1/tts")
else:
    put(par, "params", "fishtts_url", f"{e['SWAP_BASE']}/upstream/{e['TTS_MODEL']}/v1/tts")
put(par, "params", "voice_role", e["VOICE_ROLE"])
put(par, "params", "model_name", e["ASR_MODEL"])
if e["VOICE_ROLE"] != "clone":
    ref = f"{root}/f5-tts/{e['VOICE_ROLE']}"
    if not os.path.exists(ref):
        print(f"FATAL: 参考音频不存在: {ref} (VOICE_ROLE={e['VOICE_ROLE']})", file=sys.stderr)
        sys.exit(1)
    put(par, "params", "f5tts_role", f"{e['VOICE_ROLE']}#{e['VOICE_REF_TEXT']}")
if changed:
    json.dump(cfg, open(cfg_p, "w"), ensure_ascii=False, separators=(",", ":"))
    json.dump(par, open(par_p, "w"), ensure_ascii=False, separators=(",", ":"))
    print("配置已修复:", ", ".join(changed))
else:
    print("配置 OK")
PYEOF
}

log "=== dub.sh 启动: 输入=$INPUT 输出=$OUT TTS引擎=$TTS_MODEL 音色=$VOICE_ROLE ==="

# ---------- 0) TTS 引擎准备 ----------
# s2-pro: compile 预热 (幂等: 首次等GPU+编译 ~10-20min 一次性; 之后毫秒级跳过)
#         首次不预热则 llama-swap 加载 s2-pro(--compile) 会超 120s 健康检查
# auk: 无需预热 (shim 逐段启动, 见 start_auk_shim)
if [[ "$TTS_MODEL" == "auk" ]]; then
  log "TTS 引擎=auk ($AUK_CKPT), 跳过 s2-pro compile 预热"
else
  log "TTS compile 预热检查"
  bash "$SCRIPT_DIR/warmup-s2pro-compile.sh" 2>&1 | tee -a "$LOG_FILE" || die "TTS 预热失败 (详见上方 warmup 输出)"
fi

# ---------- 切分（整片模式） ----------
if [[ "$SEGMENTS_DIR" == 0 ]]; then
  mkdir -p "$SEG_DIR"
  if ls "$SEG_DIR"/seg_*.mp4 >/dev/null 2>&1; then
    log "段文件已存在, 跳过切分"
  else
    log "切分: 每 ${SEG_MIN} 分钟 (NVENC) → $SEG_DIR"
    ffmpeg -hide_banner -loglevel error -y -i "$INPUT" \
      -c:v h264_nvenc -preset p5 -cq 19 -c:a aac -b:a 192k \
      -f segment -segment_time $((SEG_MIN * 60)) -reset_timestamps 1 \
      "$SEG_DIR/seg_%03d.mp4" 2>&1 | tee -a "$LOG_FILE" || die "切分失败"
  fi
fi

shopt -s nullglob
SEGS=("$SEG_DIR"/seg_*.mp4)
shopt -u nullglob
[[ ${#SEGS[@]} -gt 0 ]] || die "$SEG_DIR 下没有 seg_*.mp4"
log "共 ${#SEGS[@]} 段"

# ---------- 逐段配音 ----------
for i in "${!SEGS[@]}"; do
  idx=$(printf "%03d" "$i")
  if (( i < FROM || i > TO )); then continue; fi
  segout="$OUT/seg_$idx"
  if [[ -f "$segout/seg_$idx.mp4" ]]; then
    log "[$((i+1))/${#SEGS[@]}] seg_$idx 已完成, 跳过"
    continue
  fi
  log "[$((i+1))/${#SEGS[@]}] seg_$idx 开始: ${SEGS[$i]}"
  assert_config 2>&1 | tee -a "$LOG_FILE" || die "配置断言失败"
  # 1) ASR（en.srt 存在则跳过）
  if [[ ! -f "$segout/en.srt" ]]; then
    wait_gpu_free 5000
    log "  [1/4] ASR → en.srt"
    ( cd "$PYVIDEOTRANS_DIR" && uv run cli.py \
        --task stt \
        --name "${SEGS[$i]}" \
        --output-dir "$segout" \
        --source_language_code en \
        --model_name "$ASR_MODEL" \
        --cuda \
        --no-clear-cache ) >>"$LOG_FILE" 2>&1 \
      || die "seg_$idx ASR 失败, 详见 $LOG_FILE"
    # stt 任务输出 <名字>.srt (不带语言码), 改名成 vtv 用的 en.srt
    [[ -f "$segout/en.srt" ]] || mv "$segout/seg_$idx.srt" "$segout/en.srt" 2>/dev/null \
      || die "seg_$idx: ASR 后未生成 en.srt (也无 seg_$idx.srt)"
  fi
  # 2) 仅翻译（zh-cn.srt 存在则跳过）—— 必须先翻译后合并:
  #    合并前翻译会产生长段, LLM 拆行 → 按行号 zip 错位 → 丢句+长静音
  if [[ ! -f "$segout/zh-cn.srt" ]]; then
    # 旧流程可能留下已合并的 en.srt (.raw=合并前原件) → 先还原再翻译
    [[ -f "$segout/en.srt.raw" ]] && cp "$segout/en.srt.raw" "$segout/en.srt"
    log "  [2/4] sts → zh-cn.srt (先翻译后合并)"
    run_sts "$segout" || die "seg_$idx 翻译失败, 详见 $LOG_FILE"
    [[ -f "$segout/zh-cn.srt" ]] || die "seg_$idx: 翻译后未生成 zh-cn.srt (也无 en.zh-cn.srt)"
  fi
  # 3) 合并前 QC: 行数 1:1 + 无空行（抓 LLM 拆行错位/漏行）; 失败自动重翻一次
  if ! qc_pass; then
    log "  [QC 失败] 疑似 LLM 拆行错位/漏行 → 重翻一次"
    rm -f "$segout/zh-cn.srt" "$segout/en.zh-cn.srt"
    run_sts "$segout" || die "seg_$idx: QC 失败后重翻也失败, 详见 $LOG_FILE"
    [[ -f "$segout/zh-cn.srt" ]] || die "seg_$idx: 重翻后未生成 zh-cn.srt"
    qc_pass || die "seg_$idx: QC 仍失败 (重翻后仍错位)"
  fi
  # 4) 合并连续短句（幂等/无损; .raw=原始备份）→ TTS 连续自然、减少加速; vtv 此时只做 TTS+合成
  log "  [4/4] 合并短句 + vtv (TTS+合成, 引擎=$TTS_MODEL)"
  python3 "$SCRIPT_DIR/merge_srt.py" "$segout/en.srt" 2>&1 | tee -a "$LOG_FILE"
  python3 "$SCRIPT_DIR/merge_srt.py" "$segout/zh-cn.srt" 2>&1 | tee -a "$LOG_FILE"
  [[ "$TTS_MODEL" == "auk" ]] && start_auk_shim
  ( cd "$PYVIDEOTRANS_DIR" && uv run cli.py \
      --task vtv \
      --name "${SEGS[$i]}" \
      --output-dir "$segout" \
      --source_language_code en --target_language_code zh-cn \
      --model_name "$ASR_MODEL" \
      --translate_type "$TRANSLATE_TYPE" \
      --tts_type "$TTS_TYPE_EFFECTIVE" \
      --voice_role "$VOICE_ROLE" \
      --voice_autorate \
      --align_sub_audio \
      --subtitle_type "$SUBTITLE_TYPE" \
      --cuda \
      --no-clear-cache ) >>"$LOG_FILE" 2>&1 \
    || die "seg_$idx 失败, 详见 $LOG_FILE"
  [[ "$TTS_MODEL" == "auk" ]] && stop_auk_shim
  outmp4="$segout/seg_$idx.mp4"
  [[ -f "$outmp4" ]] || die "seg_$idx: 输出文件缺失: $outmp4"
  din=$(dur "${SEGS[$i]}"); dout=$(dur "$outmp4")
  python3 -c "import sys; sys.exit(0 if abs($din-$dout) < 5 else 1)" \
    || die "seg_$idx: 时长偏差过大 in=${din}s out=${dout}s"
  if [[ -f "$segout/en.srt" && -f "$segout/zh-cn.srt" ]]; then
    # 用合并前的 .raw 对比块数（合并会减少块数，属正常）
    en_f="$segout/en.srt.raw";     [[ -f "$en_f" ]] || en_f="$segout/en.srt"
    zh_f="$segout/zh-cn.srt.raw";  [[ -f "$zh_f" ]] || zh_f="$segout/zh-cn.srt"
    en_n=$(srt_blocks "$en_f"); zh_n=$(srt_blocks "$zh_f")
    diff_n=$(( en_n > zh_n ? en_n - zh_n : zh_n - en_n ))
    tol=$(( en_n / 50 )); (( tol < 5 )) && tol=5
    (( diff_n <= tol )) || die "seg_$idx: 字幕块数偏差过大 en=$en_n zh=$zh_n (疑似丢句)"
    log "[$((i+1))/${#SEGS[@]}] seg_$idx 完成 (${dout}s, 合并后 $(srt_blocks "$segout/zh-cn.srt") 行)"
  else
    log "[$((i+1))/${#SEGS[@]}] seg_$idx 完成 (${dout}s)"
  fi
done

# ---------- 合并 ----------
if [[ "$NO_CONCAT" == 0 ]]; then
  log "合并 → $FINAL"
  : > "$OUT/concat.txt"
  for i in "${!SEGS[@]}"; do
    idx=$(printf "%03d" "$i")
    f="$OUT/seg_$idx/seg_$idx.mp4"
    [[ -f "$f" ]] || die "缺少 $f, 无法合并"
    echo "file '$f'" >> "$OUT/concat.txt"
  done
  ffmpeg -hide_banner -loglevel error -y -f concat -safe 0 -i "$OUT/concat.txt" -c copy "$FINAL" \
    2>&1 | tee -a "$LOG_FILE" || die "合并失败"
  total_in=0
  for seg in "${SEGS[@]}"; do
    total_in=$(python3 -c "print($total_in + $(dur "$seg"))")
  done
  total_out=$(dur "$FINAL")
  python3 -c "import sys; sys.exit(0 if abs($total_in-$total_out) < 10 else 1)" \
    || die "合并后总时长偏差过大 in=${total_in}s out=${total_out}s"
  log "=== 完成: $FINAL (${total_out}s) ==="
else
  log "=== 完成(未合并): $OUT ==="
fi

#!/usr/bin/env bash
# fetch.sh — 取片：URL 下载（yt-dlp）或本地路径透传
# stdout 最后一行 = 最终 mp4 本地路径（机器可读）
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
PIPELINE_DIR=$(dirname "$SCRIPT_DIR")
# shellcheck source=/dev/null
source "$PIPELINE_DIR/config.env"

INPUT="" OUT_DIR="$DOWNLOAD_DIR"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) OUT_DIR="$2"; shift 2 ;;
    -h|--help) echo "Usage: fetch.sh <url|local path> [-o 输出目录]"; exit 0 ;;
    *) INPUT="$1"; shift ;;
  esac
done
[[ -n "$INPUT" ]] || { echo "Usage: fetch.sh <url|local path> [-o 输出目录]" >&2; exit 1; }

dur() { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1"; }

# ---- 本地路径：验证 + 透传 ----
if [[ "$INPUT" != http* ]]; then
  [[ -f "$INPUT" ]] || { echo "FATAL: 文件不存在: $INPUT" >&2; exit 1; }
  dur "$INPUT" >/dev/null || { echo "FATAL: 不是有效视频: $INPUT" >&2; exit 1; }
  echo "[fetch] 本地文件 OK: $INPUT ($(dur "$INPUT")s)"
  echo "$INPUT"
  exit 0
fi

mkdir -p "$OUT_DIR"

# ---- URL：按域名映射站点名与 cookies ----
host=$(python3 - "$INPUT" << 'PY'
import sys, urllib.parse
h = urllib.parse.urlparse(sys.argv[1]).netloc.lower()
h = h.split(':')[0]
if h.startswith('www.'):
    h = h[4:]
print(h)
PY
)
cookies=""
case "$host" in
  weibo.com|m.weibo.cn)            site=weibo;    cookies="${WEIBO_COOKIES:-}" ;;
  bilibili.com|b23.tv)             site=bilibili; cookies="${BILI_COOKIES:-}" ;;
  douyin.com|iesdouyin.com)        site=douyin;   cookies="${DOUYIN_COOKIES:-}" ;;
  youtube.com|youtu.be)            site=youtube;  cookies="${YOUTUBE_COOKIES:-}" ;;
  twitter.com|x.com)               site=twitter;  cookies="${TWITTER_COOKIES:-}" ;;
  *)                               site="${host%%.*}" ;;
esac

extra=()
[[ -n "$cookies" && -f "$cookies" ]] && extra+=(--cookies "$cookies")

# 先解析 ID（拼文件名）
vid=$(yt-dlp -q --get-id ${extra[@]+"${extra[@]}"} "$INPUT" 2>/dev/null) \
  || { echo "FATAL: 无法解析视频 ID（可能需要 cookies）: $INPUT" >&2; exit 1; }
target="$OUT_DIR/${site}_${vid}.mp4"

if [[ -f "$target" ]]; then
  echo "[fetch] 已存在: $target"
else
  echo "[fetch] 下载: $INPUT → $target"
  yt-dlp --no-playlist -f "bv*+ba/b" --merge-output-format mp4 \
    ${extra[@]+"${extra[@]}"} \
    -o "$OUT_DIR/${site}_%(id)s.%(ext)s" "$INPUT"
fi

dur "$target" >/dev/null || { echo "FATAL: 下载文件无效: $target" >&2; exit 1; }
echo "[fetch] 完成: $target ($(dur "$target")s)"
echo "$target"

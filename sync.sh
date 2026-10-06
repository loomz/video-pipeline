#!/usr/bin/env bash
# sync.sh — 把 skills 安装/同步到两个 agent（幂等，可重复执行）
#   Claude Code : 软链接（即时同步）
#   OpenClaw    : 原生 install（复制；SKILL.md 文案改动后重跑本脚本即可）
set -euo pipefail
CANON="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

mkdir -p ~/.claude/skills
ln -sfn "$CANON/video-fetch" ~/.claude/skills/video-fetch
ln -sfn "$CANON/video-dub"   ~/.claude/skills/video-dub
echo "[sync] claude 软链接 OK"

openclaw skills install --force "$CANON/video-fetch" --as video-fetch
openclaw skills install --force "$CANON/video-dub"   --as video-dub
echo "[sync] openclaw 安装 OK"

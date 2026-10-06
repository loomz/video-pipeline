#!/usr/bin/env bash
# service.sh — video-pipeline Web 控制台 (pipeline-web.py, :8090) 管理
#
# 只管 pipeline-web.py; 不管 llama-swap (与 agent 共用, 由 ~/start-llama-swap.sh 单独管理)
#
# 用法:
#   service.sh         启动 (已在运行则直接退出; 幂等可重复执行)
#   service.sh status  查状态
#   service.sh stop    停止
#   service.sh logs    跟踪日志 (tail -f /tmp/pipeline-web.log, Ctrl-C 退出)
#   service.sh restart 重启 (stop → 等端口释放 → start)
set -euo pipefail

PIPELINE_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
WEB_URL="http://127.0.0.1:8090"
WEB_LOG=/tmp/pipeline-web.log

log()  { echo "[$(date '+%F %T')] $*"; }
web_up() { curl -sf -m 3 "$WEB_URL/" >/dev/null 2>&1; }

cmd_status() {
  if web_up; then
    log "pipeline-web: UP → $WEB_URL"
  else
    log "pipeline-web: DOWN (日志: $WEB_LOG)"
  fi
}

cmd_stop() {
  local pids
  pids=$(pgrep -f "pipeline-web\.py" || true)
  if [[ -z "$pids" ]]; then log "pipeline-web: 未在运行"; return 0; fi
  kill $pids 2>/dev/null || true
  log "pipeline-web: 已停止 (PID: $(echo $pids | tr '\n' ' '))"
}

cmd_logs() {
  if [[ ! -f "$WEB_LOG" ]]; then
    log "日志文件不存在: $WEB_LOG (pipeline-web 还没启动过)"
    exit 1
  fi
  exec tail -f "$WEB_LOG"
}

wait_down() {
  for _ in $(seq 1 5); do
    web_up || return 0
    sleep 1
  done
  log "WARN: 5s 后端口仍被占用, 继续尝试启动 (可能端口冲突)"
}

do_start() {
  if web_up; then
    log "pipeline-web: 已在运行 → $WEB_URL"
    return 0
  fi
  log "pipeline-web: 未运行 → 启动 (nohup)"
  nohup python3 "$PIPELINE_DIR/pipeline-web.py" >>"$WEB_LOG" 2>&1 &
  disown
  for _ in $(seq 1 15); do
    web_up && break
    sleep 1
  done
  if web_up; then
    log "pipeline-web: 已启动 → $WEB_URL"
    log "配音任务: 网页上启动/停止, 或手动 nohup $PIPELINE_DIR/scripts/dub.sh <video.mp4> --out <dir> >/dev/null 2>&1 &"
    log "⚠️ 流水线运行期间不要找任何 agent 说话 (Claude Code/OpenClaw); 同时只跑一条流水线"
  else
    log "ERROR: 15s 未通过健康检查, 看 $WEB_LOG"
    return 1
  fi
}

main() {
  case "${1:-start}" in
    status)  cmd_status; return 0 ;;
    stop)    cmd_stop; return 0 ;;
    logs)    cmd_logs; return 0 ;;
    restart) cmd_stop; wait_down; do_start ;;
    start)   do_start ;;
    *) echo "用法: $0 {start|status|stop|logs|restart}" >&2; exit 1 ;;
  esac
}

main "$@"

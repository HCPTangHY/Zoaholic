#!/usr/bin/env bash
# ===========================================================================
# Zoaholic 外部健康监控脚本
#
# 功能：
#   1. 定期 curl /healthz，超时视为失败
#   2. 连续 N 次失败 → 杀进程让 screen / pm2 重拉（可选）
#   3. 检测"功能性死亡"：进程活着但长时间没有成功请求
#   4. 所有检查结果写日志 + 可选发送告警
#
# 用法：
#   # 前台运行（调试）
#   ./scripts/health_monitor.sh
#
#   # cron 每分钟跑一次（推荐）
#   * * * * * /www/wwwroot/Zoaholic/scripts/health_monitor.sh --cron
#
#   # 后台常驻（每 CHECK_INTERVAL 秒轮询）
#   nohup ./scripts/health_monitor.sh --daemon &
#
# 环境变量覆盖（均有默认值）：
#   ZOAHOLIC_PORT           服务端口        (默认 8101)
#   ZOAHOLIC_HOST           服务地址        (默认 127.0.0.1)
#   HEALTH_TIMEOUT          curl 超时秒数   (默认 5)
#   MAX_FAILURES            连续失败阈值    (默认 3)
#   STALE_THRESHOLD         功能性死亡秒数  (默认 300 = 5分钟没成功请求)
#   CHECK_INTERVAL          轮询间隔秒数    (默认 30，仅 --daemon 模式)
#   AUTO_RESTART            是否自动重启    (默认 false)
#   LOG_FILE                日志文件路径
#   STATE_FILE              状态文件路径（记录连续失败数）
# ===========================================================================

set -euo pipefail

# ==================== 配置 ====================

ZOAHOLIC_PORT="${ZOAHOLIC_PORT:-8101}"
ZOAHOLIC_HOST="${ZOAHOLIC_HOST:-127.0.0.1}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-5}"
MAX_FAILURES="${MAX_FAILURES:-3}"
STALE_THRESHOLD="${STALE_THRESHOLD:-300}"
CHECK_INTERVAL="${CHECK_INTERVAL:-30}"
MEMORY_DIFF_INTERVAL="${MEMORY_DIFF_INTERVAL:-10}"  # 每N次check拉一次memory diff
AUTO_RESTART="${AUTO_RESTART:-true}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="${LOG_FILE:-${BASE_DIR}/data/health_monitor.log}"
STATE_FILE="${STATE_FILE:-${BASE_DIR}/data/health_monitor.state}"

HEALTHZ_URL="http://${ZOAHOLIC_HOST}:${ZOAHOLIC_PORT}/healthz"

# ==================== 工具函数 ====================

log() {
    local level="$1"; shift
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    local msg="[${ts}] [${level}] $*"
    echo "$msg" >> "$LOG_FILE"
    # 非 cron 模式也输出到 stderr
    [[ "${CRON_MODE:-}" != "true" ]] && echo "$msg" >&2
}

# 读取连续失败计数
read_fail_count() {
    if [[ -f "$STATE_FILE" ]]; then
        cat "$STATE_FILE" 2>/dev/null || echo "0"
    else
        echo "0"
    fi
}

# 写入连续失败计数
write_fail_count() {
    echo "$1" > "$STATE_FILE"
}

# ==================== 核心检查 ====================

do_check() {
    # 热重载检测：.reloading 文件存在说明 uvicorn 正在重载，跳过本轮检查
    local reload_marker="${BASE_DIR}/data/.reloading"
    if [[ -f "$reload_marker" ]]; then
        log "INFO" "Hot-reload in progress (.reloading marker found), skipping check"
        write_fail_count "0"
        return 0
    fi

    local fail_count
    fail_count="$(read_fail_count)"

    # --- Step 1: curl /healthz ---
    local http_code body curl_exit
    body="$(curl -s -m "${HEALTH_TIMEOUT}" -w '\n%{http_code}' "${HEALTHZ_URL}" 2>/dev/null)" && curl_exit=0 || curl_exit=$?

    if [[ $curl_exit -ne 0 ]]; then
        # curl 本身失败（超时、拒绝连接等）
        fail_count=$((fail_count + 1))
        write_fail_count "$fail_count"
        log "ERROR" "healthz unreachable (curl exit=${curl_exit}), consecutive_failures=${fail_count}/${MAX_FAILURES}"
        maybe_restart "$fail_count" "healthz_unreachable"
        return 1
    fi

    # 分离 body 和 http_code
    http_code="$(echo "$body" | tail -1)"
    body="$(echo "$body" | sed '$d')"

    if [[ "$http_code" -ge 500 ]] || [[ "$http_code" -eq 503 ]]; then
        fail_count=$((fail_count + 1))
        write_fail_count "$fail_count"
        log "ERROR" "healthz returned ${http_code}, consecutive_failures=${fail_count}/${MAX_FAILURES}"
        maybe_restart "$fail_count" "healthz_${http_code}"
        return 1
    fi

    # --- Step 2: 解析 metrics ---
    # 用 python 一行提取关键字段（jq 不一定装了）
    local metrics
    metrics="$(echo "$body" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    m = d.get('metrics', {})
    req = m.get('requests', {})
    conn = m.get('connections', {})
    mem = m.get('memory', {})
    print(f\"active={req.get('active_requests', '?')}\")
    print(f\"total={req.get('total_requests', '?')}\")
    print(f\"last_success_ago={req.get('seconds_since_last_success', '?')}\")
    print(f\"last_error_ago={req.get('seconds_since_last_error', '?')}\")
    print(f\"total_active_conn={conn.get('total_active_connections', '?')}\")
    print(f\"total_idle_conn={conn.get('total_idle_connections', '?')}\")
    print(f\"rss_mb={mem.get('rss_mb', '?')}\")
    print(f\"threads={mem.get('threads', '?')}\")
    print(f\"open_fds={mem.get('open_fds', '?')}\")
except Exception as e:
    print(f'parse_error={e}')
" 2>/dev/null)"

    # 提取各字段
    local active last_success_ago rss_mb
    active="$(echo "$metrics" | grep '^active=' | cut -d= -f2)"
    last_success_ago="$(echo "$metrics" | grep '^last_success_ago=' | cut -d= -f2)"
    rss_mb="$(echo "$metrics" | grep '^rss_mb=' | cut -d= -f2)"

    # --- Step 3: 功能性死亡检测（已禁用）---
    # healthz 200 就说明服务活着，没流量不代表卡死
    # if [[ "$last_success_ago" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    #     local stale_int
    #     stale_int="${last_success_ago%%.*}"
    #     if [[ "$stale_int" -ge "$STALE_THRESHOLD" ]]; then
    #         fail_count=$((fail_count + 1))
    #         write_fail_count "$fail_count"
    #         log "WARN" "functional stall: no success in ${stale_int}s (threshold=${STALE_THRESHOLD}s), active=${active}, consecutive_failures=${fail_count}/${MAX_FAILURES}"
    #         maybe_restart "$fail_count" "functional_stall_${stale_int}s"
    #         return 1
    #     fi
    # fi

    # --- Step 4: 一切正常，重置计数 ---
    if [[ "$fail_count" -gt 0 ]]; then
        log "INFO" "recovered after ${fail_count} consecutive failure(s)"
    fi
    write_fail_count 0

    # 正常日志（精简一行）
    log "OK" "status=${http_code} active=${active} last_ok=${last_success_ago}s rss=${rss_mb}MB conns_active=$(echo "$metrics" | grep '^total_active_conn=' | cut -d= -f2)"
    return 0
}

# ==================== 自动重启 ====================

maybe_restart() {
    local fail_count="$1"
    local reason="$2"

    if [[ "$fail_count" -lt "$MAX_FAILURES" ]]; then
        return
    fi

    log "CRIT" "=== ${fail_count} consecutive failures (reason: ${reason}), threshold ${MAX_FAILURES} reached ==="

    if [[ "$AUTO_RESTART" != "true" ]]; then
        log "CRIT" "AUTO_RESTART is off, not restarting. Set AUTO_RESTART=true to enable."
        return
    fi

    log "CRIT" "Attempting restart..."

    # 尝试 pm2（如果 zoaholic 跑在 pm2 里）
    if command -v pm2 &>/dev/null && pm2 describe zoaholic &>/dev/null 2>&1; then
        pm2 restart zoaholic
        log "CRIT" "pm2 restart zoaholic executed"
        write_fail_count 0
        return
    fi

    # 尝试 screen + kill（当前方式：找 uvicorn 进程杀掉，screen 不会自动重启）
    # 这里只杀进程，需要配合 screen 里的 while true 循环或外部重启逻辑
    local pids
    pids="$(pgrep -f "uvicorn main:app.*${ZOAHOLIC_PORT}" 2>/dev/null || true)"
    if [[ -n "$pids" ]]; then
        log "CRIT" "Killing uvicorn PIDs: ${pids}"
        echo "$pids" | xargs kill -9 2>/dev/null || true
        write_fail_count 0
        log "CRIT" "Process killed. Manual restart may be needed if not using process manager."
    else
        log "CRIT" "No uvicorn process found on port ${ZOAHOLIC_PORT}, cannot restart"
    fi
}

# ==================== 日志轮转 ====================

rotate_log() {
    local max_size=$((5 * 1024 * 1024))  # 5MB
    if [[ -f "$LOG_FILE" ]]; then
        local size
        size="$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)"
        if [[ "$size" -gt "$max_size" ]]; then
            mv "$LOG_FILE" "${LOG_FILE}.1"
            log "INFO" "log rotated (was ${size} bytes)"
        fi
    fi
}

# ==================== 入口 ====================

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$STATE_FILE")"

case "${1:-}" in
    --cron)
        CRON_MODE=true
        rotate_log
        do_check
        ;;
    --daemon)
        log "INFO" "daemon started: url=${HEALTHZ_URL} interval=${CHECK_INTERVAL}s max_failures=${MAX_FAILURES} stale=${STALE_THRESHOLD}s auto_restart=${AUTO_RESTART}"
        _mem_diff_counter=0
        while true; do
            rotate_log
            do_check || true
            _mem_diff_counter=$(( _mem_diff_counter + 1 ))
            if [ "$_mem_diff_counter" -ge "$MEMORY_DIFF_INTERVAL" ]; then
                _mem_diff_counter=0
                "${SCRIPT_DIR}/memory_diff_tracker.sh" 2>/dev/null || true
            fi
            sleep "$CHECK_INTERVAL"
        done
        ;;
    --once|"")
        do_check
        ;;
    *)
        echo "Usage: $0 [--cron|--daemon|--once]" >&2
        exit 1
        ;;
esac

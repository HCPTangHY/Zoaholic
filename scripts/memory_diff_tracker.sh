#!/usr/bin/env bash
# 定期拉 /debug/memory/diff，记录 GC 对象增长趋势
# 用法: 在 health_monitor daemon 循环里调用，或单独 cron
# 每 5 分钟拉一次 diff，结果追加到日志

set -euo pipefail

ZOAHOLIC_PORT="${ZOAHOLIC_PORT:-8101}"
ZOAHOLIC_HOST="${ZOAHOLIC_HOST:-127.0.0.1}"
BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="${BASE_DIR}/data/memory_diff.log"
MAX_LOG_SIZE=5242880  # 5MB

DIFF_URL="http://${ZOAHOLIC_HOST}:${ZOAHOLIC_PORT}/debug/memory/diff"
MEM_URL="http://${ZOAHOLIC_HOST}:${ZOAHOLIC_PORT}/debug/memory"

mkdir -p "$(dirname "$LOG_FILE")"

# 日志轮转
if [[ -f "$LOG_FILE" ]] && [[ $(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt $MAX_LOG_SIZE ]]; then
    mv "$LOG_FILE" "${LOG_FILE}.old"
fi

timestamp=$(date '+%Y-%m-%d %H:%M:%S')

# 拉 diff
diff_result=$(curl -s --max-time 10 "$DIFF_URL" 2>/dev/null || echo '{"error":"curl failed"}')

# 提取关键信息
rss=$(echo "$diff_result" | python3.11 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    rss = d.get("rss_mb", "?")
    growth = d.get("top20_growth", {})
    if "message" in d:
        print(f"RSS={rss}MB [baseline taken]")
    elif growth:
        items = sorted(growth.items(), key=lambda x: -abs(x[1].get("diff",0)))[:10]
        parts = []
        for k, v in items:
            dd = v.get("diff", 0)
            cc = v.get("current", 0)
            if dd != 0:
                parts.append(f"{k}:{dd:+d}({cc})")
        if parts:
            print(f"RSS={rss}MB " + " ".join(parts))
        else:
            print(f"RSS={rss}MB [no change]")
    else:
        print(f"RSS={rss}MB [no change]")
except Exception as e:
    print(f"parse error: {e}")
' 2>/dev/null || echo 'parse error')

echo "[$timestamp] $rss" >> "$LOG_FILE"

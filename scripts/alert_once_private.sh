#!/bin/bash
# 状态变化提醒（私信版）：跌破/突破时提醒一次，收回后自动重置，再次触发再提醒
# 与 alert_once.sh 逻辑相同，但走"私信"（notify_feishu.sh）而非群发
#
# 用法:
#   bash alert_once_private.sh "触发键" "状态" "提醒内容"
set -u

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$SKILL_DIR/data/alert_state.txt"
KEY="${1:-}"
ST="${2:-}"
MSG="${3:-}"

if [ -z "$KEY" ] || [ -z "$ST" ]; then
    echo "用法: bash alert_once_private.sh \"触发键\" \"状态\" \"提醒内容\"" >&2
    exit 1
fi

mkdir -p "$(dirname "$STATE")"

CUR=$(grep "^${KEY}=" "$STATE" 2>/dev/null | head -1 | cut -d= -f2)

if [ "$CUR" = "$ST" ]; then
    echo "[跳过] $KEY 状态未变($ST)"
    exit 0
fi

if grep -q "^${KEY}=" "$STATE" 2>/dev/null; then
    sed -i "s|^${KEY}=.*|${KEY}=${ST}|" "$STATE"
else
    echo "${KEY}=${ST}" >> "$STATE"
fi

if [ "$ST" = "above" ] || [ -z "$MSG" ]; then
    echo "[重置] $KEY -> $ST"
    exit 0
fi

# 私信
bash "$SKILL_DIR/scripts/notify_feishu.sh" "$MSG"
echo "[已提醒] $KEY -> $ST"

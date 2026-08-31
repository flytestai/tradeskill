#!/bin/bash
# 状态变化提醒：跌破/突破时提醒一次；收回后自动重置；再次有效跌破/突破再提醒
#
# 用法:
#   bash alert_once.sh "触发键" "状态" "提醒内容"
#
# 状态取值:
#   below = 跌破（发提醒）
#   break = 突破（发提醒）
#   above = 收回/回落到关键位内（只重置状态，不提醒）
#   提醒内容为空时 = 只更新状态、不提醒
#
# 状态记录在 data/alert_state.txt（格式：键=状态）
# 需要全部重置时删除 data/alert_state.txt 即可
set -u

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$SKILL_DIR/data/alert_state.txt"
KEY="${1:-}"
ST="${2:-}"
MSG="${3:-}"

if [ -z "$KEY" ] || [ -z "$ST" ]; then
    echo "用法: bash alert_once.sh \"触发键\" \"状态\" \"提醒内容\"" >&2
    exit 1
fi

mkdir -p "$(dirname "$STATE")"

# 读取当前状态
CUR=$(grep "^${KEY}=" "$STATE" 2>/dev/null | head -1 | cut -d= -f2)

# 状态未变 → 跳过
if [ "$CUR" = "$ST" ]; then
    echo "[跳过] $KEY 状态未变($ST)"
    exit 0
fi

# 更新状态
if grep -q "^${KEY}=" "$STATE" 2>/dev/null; then
    sed -i "s|^${KEY}=.*|${KEY}=${ST}|" "$STATE"
else
    echo "${KEY}=${ST}" >> "$STATE"
fi

# 收回/重置，或内容为空 → 只更新状态、不提醒
if [ "$ST" = "above" ] || [ -z "$MSG" ]; then
    echo "[重置] $KEY -> $ST"
    exit 0
fi

# 跌破/突破 → 发提醒
bash "$SKILL_DIR/scripts/notify_feishu.sh" "$MSG"
echo "[已提醒] $KEY -> $ST"

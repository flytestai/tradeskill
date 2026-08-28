#!/bin/bash
# 一次性提醒：同一触发条件只提醒一次，避免重复轰炸
#
# 用法:
#   bash alert_once.sh "触发键" "提醒内容"
#
# 说明:
#   - 触发键记录在 data/alert_state.txt（每行一个）
#   - 已触发过则跳过，不再发提醒
#   - 需要重置时删除 data/alert_state.txt 即可
set -u

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$SKILL_DIR/data/alert_state.txt"
KEY="${1:-}"
MSG="${2:-}"

if [ -z "$KEY" ] || [ -z "$MSG" ]; then
    echo "用法: bash alert_once.sh \"触发键\" \"提醒内容\"" >&2
    exit 1
fi

# 已触发过 → 跳过
if grep -qx "$KEY" "$STATE" 2>/dev/null; then
    echo "[跳过] $KEY 已提醒过"
    exit 0
fi

# 发送提醒
bash "$SKILL_DIR/scripts/notify_feishu.sh" "$MSG"

# 记录已触发
mkdir -p "$(dirname "$STATE")"
echo "$KEY" >> "$STATE"
echo "[已提醒] $KEY"

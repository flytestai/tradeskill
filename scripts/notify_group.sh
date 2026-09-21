#!/bin/bash
# 通过飞书机器人往「群聊」发消息（Card 2.0 卡片），默认发到 VIP 推送群
#
# 用法:
#   bash notify_group.sh "消息内容"                # 发到默认群
#   bash notify_group.sh "消息内容" "群chat_id"     # 发到指定群
#   bash notify_group.sh @消息文件路径 [群chat_id]   # 从文件读取消息（避免命令行编码问题）
#
# 说明:
#   - 用机器人(bot)身份发到群，机器人需在该群里
#   - 统一走 send_card.py（common.send_card）发送 Card 2.0 交互式卡片
#   - 卡片标题从正文首行【】提取，兜底「通知」
set -u

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$SKILL_DIR/data/local_config.env"

MSG="${1:-}"
GROUP_ID="${2:-}"

# 未显式传群 ID 时，从本地配置读取（不入 git）
if [ -z "$GROUP_ID" ]; then
    GROUP_ID="$(grep '^VIP_PUSH_CHAT_ID=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
fi
if [ -z "$GROUP_ID" ]; then
    echo "[CONFIG] 未配置 VIP_PUSH_CHAT_ID（data/local_config.env），群发推送将失败" >&2
fi

if [ -z "$MSG" ]; then
    echo "用法: bash notify_group.sh \"消息内容\" 或 bash notify_group.sh @消息文件 [群ID]" >&2
    exit 1
fi

# 支持从文件读取（@ 开头）
if [ "${MSG#@}" != "$MSG" ]; then
    MSG=$(cat "${MSG#@}")
fi

# 把 \n 转成真实换行（从命令行传参时）
MSG=$(printf '%b' "$MSG")

# 卡片标题：从正文首行【】提取，兜底「通知」
TITLE="$(printf '%s\n' "$MSG" | sed -n '1s/.*【\([^】]*\)】.*/\1/p')"
[ -z "$TITLE" ] && TITLE="通知"

PY_BIN="${SKILL_PYTHON:-python3}"
command -v "$PY_BIN" >/dev/null 2>&1 || PY_BIN=python

# 统一走 send_card.py 发 Card 2.0（内部已含 feishu_client → lark-cli 双通道）
printf '%s' "$MSG" | "$PY_BIN" "$SKILL_DIR/scripts/send_card.py" \
    --chat-id "$GROUP_ID" --title "$TITLE"
rc=$?
exit $rc

#!/bin/bash
# 通过飞书机器人往「群聊」发消息（Markdown 富文本），默认发到荔枝种植交流群
#
# 用法:
#   bash notify_group.sh "消息内容"              # 发到默认群（荔枝种植交流群）
#   bash notify_group.sh "消息内容" "群chat_id"   # 发到指定群
#
# 说明:
#   - 用机器人(bot)身份发到群，机器人需在该群里
#   - 同步发送，timeout 兜底（lark-cli 偶发"发送后进程不退出"，-k 3 强制杀）
set -u

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MSG="${1:-}"
GROUP_ID="${2:-oc_92e9e038a0b4aa5356427e8c2901a970}"

if [ -z "$MSG" ]; then
    echo "用法: bash notify_group.sh \"消息内容\" [群chat_id]" >&2
    exit 1
fi

# 把输入里的 \n 转成真实换行
MSG=$(printf '%b' "$MSG")

# 同步发送，确保消息送达；timeout 兜底杀掉不退出的进程
timeout -k 3 20 lark-cli im +messages-send \
    --chat-id "$GROUP_ID" \
    --as bot \
    --markdown "$MSG" >/dev/null 2>&1

exit 0

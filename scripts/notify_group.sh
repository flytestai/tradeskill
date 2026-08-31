#!/bin/bash
# 通过飞书机器人往「群聊」发消息（Markdown 富文本），默认发到荔枝种植交流群
#
# 用法:
#   bash notify_group.sh "消息内容"                 # 发到默认群
#   bash notify_group.sh "消息内容" "群chat_id"      # 发到指定群
#   bash notify_group.sh @消息文件路径 [群chat_id]    # 从文件读取消息（避免命令行编码问题）
#
# 说明:
#   - 用机器人(bot)身份发到群，机器人需在该群里
#   - 同步发送，timeout 兜底（lark-cli 偶发"发送后进程不退出"，-k 3 强制杀）
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

# 同步发送，确保消息送达；timeout 兜底杀掉不退出的进程
timeout -k 3 20 lark-cli im +messages-send \
    --chat-id "$GROUP_ID" \
    --as bot \
    --markdown "$MSG" >/dev/null 2>&1

exit 0

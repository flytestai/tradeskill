#!/bin/bash
# 通过飞书机器人给用户发送提醒消息（Markdown 富文本，更美观）
#
# 用法:
#   bash notify_feishu.sh "Markdown 提醒内容"
#
# 说明:
#   - 飞书应用 cli_a92579c6ddf9dcb5 的机器人，给用户253172 发私聊
#   - 使用 --markdown 发送，支持 **加粗**、换行、emoji 等富文本样式
#   - 依赖 lark-cli（需先完成 auth login 授权）
#   - lark-cli(node) 偶发"发送成功后进程不退出"，后台发送、脚本立即返回
set -u

USER_OPEN_ID="ou_aa522eed7dac7c0c6bad6d8d3236f0f2"
MSG="${1:-}"

if [ -z "$MSG" ]; then
    echo "用法: bash notify_feishu.sh \"Markdown 提醒内容\"" >&2
    exit 1
fi

nohup lark-cli im +messages-send \
    --user-id "$USER_OPEN_ID" \
    --as bot \
    --markdown "$MSG" >/dev/null 2>&1 &

exit 0

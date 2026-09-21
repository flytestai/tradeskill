#!/bin/bash
# 通过飞书机器人给用户发送提醒消息（Card 2.0 卡片）
#
# 用法:
#   bash notify_feishu.sh "提醒内容" [卡片标题]
#   bash notify_feishu.sh @消息文件路径 [卡片标题]
#
# 说明:
#   - 飞书应用 cli_a92579c6ddf9dcb5 的机器人，给用户253172 发私聊
#   - 统一走 send_card.py（common.send_card）发送 Card 2.0 交互式卡片，
#     与群消息共用同一套卡片构造与双通道（feishu_client 直连 → lark-cli 回退）
#   - 卡片标题：优先取第 2 个参数；否则从正文首行【】中提取；再兜底「提醒」
set -u

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$SKILL_DIR/data/local_config.env"

# 从本地配置读取 open_id（不入 git）；缺失则告警
USER_OPEN_ID="$(grep '^USER_OPEN_ID=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
if [ -z "$USER_OPEN_ID" ]; then
    echo "[CONFIG] 未配置 USER_OPEN_ID（data/local_config.env），私信推送将失败" >&2
fi

MSG="${1:-}"
TITLE="${2:-}"

if [ -z "$MSG" ]; then
    echo "用法: bash notify_feishu.sh \"提醒内容\" [卡片标题]" >&2
    exit 1
fi

# 把输入里的 \n 转成真实换行（bash 双引号不会自动解释 \n）
MSG=$(printf '%b' "$MSG")

# @相对路径：读取文件内容作为消息体。
# market_summary.py 的长播报用 "@data/_market_summary_xxx.txt" 传内容，
# 避免中文/换行经命令行参数被 shell 破坏。相对路径基于 SKILL_DIR 解析。
case "$MSG" in
    @*)
        _file="${MSG#@}"
        case "$_file" in
            /*) ;;
            *) _file="$SKILL_DIR/$_file" ;;
        esac
        if [ -f "$_file" ]; then
            MSG="$(cat "$_file")"
        else
            echo "[ERROR] @文件不存在: $_file" >&2
            exit 1
        fi
        ;;
esac

# 卡片标题：优先显式参数；否则从正文首行【】提取；兜底「提醒」
if [ -z "$TITLE" ]; then
    TITLE="$(printf '%s\n' "$MSG" | sed -n '1s/.*【\([^】]*\)】.*/\1/p')"
    [ -z "$TITLE" ] && TITLE="提醒"
fi

PY_BIN="${SKILL_PYTHON:-python3}"
command -v "$PY_BIN" >/dev/null 2>&1 || PY_BIN=python

# 统一走 send_card.py 发 Card 2.0（内部已含 feishu_client → lark-cli 双通道）
printf '%s' "$MSG" | "$PY_BIN" "$SKILL_DIR/scripts/send_card.py" \
    --user-id "$USER_OPEN_ID" --title "$TITLE"
rc=$?
exit $rc

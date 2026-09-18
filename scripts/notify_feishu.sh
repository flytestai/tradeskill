#!/bin/bash
# 通过飞书机器人给用户发送提醒消息（Markdown 富文本，更美观）
#
# 用法:
#   bash notify_feishu.sh "Markdown 提醒内容"
#
# 说明:
#   - 飞书应用 cli_a92579c6ddf9dcb5 的机器人，给用户253172 发私聊
#   - 使用 --markdown 发送，支持 **加粗**、换行、emoji 等富文本样式
#   - 同步发送，timeout 兜底（lark-cli 偶发"发送后进程不退出"，-k 3 强制杀）
set -u

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$SKILL_DIR/data/local_config.env"

# 从本地配置读取 open_id（不入 git）；缺失则告警
USER_OPEN_ID="$(grep '^USER_OPEN_ID=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
if [ -z "$USER_OPEN_ID" ]; then
    echo "[CONFIG] 未配置 USER_OPEN_ID（data/local_config.env），私信推送将失败" >&2
fi
MSG="${1:-}"

if [ -z "$MSG" ]; then
    echo "用法: bash notify_feishu.sh \"Markdown 提醒内容\"" >&2
    exit 1
fi

# 把输入里的 \n 转成真实换行（bash 双引号不会自动解释 \n）
MSG=$(printf '%b' "$MSG")

# 跨平台定位 lark-cli：
#   1) 显式环境变量 LARK_CLI（Linux systemd EnvironmentFile 用）
#   2) Windows：蜜蜂 npm-global 下的原生 exe（避免 POSIX 包装脚本的 node 子进程不退出）
#   3) PATH 中的 lark-cli（Linux: /usr/local/bin/lark-cli）
resolve_lark() {
    if [ -n "${LARK_CLI:-}" ] && [ -x "${LARK_CLI}" ]; then
        printf '%s' "$LARK_CLI"; return 0
    fi
    if command -v cygpath >/dev/null 2>&1 && [ -n "${APPDATA:-}" ]; then
        _p="$(cygpath -u "$APPDATA" 2>/dev/null)/bee_ai_test/agent-runtime/npm-global/node_modules/@larksuite/cli/bin/lark-cli.exe"
        [ -f "$_p" ] && { printf '%s' "$_p"; return 0; }
    fi
    command -v lark-cli 2>/dev/null
}
LARK="$(resolve_lark)"
if [ -z "$LARK" ]; then
    echo "[CONFIG] 未找到 lark-cli（请安装并加入 PATH，或设置 LARK_CLI）" >&2
    exit 1
fi

timeout -k 3 20 "$LARK" im +messages-send \
    --user-id "$USER_OPEN_ID" \
    --as bot \
    --markdown "$MSG" >/dev/null 2>&1
rc=$?
exit $rc

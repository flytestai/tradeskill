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

# 跨平台定位 lark-cli（同 notify_feishu.sh）：
#   1) 环境变量 LARK_CLI  2) Windows 原生 exe  3) PATH 中的 lark-cli
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
# ---------------------------------------------------------------------------
# 通道选择：优先「纯 Python 直连飞书 API」，回退 lark-cli
#   容器无法创建线程 → Node 崩溃 → lark-cli 在容器内不可用；
#   feishu_client.py 用 urllib（无线程依赖），容器内可正常工作。
# ---------------------------------------------------------------------------
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY_BIN="${SKILL_PYTHON:-python3}"
command -v "$PY_BIN" >/dev/null 2>&1 || PY_BIN=python

if [ -n "${FEISHU_APP_ID:-}" ] && [ -n "${FEISHU_APP_SECRET:-}" ]; then
    echo "$MSG" | "$PY_BIN" "$SKILL_DIR/scripts/feishu_client.py" \
        --to "$GROUP_ID" --type chat_id --text-stdin >/dev/null 2>&1
    rc=$?
    if [ $rc -eq 0 ]; then exit 0; fi
    echo "[WARN] Python 飞书通道失败(rc=$rc)，尝试 lark-cli 回退" >&2
fi

LARK="$(resolve_lark)"
if [ -z "$LARK" ]; then
    echo "[CONFIG] 无可用发送通道：未配置 FEISHU_APP_ID/SECRET，且未找到 lark-cli" >&2
    exit 1
fi

timeout -k 3 20 "$LARK" im +messages-send \
    --chat-id "$GROUP_ID" \
    --as bot \
    --markdown "$MSG" >/dev/null 2>&1
rc=$?

# 把真实退出码返回给调用方，便于盘前播报记录发送失败并重试。
exit $rc

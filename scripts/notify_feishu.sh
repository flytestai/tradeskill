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
# ---------------------------------------------------------------------------
# 通道选择：优先「纯 Python 直连飞书 API」，回退 lark-cli
#
# 为什么优先 Python 通道：
#   目标服务器容器**无法创建线程** → Node 启动即崩 → lark-cli 在容器内不可用。
#   feishu_client.py 用 urllib 直连 OpenAPI（纯同步、无线程），容器内可正常工作。
#   若未配置 FEISHU_APP_ID/SECRET，再回退 lark-cli（宿主机场景）。
# ---------------------------------------------------------------------------
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY_BIN="${SKILL_PYTHON:-python3}"
command -v "$PY_BIN" >/dev/null 2>&1 || PY_BIN=python

if [ -n "${FEISHU_APP_ID:-}" ] && [ -n "${FEISHU_APP_SECRET:-}" ]; then
    echo "$MSG" | "$PY_BIN" "$SKILL_DIR/scripts/feishu_client.py" \
        --to "$USER_OPEN_ID" --type open_id --text-stdin >/dev/null 2>&1
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
    --user-id "$USER_OPEN_ID" \
    --as bot \
    --markdown "$MSG" >/dev/null 2>&1
rc=$?
exit $rc

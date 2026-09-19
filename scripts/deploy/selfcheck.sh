#!/usr/bin/env bash
# ============================================================================
# 服务器侧自监控：本机巡检关键组件，异常时**主动飞书告警**
#
# 为什么需要
#   此前服务器上**没有任何东西会主动告警**：
#     · 业务告警（仓位/关键位）只覆盖行情事件，不覆盖「服务是否活着」
#     · Windows 侧的 kol-platform-health-monitor 能告警，但它
#       **已被禁用**，且依赖本机在线 —— 服务器故障时它未必知道
#   实测踩过多次「静默故障」：CRLF 导致 9 个任务全挂、auth_keepalive
#   NameError 崩了、镜像缺文件导致取数失效 —— **全都是自己发现的，
#   没有任何告警**。本脚本补上这个自举陷阱。
#
# 检查项（全部本机可判，不依赖外部）
#   1. 容器：kolplatform-rest / kolplatform-mcp 是否 running
#   2. REST：/healthz?deep=1 是否 200（含蜜蜂通道探测）
#   3. trade365：backend 容器/进程 + :8000 是否监听
#   4. 关键文件：队列/去重/关键位 是否可解析（损坏即告警）
#   5. 磁盘：使用率是否 > 90%
#
# 告警策略
#   · 复用 alert_once.sh：**同一故障只告警一次**，恢复后才重置
#     （避免每 5 分钟轰炸一次）
#   · 用 notify_feishu.sh（bot 身份私信），bot token 无 7 天限制
#
# 用法：
#   bash selfcheck.sh            # 巡检 + 异常告警
#   bash selfcheck.sh --quiet    # 只巡检，不告警（人工看）
#   bash selfcheck.sh --json     # 结构化输出
# ============================================================================
set -uo pipefail

KOL_DIR="/opt/kol-skills-platform"
PY="$KOL_DIR/.venv-host/bin/python"

QUIET=0
JSON=0
for a in "$@"; do
    case "$a" in
        --quiet) QUIET=1 ;;
        --json)  JSON=1 ;;
    esac
done

# 加载配置（告警出口需要 USER_OPEN_ID 等）
_load_env() {
    local f="$1"; [ -f "$f" ] || return 0
    local t; t="$(mktemp)"
    tr -d '\r' < "$f" > "$t"
    set -a; . "$t" 2>/dev/null; set +a; rm -f "$t"
}
_load_env "$KOL_DIR/.env"
_load_env "$KOL_DIR/data/local_config.env"
export PATH="/opt/node20/bin:/usr/local/bin:/usr/bin:/bin"
export TZ=Asia/Shanghai

problems=""

_add() { problems="${problems}${problems:+, }$1"; }

# ---- 1. 容器 ---------------------------------------------------------------
for c in kolplatform-rest kolplatform-mcp; do
    st="$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null || echo missing)"
    [ "$st" = "running" ] || _add "$c=$st"
done

# ---- 2. REST 深度健康 -------------------------------------------------------
hz="$(curl -s -o /dev/null -w '%{http_code}' -m 30 \
      'http://127.0.0.1:8020/healthz?deep=1' 2>/dev/null || echo 000)"
case "$hz" in
    200|207) ;;
    *) _add "REST healthz=$hz" ;;
esac

# ---- 3. trade365 -----------------------------------------------------------
t365="$(curl -s -o /dev/null -w '%{http_code}' -m 30 \
        'http://127.0.0.1:8000/api/overview' 2>/dev/null || echo 000)"
[ "$t365" = "200" ] || _add "trade365=$t365"

# ---- 4. 关键状态文件可解析 ---------------------------------------------------
for f in group_qa_queue.json group_qa_answered.json level_targets.json \
         price_alerts.json; do
    p="$KOL_DIR/data/$f"
    [ -f "$p" ] || continue
    "$PY" -c "
import json,sys
try: json.load(open('$p', encoding='utf-8'))
except Exception: sys.exit(1)
" 2>/dev/null || _add "$f 损坏"
done

# ---- 5. 磁盘 ---------------------------------------------------------------
use="$(df / | tail -1 | awk '{print $5}' | tr -d '%')"
[ -n "$use" ] && [ "$use" -gt 90 ] 2>/dev/null && _add "磁盘=${use}%"

# ---- 输出 ------------------------------------------------------------------
if [ -n "$problems" ]; then
    if [ "$JSON" = "1" ]; then
        echo "{\"ok\":false,\"problems\":\"$problems\"}"
    else
        echo "[$(date '+%F %T')] ❌ 自检异常: $problems"
    fi
    if [ "$QUIET" != "1" ]; then
        # 同一故障只告警一次（内容变化才重新告警 → 恢复后自然重置）
        bash "$KOL_DIR/scripts/alert_once.sh" "selfcheck" "$problems" \
            "🚨 **【服务器自检异常】**
$problems

时间：$(date '+%F %T')
（同一故障只提醒一次，恢复正常后自动重置）" >/dev/null 2>&1 || true
    fi
    exit 1
fi

if [ "$JSON" = "1" ]; then
    echo '{"ok":true,"problems":""}'
else
    echo "[$(date '+%F %T')] ✅ 自检正常（容器/REST/trade365/状态文件/磁盘）"
fi
# 恢复正常 → 重置告警状态，下次故障会重新告警
bash "$KOL_DIR/scripts/alert_once.sh" "selfcheck" "ok" "" >/dev/null 2>&1 || true
exit 0

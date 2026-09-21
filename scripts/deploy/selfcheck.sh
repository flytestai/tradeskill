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
#   6. 定时任务：是否停摆 / 是否因时区错位而在错误时刻执行（本轮新增）
#   7. LLM：主动拨测 Kimi，识别"账号欠费停用"这类无症状故障（本轮新增）
#
# 告警策略
#   · 复用 alert_once_private.sh：**同一故障只告警一次**，恢复后才重置
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
# 2026-09-20 适配变更：kol 平台从「Docker 容器」改为「systemd 服务」
#   旧架构：docker run 两个容器（kolplatform-rest / kolplatform-mcp），REST 在 8020
#   新架构：systemd 托管 kol-platform（REST 8020）+ trade365（8000）+ nginx/docker/cron
#   检查方式随之由 `docker inspect` 改为 `systemctl is-active`。
#   （新服务器基于 Ubuntu 22.04，kol 平台跑在 systemd 下，见服务器重建报告）
for svc in kol-platform kol-platform-mcp trade365 nginx docker cron; do
    st="$(systemctl is-active "$svc" 2>/dev/null)"
    [ "$st" = "active" ] || _add "$svc=$st"
done

# ---------------------------------------------------------------------------
# 带重试的 HTTP 探测（REST 与 trade365 共用）
#
# ⚠️ 两个坑都踩过，必须同时避免：
#
#  1) `curl ... || echo 000` 会拼成 "000000"
#     curl 失败时 `-w '%{http_code}'` 已经输出 "000"，
#     再 `|| echo 000` 又多输出一次 → 得到 6 个 0 的畸形值。
#     （第五轮在 preflight 修过同一写法，却漏了这个文件 —— 于是
#      自监控持续误报 "REST healthz=000000"，而 REST 其实一直是 200。）
#
#  2) 单次探测失败就判故障 → 误报
#     实测该容器为单线程（宿主限制），长请求会短暂阻塞探针；
#     一次超时不代表服务挂了。必须**重试 N 次**才判失败。
#     实测反例：报 "000000" 的那一刻，REST 直连 200、耗时 4ms、
#     容器 0 重启、访问日志里该请求也返回了 200 —— 纯属探针自身抖动。
# ---------------------------------------------------------------------------
_probe_http() {
    # $1=url  $2=超时秒  $3=尝试次数
    local url="$1" tmo="${2:-20}" tries="${3:-3}" i code
    for i in $(seq 1 "$tries"); do
        code="$(curl -s -o /dev/null -w '%{http_code}' -m "$tmo" "$url" 2>/dev/null)"
        [ -n "$code" ] || code="000"
        [ "$code" != "000" ] && { printf '%s' "$code"; return 0; }
        [ "$i" -lt "$tries" ] && sleep 2
    done
    printf '%s' "${code:-000}"
}

# ---- 2. REST 深度健康 -------------------------------------------------------
hz="$(_probe_http 'http://127.0.0.1:8020/healthz?deep=1' 25 3)"
case "$hz" in
    200|207) ;;
    *) _add "REST healthz=$hz（已重试3次）" ;;
esac

# ---- 3. trade365 -----------------------------------------------------------
t365="$(_probe_http 'http://127.0.0.1:8000/api/overview' 40 5)"
[ "$t365" = "200" ] || _add "trade365=$t365（已重试5次）"

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

# ---------------------------------------------------------------------------
# ---- 6. 定时任务「是否在预期时刻执行」（本轮新增，针对一个真实静默故障）----
#
# ⚠️ 为什么必须加（2026-09-19 实测发现）
#   宿主机时区是 **US/Eastern**，而 crontab 的小时字段是**北京时间口径**
#   （45 8 = 盘前、0 11 = 盘中、*/5 9-15 = 盘中持仓监控…），
#   且 Debian 的 cron（vixie 3.0pl1）**忽略 TZ/CRON_TZ 用于调度**
#   （官方文档：cron ignores it other than passing it on through）。
#
#   实证：9/18（周五）日志显示
#       monitor-alerts 实际 21:40 北京 = 09:40 EDT
#   → 所有交易时段任务**整体晚约 12 小时**：
#       盘前 20:45（收盘后）、盘中 23:00（盘后）、
#       关键位 20:30、持仓监控 21:00~03:59（半夜）
#
#   最要命的不是错位本身，而是**它完全没有症状**：
#   任务照常执行、退出码全 0，自监控 5 项检查全 ✅。
#   本项检查就是补上这个盲区 —— 让"任务没在该跑的时候跑"变成告警。
#
# 判定方式：取任务日志里最新一条记录的【北京时间时刻】，
#   若落在预期的交易时段之外且当天已过预期时点，则告警。
# ---------------------------------------------------------------------------
_host_log="$KOL_DIR/data/_host_task.log"
if [ -f "$_host_log" ]; then
    # 日志时间戳由脚本按北京时间写入 → 直接解析即可
    last_ts="$(grep -oE '^--- [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' "$_host_log" | tail -1 | cut -d' ' -f2-)"
    if [ -n "$last_ts" ]; then
        last_epoch="$(date -d "$last_ts" +%s 2>/dev/null || echo 0)"
        now_epoch="$(date -d "$(TZ=Asia/Shanghai date '+%F %T')" +%s 2>/dev/null || echo 0)"
        if [ "$last_epoch" -gt 0 ] && [ "$now_epoch" -gt 0 ]; then
            age=$(( now_epoch - last_epoch ))
            # 常态任务（qa / react-cleanup）每 2~10 分钟一次，
            # 故超过 30 分钟没有任何记录 = 定时任务链路已停摆
            [ "$age" -gt 1800 ] && _add "定时任务停摆：日志最新记录距今 $((age/60)) 分钟（$_host_log）"
        fi

        # ★ 时区错位检测：交易时段任务的触发时刻应落在大致 08:00~16:30（北京）。
        #   查看当天「交易时段类」任务记录，若它们集中在 20:00~04:00
        #   说明 cron 用错了时区（晚 12h）。
        trade_ts="$(grep -E '(premarket|intraday|position-monitor|monitor-alerts|summary-close|level)' "$_host_log" \
                    | grep -oE '^--- [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' \
                    | tail -1 | awk '{print $3}')"
        if [ -n "$trade_ts" ]; then
            hh="${trade_ts%%:*}"
            if [ "$hh" -ge 19 ] || [ "$hh" -lt 5 ] 2>/dev/null; then
                _add "定时任务时区错位：交易类任务最近触发于北京时间 ${trade_ts}（应落在 08:00~16:30）—— 宿主机时区=$(cat /etc/timezone 2>/dev/null)，cron 忽略 CRON_TZ"
            fi
        fi
    fi
fi

# ---------------------------------------------------------------------------
# ---- 7. LLM（Kimi）可用性（本轮新增，针对一起"无症状"的生产故障）----------
#
# ⚠️ 为什么必须加（2026-09-19 实测发现）
#   实测发现 Kimi 账号**因余额不足被停用**：
#     HTTP 429 "account ... is suspended due to insufficient balance,
#               type: exceeded_current_quota_error"
#
#   而它在系统里**完全没有症状**：
#     · 队列 0 条（没人提问 → 没触发调用）
#     · 日志里那条 429 是**组织级 3 RPM 限流**（type/文案完全不同），
#       跟"余额停用"是两回事 —— 看日志根本发现不了
#     · 且平台**只有 kimi 一个供应商，没有备用**
#   → 一旦有人提问，会静默失败；而没有任何东西会告警。
#
#   本项主动拨测一次（开销极小），并区分两类 429：
#     · insufficient balance / quota  → 需要充值（严重，需人工）
#     · 限流（max organization ...）  → 瞬时，自愈，不告警
# ---------------------------------------------------------------------------
# 2026-09-22 修复：self-monitor 每 10 分钟一轮，原先每轮都真实调用 LLM 拨测：
#   · 烧 token（~7 token/次 x 144 次/天）
#   · 拨测超时（90s）会误报「服务器自检异常」并发飞书（本次事故根因）
# 改为：SKIP_LLM_PROBE=1 时只做零成本配置检查（不发真实请求）；
#   真实拨测交给独立低频任务（daily-llm-probe，每天一次）。
SKIP_LLM_PROBE="${SKIP_LLM_PROBE:-1}"
if [ "$SKIP_LLM_PROBE" = "1" ]; then
    _llm_out="$(cd "$KOL_DIR" && set -a && . ./.env 2>/dev/null && set +a && \
        timeout 15 "$PY" - <<'PYEOF' 2>/dev/null
import sys
sys.path.insert(0, "scripts")
try:
    import llm_client
    print("OK" if llm_client.is_configured() else "NOT_CONFIGURED")
except Exception as e:
    print("IMPORT_FAIL", str(e)[:80])
PYEOF
    )"
else
_llm_out="$(cd "$KOL_DIR" && set -a && . ./.env 2>/dev/null && set +a && \
    timeout 90 "$PY" - <<'PYEOF' 2>/dev/null
import sys
sys.path.insert(0, "scripts")
try:
    import llm_client
except Exception as e:
    print("IMPORT_FAIL", str(e)[:80]); sys.exit(0)
try:
    if not llm_client.is_configured():
        print("NOT_CONFIGURED"); sys.exit(0)
except Exception as e:
    print("CHECK_FAIL", str(e)[:80]); sys.exit(0)
try:
    llm_client.chat("ok", max_tokens_=5, retries=0)
    print("OK")
except Exception as e:
    print("CALL_FAIL", str(e)[:300])
PYEOF
)"

fi

case "$_llm_out" in
    OK) ;;
    NOT_CONFIGURED)
        _add "LLM 未配置（群问答将全部失败）" ;;
    "")
        _add "LLM 拨测无输出（可能超时）" ;;
    *insufficient\ balance*|*exceeded_current_quota*|*suspended*)
        _add "🔴 LLM 账号被停用/欠费 —— 群问答已失效，需充值或更换账号" ;;
    *max\ organization*|*rate*limit*|*429*)
        ;;   # 限流属瞬时，不告警（历史上多为组织级 3 RPM）
    *)
        _add "LLM 调用异常: $(printf '%s' "$_llm_out" | cut -c1-120)" ;;
esac

# ---- 输出 ------------------------------------------------------------------
if [ -n "$problems" ]; then
    if [ "$JSON" = "1" ]; then
        echo "{\"ok\":false,\"problems\":\"$problems\"}"
    else
        echo "[$(date '+%F %T')] ❌ 自检异常: $problems"
    fi
    if [ "$QUIET" != "1" ]; then
        # 同一故障只告警一次（内容变化才重新告警 → 恢复后自然重置）
        bash "$KOL_DIR/scripts/alert_once_private.sh" "selfcheck" "$problems" \
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
    # ⚠️ 文案必须与**实际执行的检查项**一致。
    #    此前固定写「容器/REST/trade365/状态文件/磁盘」，而本轮已扩到 7 项 ——
    #    文案不更新就会出现"报告说查了,其实没查"的误导，
    #    这正是本会话反复出现的那类问题（检查项自述 ≠ 实际）。
    echo "[$(date '+%F %T')] ✅ 自检正常（容器 / REST / trade365 / 状态文件 / 磁盘 / 定时任务 / LLM）"
fi
# 恢复正常 → 重置告警状态，下次故障会重新告警
bash "$KOL_DIR/scripts/alert_once_private.sh" "selfcheck" "ok" "" >/dev/null 2>&1 || true
exit 0

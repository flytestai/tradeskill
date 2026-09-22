#!/usr/bin/env bash
# ============================================================================
# 宿主机定时任务安装（替代 Windows 的 supervisor）—— **唯一**登记入口
#
# 迁移自 Windows supervisor.py：
#   DAILY_AT   08:45 盘前播报 / 11:00 盘中播报 / 14:55 & 16:00 兜底同步
#   PERIODIC   持仓监控(5min) / 告警监控(10min) / 敲键盘清理(5min)
#              授权保活(6h)
#
# 为什么跑宿主机而非容器：
#   这些脚本要调 lark-cli（读群消息 / 发消息），而容器无法创建线程 →
#   Node 崩溃 → lark-cli 不可用。宿主机有 Node 20 + Python 3.11，可用。
#   （容器内的纯 Python 通道负责发消息；读消息必须用宿主 lark-cli。）
#
# ★ 2026-09-20 变更：收敛为「1 个 runner」
# ----------------------------------------------------------------------------
# 本脚本此前只登记 9 条任务，群问答由**另一个**脚本 setup_qa_24x7.sh 生成
# **另一个** runner（_run_qa.sh）并登记 **另一个标记**（# kol-platform-qa）。
# 两个 runner 的代价（实测）：
#   · 两套标记 → 登记/卸载必须成对，漏一个就留下幽灵任务
#   · 守卫语义不一致 → `1-5` 挡不住法定节假日，监控整天空转
#   · 群问答链路的存在与否，在 setup_host_tasks.sh 里完全看不出来
# 现统一为：**一个 runner（_run_task.sh）+ 一个标记（# kol-platform-task）**，
# 本脚本是唯一登记入口（setup_qa_24x7.sh 已降级为兼容转发，见该文件）。
#
# 交易日守卫
# ----------------------------------------------------------------------------
# 由 runner 的 `--trading` 选项统一实现（判定逻辑见 _run_task.sh 注释）。
# ⚠️ 与 runner 一致：**默认不守卫**，仅对交易语义任务显式加 --trading。
# ⚠️ **默认不加守卫** —— 这是刻意的安全默认：
#    忘记加 `--trading` 的后果是「非交易日多跑一次」（脚本自带守卫时无副作用），
#    而如果反过来默认加守卫，忘记加 `--anyday` 会让**自监控/备份在节假日静默停摆**
#    —— 这正是本项目反复踩到的「静默失效」类故障，代价大得多。
#    故：需要跳过节假日的任务必须**显式**写明 --trading。
#
# 用法：
#   sudo bash scripts/deploy/setup_host_tasks.sh
#   sudo bash scripts/deploy/setup_host_tasks.sh --uninstall
# ----------------------------------------------------------------------------
set -uo pipefail

DEPLOY_DIR="/opt/kol-skills-platform"
NODE_BIN="/opt/node20/bin"
VENV="$DEPLOY_DIR/.venv-host"
LOG_DIR="$DEPLOY_DIR/data"
# 统一任务标记：**所有**平台定时任务（含群问答）都带它。
# ⚠️ 清理时必须用**同一标记**，否则会误删其他脚本注册的任务
#    （曾因 setup_host_sync.sh 用 # kol-platform-sync、本脚本用 # kol-host-task，
#      导致清理时互相删掉对方 —— 已修复为共用标记）。
MARK="# kol-platform-task"
# ⚠️ 任务名必须为纯 ASCII：cron 在 C locale 下处理非 ASCII（中文）会产生
#    非法字节，导致 crontab 拒绝写入**整批**任务（已实测踩坑）。

# 历史标记：群问答曾用独立 runner（_run_qa.sh）与独立标记注册。
# ⚠️ 必须一并清理 —— 否则「清理本脚本标记 → 重新登记」之后，
#    旧 QA 行仍在 crontab 里，于是 **_run_task.sh --qa 与 _run_qa.sh 同时消费
#    同一条队列 → 用户被重复回复**（这正是合并 runner 要消除的问题）。
LEGACY_MARK="# kol-platform-qa"
LEGACY_RUNNER="$DEPLOY_DIR/scripts/deploy/_run_qa.sh"

_purge_legacy() {
    crontab -l 2>/dev/null | grep -v "$LEGACY_MARK" | crontab - || true
    if [ -f "$LEGACY_RUNNER" ]; then
        rm -f "$LEGACY_RUNNER"
        echo "  ✅ 已清理历史 runner _run_qa.sh 与标记 $LEGACY_MARK"
    fi
}

if [ "${1:-}" = "--uninstall" ]; then
    crontab -l 2>/dev/null | grep -vE "$MARK|$LEGACY_MARK" | crontab - || true
    [ -f "$LEGACY_RUNNER" ] && rm -f "$LEGACY_RUNNER"
    echo "  ✅ 已移除全部宿主机定时任务（含历史群问答任务）"
    crontab -l 2>/dev/null | grep -v "^#" | sed 's/^/     /' || echo "     (crontab 为空)"
    exit 0
fi

[ -x "$VENV/bin/python" ] || { echo "  ❌ 未找到 $VENV，请先运行 setup_host_sync.sh"; exit 1; }

echo "=== 0. 强制时区 Asia/Shanghai ==="
bash "$DEPLOY_DIR/scripts/deploy/force_tz.sh"

echo "=== 1. 安装任务运行器（唯一入口）==="
RUNNER="$DEPLOY_DIR/scripts/deploy/_run_task.sh"
SRC="$(cd "$(dirname "$0")" && pwd)/_run_task.sh"
if [ -f "$SRC" ]; then
    install -m 755 "$SRC" "$RUNNER"
    echo "  ✅ $RUNNER"
else
    echo "  ❌ 未找到 $SRC（runner 随仓库分发，不再由本脚本内联生成）"; exit 1
fi

echo ""
echo "=== 2. 注册定时任务 ==="
_purge_legacy
TMP="$(mktemp)"
crontab -l 2>/dev/null | grep -v "$MARK" > "$TMP" || true

# emit <cron 时间字段> <任务名> <守卫> <runner 选项> [脚本/参数...]
#   守卫       : trading = 仅交易日（runner --trading）；其余值 = 每天执行
#   runner 选项: 传给 runner 本身的开关（--lock / --qa 等），**可以为空串**
#
# ⚠️ runner 选项必须排在**任务名之前**（2026-09-20 实测踩坑）
#   runner 的参数解析遇到第一个非选项参数就 `break`（即任务名），
#   其后的内容原样交给 python。所以把 `--qa` 写在任务名之后会变成：
#       _run_task.sh qa-poll --qa   →   python qa-poll --qa
#       →  python: unknown option --qa（退出码 2，任务静默失效）
#   故这里把「runner 选项」与「脚本参数」分成两段拼装，顺序固定为
#       <时间> <RUNNER> <--trading?> <runner选项> <任务名> <脚本 参数...>
#
# ⚠️ 命令体里**不要出现裸 `%`**（crontab 会把第一个裸 % 之后的内容当 stdin，
#    截断命令）。现在所有任务都指向**不含 `%` 的脚本路径**，这个问题自然消失
#    —— 这也是把 `$(date +%F)` 那类逻辑提升为独立 .sh 脚本的原因之一
#    （见 full_backup.sh）。若将来确有需要，直接在命令里写 `\%`。
emit() {
    local when="$1"; shift
    local name="$1"; shift
    local guard="$1"; shift
    local opts="$1"; shift
    [ "$guard" = "trading" ] && opts="--trading $opts"
    printf '%s %s %s %s %s >/dev/null 2>&1  %s\n' \
        "$when" "$RUNNER" "$opts" "$name" "$*" "$MARK" >> "$TMP"
}

# ---- 每日定点（脚本内部同样有交易日守卫，这里是双保险）----
emit "30 8 * * 1-5"    "level-refresh"   trading  ""       "scripts/level_refresh.py"
emit "45 8 * * 1-5"    "premarket"       trading  ""       "scripts/market_summary.py" "--premarket"
emit "0 11 * * 1-5"    "intraday"        trading  ""       "scripts/market_summary.py" "--intraday"
emit "30 14 * * 1-5" "afternoon" trading "" "scripts/market_summary.py" "--afternoon"
emit "5 15 * * 1-5"    "summary-close"   trading  ""       "scripts/market_summary.py"
emit "55 14 * * 1-5"   "sync-preclose"   trading  ""       "scripts/sync_feishu_auto.py" "--force"
emit "0 16 * * 1-5"    "sync-afterclose" trading  ""       "scripts/sync_feishu_auto.py" "--force"

# ---- 盘中周期（脚本内部有交易日守卫；--trading 挡掉节假日空转）----
emit "*/5 9-15 * * 1-5"  "position-monitor" trading "--lock" "scripts/position_monitor.py" "--notify"
emit "*/10 9-15 * * 1-5" "monitor-alerts"   trading "--lock" "scripts/monitor_alerts.py"

# ---- 盘中高频链路（交易日 9-16 每分钟；脚本内部有交易日守卫）----
# price-alerts：价格提醒检查（--trading 挡节假日，check 内部再按订阅过滤）
# feishu-intraday-poll：wu2198 盘中观点同步 + VIP 推送
emit "*/1 9-16 * * 1-5" "price-alerts"         trading "--lock" "scripts/price_alerts.py" "check"
emit "*/1 9-16 * * 1-5" "feishu-intraday-poll" trading "--lock" "scripts/sync_feishu_auto.py"

# ---- 全天周期（无交易日语义 → always）----
# 敲键盘表情清理 / 授权保活 / 自监控 / trade365 自愈，周末与节假日都必须照跑
emit "*/5 * * * *"   "react-cleanup"     always  ""       "scripts/react.py" "cleanup"
emit "0 */6 * * *"   "auth-keepalive"    always  ""       "scripts/auth_keepalive.py"
emit "*/10 * * * *"  "self-monitor"      always  "--bash" "scripts/deploy/selfcheck.sh"
emit "*/5 * * * *"   "trade365-selfheal" always  "--bash" "/opt/trade365-bot/_run_trade365.sh"

# ---- 公网 MCP 链路验收（每日一次）----
# ⚠️ 为什么需要独立任务：MCP 公网链路有 4 层各自独立的坑（Nginx 路由 /
#    DNS-rebinding 白名单 / 鉴权模式 / 日志落点），任何一层坏掉都是
#    「端点看起来正常、实际不可用」。selfcheck.sh 只查本机，
#    覆盖不到公网路径（2026-09-19 那次「18 个工具全挂」正是这么漏掉的）。
#    每日跑一次，失败会经 runner 落日志（退出码非 0 → _task_errors.log）。
emit "0 8 * * *"     "mcp-public-check"  always  "--bash" "scripts/deploy/verify_mcp_public.sh"

# ---- 备份（每天；与是否交易日无关）----
emit "30 23 * * *"   "data-backup"       always  "--bash" "scripts/deploy/backup_data.sh"
emit "45 23 * * *"   "github-sync"       always  "--bash" "scripts/deploy/push_sync.sh"
# 完整打包：逻辑已提升为独立脚本 full_backup.sh（含校验 + 轮转 + 自检输出）
# ⚠️ 为什么不再内联在 crontab 里（详见该脚本头部注释）：
#    · `%` 一旦忘了转义，crontab 会在那里截断命令（且不报错）
#    · 内联命令里的 `&&`/`;` 会被 **cron 自己的 shell** 拆开 → 不经过 runner，
#      因而没有任何日志、校验与退出码回收（"看起来在跑"最危险）
#    · 备份逻辑无法脱离 crontab 单独验证
emit "0 3 * * *"     "full-backup"       always  "--bash" "scripts/deploy/full_backup.sh"

# ---- 年度维护（12 月 1/15 提醒：下一年休市日需录入 data/holidays.txt）----
emit "0 10 1 12 *"   "holidays-remind"   always  "--bash" "scripts/deploy/holidays_remind.sh"
emit "0 10 15 12 *"  "holidays-remind"   always  "--bash" "scripts/deploy/holidays_remind.sh"

# ---- 群问答（24×7，**不守卫**：周末与节假日也要回复）----
# ⚠️ 为什么 --qa：
#    · 24×7：群问答不按交易时段，深夜/周末用户提问也要回 → 不加 --trading
#    · --qa 隐含 --lock：单次最长可达数分钟，而 cron 每 2 分钟触发，
#      必须防重入，否则两个进程同抢一个队列会**重复回复**、并发调 LLM 会 429
#    · --qa-poll 内置「先溜队列→再拉取→再分析」的三步顺序，见 _run_task.sh
#    · 末尾**不接脚本名**：--qa-poll 分支自带完整链路（多传参数会被忽略）
emit "*/1 * * * *"   "qa-poll"           always  "--qa-poll"

crontab "$TMP"
rm -f "$TMP"

echo "  已注册任务："
crontab -l 2>/dev/null | grep "$MARK" | sed "s|$RUNNER||; s|  $MARK||" | sed 's/^/     /'

echo ""
echo "=== 3. 首次验证（实跑两条关键链路）==="
# ⚠️ 验证时强制写日志（_RUN_TASK_LOG=1），否则非交易时段/静默任务会「无输出」，
#    看起来像失败 —— 实际是设计如此。这里必须留痕，故覆盖静默开关。
echo "  --- 持仓监控 ---"
_RUN_TASK_LOG=1 bash "$RUNNER" "verify-position-monitor" "scripts/position_monitor.py" "--notify" || true

# 群问答链路单独验证：这是**最容易静默断裂**的一条（曾出现
# 「队列恒空、用户 @机器人 永远没人回」），且它默认静默、出问题时看不见。
# 故这里用 --qa-verbose 强制出日志，把「三步是否都执行了」摊开。
echo "  --- 群问答链路（--qa-verbose）---"
timeout 240 bash "$RUNNER" --qa-verbose "verify-qa" || echo "     ⚠️ 问答链路返回非 0，见下方日志"

echo "  最近日志："
tail -14 "$LOG_DIR/_host_task.log" 2>/dev/null | sed 's/^/     /'

echo ""
echo "=== 完成 ==="
echo "  统一日志：$LOG_DIR/_host_task.log"
echo "  错误日志：$LOG_DIR/_task_errors.log（仅高频静默任务出错时写入）"
echo "  手动触发：bash $RUNNER <名称> <脚本> [参数]"
echo "  卸载    ：bash $0 --uninstall"

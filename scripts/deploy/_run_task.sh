#!/usr/bin/env bash
# ============================================================================
# 宿主机定时任务 —— **唯一**入口（由 setup_host_tasks.sh 生成，勿手改）
#
# 用法：
#   _run_task.sh [选项] <任务名> [命令/参数...]
#
# 选项（可组合，顺序无关）：
#   --trading     只在交易日执行（守卫，见下）；**不加则每天都执行**
#   --lock        加单实例锁（高频任务，防上一轮未结束又起一轮）
#   --qa-poll     群问答链路（内置「溜队列 → 拉取 → 分析」+ 隐含 --lock）
#   --qa-verbose  同上，但**输出写日志**（手工排障用；见下「静默策略」）
#   --bash        用 bash 执行脚本（.sh 任务）
#   --shell       把参数当整条 shell 命令执行（⚠️ 慎用，见下）
#   默认          用 .venv-host 的 python 执行脚本
#
# ⚠️ --shell 的陷阱（2026-09-20 实测，因此**不再有 cron 任务使用它**）
#   cron 是用 `sh -c "<整行>"` 执行的，所以你在 crontab 里写的
#       _run_task.sh j --shell cd /opt/x && tar ... ; find ... -delete
#   **先被 cron 的 shell 拆开**，`&&` 与 `;` 不会传进 runner，实际变成：
#       ① runner j --shell cd /opt/x          ← 只跑了这一段
#       ② && tar ...                          ← 绕过 runner 直接执行
#       ③ ;  find ... -delete >/dev/null 2>&1  ← 重定向错位
#   于是 ②③ 虽然"碰巧能跑"，但**完全绕过**了日志、单实例锁、交易日守卫、
#   错误落盘 —— 而且它看起来是工作的，属于最难发现的一类故障。
#   结论：**需要多步逻辑就写成独立 .sh 脚本，用 --bash 调用**
#   （参见 scripts/deploy/full_backup.sh 的头部注释）。
#   --shell 仅保留给「单个不含 shell 元字符的命令」使用。
#
# ⚠️ 守卫为什么是「显式开启」而不是默认开启（刻意的安全默认）
#   忘记加 --trading 的后果：非交易日**多跑一次**——
#     而 market_summary / sync_feishu 等脚本**自带**交易日守卫会自行跳过，
#     position_monitor / monitor_alerts 有状态去重、节假日无新数据即无动作，
#     trade365 是幂等自愈 → 实际无副作用，只多一次进程启动。
#   反过来若默认开启守卫，忘记加 --anyday 会让**自监控 / 备份在节假日静默停摆**
#     ——那正是本项目反复踩到的「静默失效」类故障，代价大得多。
#   故：仅交易语义的任务显式写 --trading。
#
# 为什么只用「1 个 runner」（2026-09-20 合并）
# ---------------------------------------------------------------------------
# 此前有**两个** runner，分别由 setup_host_tasks.sh 与 setup_qa_24x7.sh 生成：
#     _run_task.sh   交易时段任务   无守卫（靠 cron 的 1-5 兜底，节假日照跑）
#     _run_qa.sh     群问答 24×7    无守卫（设计如此）
# 两个 runner 带来三个真实问题：
#   1) 两套 cron 标记（# kol-platform-task / # kol-platform-qa），登记与
#      卸载必须成对执行，漏一个就留下「幽灵任务」（实测已发生）
#   2) 守卫语义不一致 —— `*/5 9-15 * * 1-5` 在**法定节假日**照常触发，
#      仓位/关键位监控整天空转（每次都要联网取行情才发现无事可做）
#   3) QA 链路在非交易日仍高频拉取飞书 API（720 次/天），只增无谓调用
# 合并后：**一个 runner + 一个标记**，守卫统一在本文件内实现，
# 各 setup 脚本只负责「登记任务行」，不再各自生成 runner。
#
# 交易日守卫（统一实现）
# ---------------------------------------------------------------------------
# 为什么守卫放在 runner 里，而不是靠 cron 的 `* * 1-5`：
#   `1-5` 只排除周末，**不排除法定节假日**。节假日是「工作日」，cron 照跑。
#   实测代价：非交易日仍会真实调用行情/飞书接口，且日志被 cron 丢弃，
#   出问题完全不可见。故这里在 runner 层统一先判交易日再执行。
#
#   ⚠️ 刻意**不直接调 common.is_trading_day()** —— 那需要先 import
#      requests/bs4，在 `*/5` 的高频路径上纯属浪费。这里用与
#      `scripts/common.py` **同一份** data/holidays.txt 与同一套语义
#      （周一~周五且不在 holidays.txt 中，北京时间）复现，零依赖。
#      若两者语义漂移，preflight 会以「runner 与 common 语义一致」断言拦下。
# ============================================================================
set -u
DEPLOY_DIR="/opt/kol-skills-platform"
export PATH="/opt/node20/bin:/usr/local/bin:/usr/bin:/bin"
export TZ=Asia/Shanghai
cd "$DEPLOY_DIR" || exit 1

PY="$DEPLOY_DIR/.venv-host/bin/python"

# ---- 配置加载（.env 优先，回退 data/local_config.env）----------------------
# ⚠️ 必须在解析 --qa 等选项**之前**加载：万一将来选项默认值改成读环境变量，
#    顺序颠倒会静默取到空值。当前选项均为字面量，此处顺序仅为防御。
# ⚠️ 必须剥离 CRLF 后再 source：配置文件若在 Windows 上编辑过会带回车符，
#    source 时该回车会被当成命令的一部分报「command not found」，
#    且变量值尾部多出一个回车字符
#    （实测导致 chat_id 长度 36 而非 35，飞书 API 报 invalid receive_id）。
_load_env() {
    local f="$1"
    [ -f "$f" ] || return 0
    local tmp
    tmp="$(mktemp)"
    tr -d '\r' < "$f" > "$tmp"
    set -a
    # shellcheck disable=SC1090
    . "$tmp"
    set +a
    rm -f "$tmp"
}
_load_env ./.env
_load_env ./data/local_config.env

# ---- 交易日判定（北京时间；与 common.is_trading_day 同语义）-----------------
_is_trading_day() {
    local bj dow today
    bj="$(TZ=Asia/Shanghai date '+%u %Y-%m-%d')" || return 0
    dow="${bj%% *}"; today="${bj##* }"
    [ "$dow" -le 5 ] || return 1          # 1=周一 … 7=周日
    local hol="$DEPLOY_DIR/data/holidays.txt"
    [ -f "$hol" ] || return 0             # 无节假日表则只按周末判定
    local d
    while IFS= read -r d; do
        [ -n "$d" ] || continue
        case "$d" in \#*) continue ;; esac
        [ "$d" = "$today" ] && return 1   # 精确整行匹配：2026-01-01
    done < "$hol"
    return 0
}

# ---- 参数解析 ---------------------------------------------------------------
MODE="python"; TRADING_ONLY=0; WANT_LOCK=0; FORCE_LOG=0
while [ $# -gt 0 ]; do
    case "$1" in
        --trading)    TRADING_ONLY=1; shift ;;
        --lock)       WANT_LOCK=1; shift ;;
        # --qa-poll 隐含 --lock：群问答单次最长数分钟，而 cron 每 2 分钟触发，
        # 不防重入会「两个进程抢同一队列 → 重复回复 + LLM 429」。
        --qa-poll)    MODE="qa"; WANT_LOCK=1; shift ;;
        --qa-verbose) MODE="qa"; WANT_LOCK=1; FORCE_LOG=1; shift ;;
        --bash)       MODE="bash"; shift ;;
        --shell)      MODE="shell"; shift ;;
        *) break ;;
    esac
done
NAME="${1:-task}"; shift || true

# ---- 执行开关 ---------------------------------------------------------------
# 两个独立开关，避免「--qa 必然静默」把 QA 的手工排障路径也堵死：
#  默认        → 写日志（低频任务，日志是排障唯一线索，必须留）
#  --qa        → **默认静默**（高频 720 次/天，逐次写日志会淹没一切）
#  QA_LOG=1/0  → 显式覆盖（0=静默，1=写日志）
#  _RUN_TASK_LOG=1 → 强制写日志（手工排障 / 首次验证用，优先级最高）
# ⚠️ 为什么 QA 必须静默：群问答 `*/2`（720 次/天）若每次都往
#    _host_task.log 追加「开始/退出码」两行，一天 1440 行，
#    日志以每月数万行膨胀（logrotate 能压缩，但不能阻止写放大）。
if [ "${QA_LOG:-}" != "" ]; then
    SILENT="$QA_LOG"
elif [ "$MODE" = "qa" ]; then
    SILENT=0
else
    SILENT=1
fi
[ "$FORCE_LOG" = "1" ] && SILENT=1          # --qa-verbose
[ "${_RUN_TASK_LOG:-0}" = "1" ] && SILENT=1  # 环境变量覆盖（手工排障）
if [ "$SILENT" = "0" ]; then
    LOG="/dev/null"
    ERRLOG="$DEPLOY_DIR/data/_task_errors.log"
else
    LOG="$DEPLOY_DIR/data/_host_task.log"
    ERRLOG=""
fi

# ---- 交易日守卫（仅 --trading 时生效；非交易日静默跳过）---------------------
if [ "$TRADING_ONLY" = "1" ] && ! _is_trading_day; then
    # ⚠️ 这里**不写日志** —— 否则节假日里 `*/5` 任务会以每 5 分钟一条的速度
    #    刷屏（288 条/天 × 若干任务），把真正的故障信息淹掉。
    exit 0
fi

# ---- 单实例锁（高频任务必需）------------------------------------------------
# ⚠️ 为什么：群问答单次最长可达数分钟（实测「厦门钨业能买吗」端到端 167s），
#    而 cron 每 2 分钟就再触发一次。若上一轮未结束就起下一轮：
#      1) 两个进程同时消费同一队列 → 用户被**重复回复**
#      2) 并发调用 LLM → 触发限流 429
#    抢不到锁直接退出（下一轮再来），不排队、不等待。
if [ "$WANT_LOCK" = "1" ]; then
    exec 9>"$DEPLOY_DIR/data/_task_runner.lock"
    flock -n 9 || exit 0
fi

# ---- 执行 -------------------------------------------------------------------
TMPOUT="$(mktemp)"
rc=0

case "$MODE" in
    qa)
        # 群问答链路：必须成对，缺任一半整条链路都不工作
        #   （曾漏配拉取端 → 队列恒空 → 用户 @机器人 永远无人回复）
        #
        # 三步顺序（2026-09-20 调整）：
        #   ① 先「溜一遍队列」—— 处理上轮遗留
        #      为什么放在拉取**之前**：以前单次最长 167s，若上一轮没跑完就
        #      在队列里留了活，先拉取只会让它排队等更久。先消费掉积压，
        #      把「提问 → 看到回复」的延迟压到最小。
        #   ② 拉取荔枝群 @机器人 消息入队
        #   ③ 立刻再处理一次 —— 本轮新入队的提问在**同一轮**内就被回复，
        #      不用再等 2 分钟（这是延迟从「分钟级」到「秒级」的关键）
        rc1=0; rc2=0; rc3=0
        {
            echo "--- $(date '+%F %T') [$NAME] 开始 ---"
            "$PY" scripts/qa_analyzer.py
            rc1=$?
            "$PY" scripts/sync_litchi_auto.py --group litchi
            rc2=$?
            # 复盘群由 trade365 bot 统一轮询（同一飞书应用、同一群）
            # → 两边各自轮询会导致用户 @ 一次被回两条，故此处不再拉取。
            "$PY" scripts/qa_analyzer.py
            rc3=$?
            echo "--- $(date '+%F %T') [$NAME] 溜队列=$rc1 拉取=$rc2 处理=$rc3 ---"
        } > "$TMPOUT" 2>&1
        # 非静默时也要落盘（默认路径 TMPOUT 是临时文件，会被删掉）——
        # 否则 --qa-verbose / _RUN_TASK_LOG=1 打开后却看不到任何输出，
        # 手工排障会以为「任务没跑」，反而误判。
        [ "$SILENT" = "1" ] && cat "$TMPOUT" >> "$LOG"
        [ "$rc1" -eq 0 ] && [ "$rc2" -eq 0 ] && [ "$rc3" -eq 0 ] || rc=1
        ;;
    shell)
        {
            echo "--- $(date '+%F %T') [$NAME] 开始 ---"
            bash -c "$*"
            rc=$?
            echo "--- $(date '+%F %T') [$NAME] 退出码 $rc ---"
        } >> "$LOG" 2>&1
        ;;
    bash)
        {
            echo "--- $(date '+%F %T') [$NAME] 开始 ---"
            bash "$@"
            rc=$?
            echo "--- $(date '+%F %T') [$NAME] 退出码 $rc ---"
        } >> "$LOG" 2>&1
        ;;
    *)
        {
            echo "--- $(date '+%F %T') [$NAME] 开始 ---"
            "$PY" "$@"
            rc=$?
            echo "--- $(date '+%F %T') [$NAME] 退出码 $rc ---"
        } >> "$LOG" 2>&1
        ;;
esac

# ---- 静默模式的错误落盘 -----------------------------------------------------
if [ "$SILENT" = "0" ] && [ "$rc" != "0" ] && [ -n "$ERRLOG" ]; then
    {
        echo "=== $(date '+%F %T') [$NAME] 退出码 $rc ==="
        cat "$TMPOUT" 2>/dev/null
    } >> "$ERRLOG"
fi
rm -f "$TMPOUT"

exit "$rc"

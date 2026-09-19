#!/usr/bin/env bash
# ============================================================================
# 群问答 24×7 监控安装：荔枝群 + 每日复盘群，@机器人 提问即时回复
#
# 为什么需要独立脚本（而非塞进 setup_host_tasks.sh）
# ---------------------------------------------------
# 群问答是一条**两段式链路**，必须成对：
#     sync_litchi_auto.py --group <群>   拉取 @机器人 消息 → 入队
#     qa_analyzer.py                     取队列 → Kimi 分析 → 发回群
# 缺任一半整条链路都不工作（曾漏配拉取端，导致队列恒空、提问无人应答）。
#
# 本脚本把这两段**串在同一条 cron 任务里**（用 bash -c），好处：
#   1. 拉取后立即处理，回复延迟从「分钟级」降到「秒级」
#   2. 只占 1 条 cron，避免多任务间的标记/顺序问题
#   3. 顺序确定：先入队再消费
#
# 频率：每 2 分钟（24×7）。为什么不是 1 分钟：
#   每次拉取都会真实调用飞书 API（用户身份），1 分钟 = 每天 1440 次调用，
#   有触发飞书侧限流（QPS/日配额）的风险；2 分钟 = 每天 720 次，
#   在"及时回复"与"稳定运行"之间取平衡。若需更快可传 --every 1。
#
# 静默日志：包装脚本自带日志开关（QA_LOG=0 时完全不写日志文件），
#   避免 *_run_task.sh 每次运行追加 2 行 → 高频轮询下日志爆炸。
#
# 用法：
#   sudo bash scripts/deploy/setup_qa_24x7.sh              # 每 2 分钟
#   sudo bash scripts/deploy/setup_qa_24x7.sh --every 1    # 每 1 分钟（更快）
#   sudo bash scripts/deploy/setup_qa_24x7.sh --uninstall  # 移除
# ============================================================================
set -uo pipefail

DEPLOY_DIR="/opt/kol-skills-platform"
NODE_BIN="/opt/node20/bin"
VENV="$DEPLOY_DIR/.venv-host"
MARK="# kol-platform-qa"
RUNNER="$DEPLOY_DIR/scripts/deploy/_run_qa.sh"
EVERY=2

while [ $# -gt 0 ]; do
    case "$1" in
        --every) EVERY="${2:-2}"; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        *) shift ;;
    esac
done

if [ "${UNINSTALL:-0}" = "1" ]; then
    crontab -l 2>/dev/null | grep -v "$MARK" | crontab - || true
    echo "  ✅ 已移除群问答 24×7 任务"
    crontab -l 2>/dev/null | grep -v "^#" | sed 's/^/     /' || echo "     (crontab 为空)"
    exit 0
fi

[ -x "$VENV/bin/python" ] || { echo "  ❌ 未找到 $VENV，请先运行 setup_host_sync.sh"; exit 1; }

echo "=== 1. 生成 24×7 群问答运行器 ==="
cat > "$RUNNER" <<'EOF'
#!/usr/bin/env bash
# 由 setup_qa_24x7.sh 生成；群问答专用运行器（24×7，无交易日/时段守卫）
#
# 与 _run_task.sh 的区别：
#   1. 不写逐次日志（QA_LOG=0）—— 高频轮询下日志会爆炸
#   2. 只在出错时记录（RC != 0）
#   3. 串联「拉取两群 → 处理队列」，一次完成
set -u
DEPLOY_DIR="/opt/kol-skills-platform"
export PATH="/opt/node20/bin:/usr/local/bin:/usr/bin:/bin"
export TZ=Asia/Shanghai
cd "$DEPLOY_DIR" || exit 1

# ---- 单实例锁（CRITICAL）----------------------------------------------------
# 为什么需要：cron 每 2 分钟触发一次，而开启多轮追加取数后，单次问答最长可达
#   数分钟（实测「厦门钨业能买吗」端到端 167s）。若上一轮尚未结束就再起一轮：
#     1) 两个进程同时消费同一队列 → 可能重复回复
#     2) 并发调用 Kimi → 账号是组织级 3 RPM，会直接触发 429
#   故用 flock 保证同一时刻只有一个实例；抢不到锁直接退出（下一轮再来）。
LOCKFILE="$DEPLOY_DIR/data/_qa_run.lock"
exec 9>"$LOCKFILE"
if ! flock -n 9; then
    exit 0
fi

# 加载配置（剥离 CRLF —— Windows 编辑过的配置会带 \r，
#   会导致 source 报 `$'\r': command not found` 且变量尾部多出 \r）
_load_env() {
    local f="$1"; [ -f "$f" ] || return 0
    local tmp; tmp="$(mktemp)"
    tr -d '\r' < "$f" > "$tmp"
    set -a; . "$tmp"; set +a; rm -f "$tmp"
}
_load_env ./.env
_load_env ./data/local_config.env

PY="$DEPLOY_DIR/.venv-host/bin/python"
ERRLOG="$DEPLOY_DIR/data/_qa_errors.log"

# ---- ① 拉取荔枝群 @机器人 消息入队 ----
"$PY" scripts/sync_litchi_auto.py --group litchi  >/dev/null 2>&1
rc1=$?
# ---- ② 拉取每日复盘群 @机器人 消息入队 ----
# 复盘群改由 trade365 bot 统一轮询（「合并为一套处理」）：
#   trade365 与 kol 问答是同一个飞书应用、同一个群，两边各自轮询会导致
#   用户 @ 一次被回两条。现由 trade365 bot 作为该群唯一轮询器，
#   非交易命令再桥接回 kol 问答（scripts/qa_oneshot.py）。
# "$PY" scripts/sync_litchi_auto.py --group review  >/dev/null 2>&1
rc2=$?
# ---- ③ 处理队列（Kimi 分析 → 回复到群）----
"$PY" scripts/qa_analyzer.py >/dev/null 2>&1
rc3=$?

# 只在异常时记日志（正常静默，避免日志膨胀）
if [ $rc1 -ne 0 ] || [ $rc2 -ne 0 ] || [ $rc3 -ne 0 ]; then
    echo "$(date '+%F %T') 拉取荔枝=$rc1 拉取复盘=$rc2 处理=$rc3" >> "$ERRLOG"
fi
exit 0
EOF
chmod +x "$RUNNER"
echo "  ✅ $RUNNER"

echo ""
echo "=== 2. 注册 cron（每 $EVERY 分钟，24×7，无交易日限制）==="
TMP="$(mktemp)"
crontab -l 2>/dev/null | grep -v "$MARK" > "$TMP" || true
printf '*/%s * * * * %s >/dev/null 2>&1  %s\n' "$EVERY" "$RUNNER" "$MARK" >> "$TMP"
crontab "$TMP"
rm -f "$TMP"

echo "  已注册："
crontab -l 2>/dev/null | grep "$MARK" | sed 's/^/     /'

echo ""
echo "=== 3. 立即执行一次（验证链路）==="
bash "$RUNNER"
echo "  退出码: $?  （若上一步无输出即为正常）"
if [ -f "$DEPLOY_DIR/data/_qa_errors.log" ]; then
    echo "  最近错误日志："
    tail -3 "$DEPLOY_DIR/data/_qa_errors.log" | sed 's/^/     /'
else
    echo "  ✅ 无错误日志（本轮全部正常）"
fi

echo ""
echo "=== 完成 ==="
echo "  监控范围：荔枝种植交流群 + 每日复盘群"
echo "  响应延迟：≤ $EVERY 分钟（拉取与回复在同一轮完成）"
echo "  启动时段：24 小时（含周末、节假日、凌晨）"
echo "  错误日志：$DEPLOY_DIR/data/_qa_errors.log（仅异常时写入）"
echo "  调整频率：bash $0 --every 1     # 改为每 1 分钟"
echo "  卸载    ：bash $0 --uninstall"

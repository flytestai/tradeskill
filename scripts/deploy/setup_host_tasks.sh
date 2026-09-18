#!/usr/bin/env bash
# ============================================================================
# 宿主机定时任务安装（替代 Windows 的 supervisor）
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
# 交易日判断由脚本内部的守卫完成（非交易日自动跳过），cron 只需按工作日触发。
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
MARK="# kol-host-task"

if [ "${1:-}" = "--uninstall" ]; then
    crontab -l 2>/dev/null | grep -v "$MARK" | crontab - || true
    echo "  ✅ 已移除全部宿主机定时任务"
    crontab -l 2>/dev/null | grep -v "^#" | sed 's/^/     /' || echo "     (crontab 为空)"
    exit 0
fi

[ -x "$VENV/bin/python" ] || { echo "  ❌ 未找到 $VENV，请先运行 setup_host_sync.sh"; exit 1; }

echo "=== 1. 生成任务运行器（统一加载 env + PATH）==="
RUNNER="$DEPLOY_DIR/scripts/deploy/_run_task.sh"
cat > "$RUNNER" <<'EOF'
#!/usr/bin/env bash
# 由 setup_host_tasks.sh 生成；宿主机定时任务统一入口
# 用法: _run_task.sh <任务名> <脚本> [参数...]
set -u
DEPLOY_DIR="/opt/kol-skills-platform"
export PATH="/opt/node20/bin:/usr/local/bin:/usr/bin:/bin"
export TZ=Asia/Shanghai
cd "$DEPLOY_DIR" || exit 1

# 加载配置（.env 优先，回退 data/local_config.env）
if [ -f .env ]; then set -a; . ./.env; set +a; fi
if [ -f data/local_config.env ]; then set -a; . ./data/local_config.env; set +a; fi

NAME="$1"; shift
LOG="$DEPLOY_DIR/data/_host_task.log"
echo "--- $(date '+%F %T') [$NAME] 开始 ---" >> "$LOG"
"$DEPLOY_DIR/.venv-host/bin/python" "$@" >> "$LOG" 2>&1
rc=$?
echo "--- $(date '+%F %T') [$NAME] 退出码 $rc ---" >> "$LOG"
exit $rc
EOF
chmod +x "$RUNNER"
echo "  ✅ $RUNNER"

echo ""
echo "=== 2. 注册定时任务 ==="
TMP="$(mktemp)"
crontab -l 2>/dev/null | grep -v "$MARK" > "$TMP" || true

emit() {  # emit <cron 时间字段> <任务名> <脚本> [参数...]
    local when="$1"; shift
    local name="$1"; shift
    printf '%s %s %s %s >/dev/null 2>&1  %s\n' "$when" "$RUNNER" "$name" "$*" "$MARK" >> "$TMP"
}

# ---- 每日定点（交易日守卫在脚本内）----
emit "45 8  * * 1-5"  "盘前播报"   "scripts/market_summary.py"  "--premarket"
emit "0  11 * * 1-5"  "盘中播报"   "scripts/market_summary.py"  "--intraday"
emit "55 14 * * 1-5"  "盘后兜底同步" "scripts/sync_feishu_auto.py" "--force"
emit "0  16 * * 1-5"  "收盘兜底同步" "scripts/sync_feishu_auto.py" "--force"
emit "5  15 * * 1-5"  "收盘汇总"   "scripts/market_summary.py"  ""

# ---- 周期性（盘中时段，脚本内部有交易日守卫）----
emit "*/5  9-15 * * 1-5" "持仓监控"   "scripts/position_monitor.py" "--notify"
emit "*/10 9-15 * * 1-5" "关键位监控" "scripts/monitor_alerts.py"
emit "*/5  *    * * *"   "敲键盘清理" "scripts/react.py" "cleanup"

# ---- 授权保活（每 6 小时；脚本内部按「距上次刷新≥20h」决定是否真刷）----
emit "0 */6 * * *"       "授权保活"   "scripts/auth_keepalive.py"

crontab "$TMP"
rm -f "$TMP"

echo "  已注册任务："
crontab -l 2>/dev/null | grep "$MARK" | sed 's/  # kol-host-task//' | sed 's/^/     /'

echo ""
echo "=== 3. 首次验证（跑一次持仓监控，确认链路）==="
bash "$RUNNER" "验证-持仓监控" "scripts/position_monitor.py" "--notify" || true
echo "  最近日志："
tail -8 "$LOG_DIR/_host_task.log" 2>/dev/null | sed 's/^/     /'

echo ""
echo "=== 完成 ==="
echo "  统一日志：$LOG_DIR/_host_task.log"
echo "  手动触发：bash $RUNNER <名称> <脚本> [参数]"
echo "  卸载    ：bash $0 --uninstall"

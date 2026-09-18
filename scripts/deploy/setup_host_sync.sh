#!/usr/bin/env bash
# ============================================================================
# 宿主机同步服务安装（飞书群消息 → 言论库）
#
# 为什么这部分跑在宿主机而非容器
# ------------------------------
# 读群消息需要**用户身份**（lark-cli + OAuth），而 lark-cli 依赖 Node；
# 实测该服务器的容器**无法创建线程**（宿主内核 + Docker 18.09 限制），
# Node 启动即崩（uv_thread_create 断言失败）→ 容器内无法用 lark-cli。
#
# 因此采用分工：
#   宿主机（Python 3.11 via uv + lark-cli）→ 拉群消息、写 JSONL
#   容器（纯 Python，无线程依赖）        → 提供服务、发送消息
#   两者通过 **数据卷共享文件** 交换数据（容器内 /app/data 即卷）
#
# 注意：不修改系统 Python（3.7），用 uv 装的独立 3.11。
#
# 用法：
#   sudo bash scripts/deploy/setup_host_sync.sh
#   sudo bash scripts/deploy/setup_host_sync.sh --uninstall
# ============================================================================
set -euo pipefail

DEPLOY_DIR="/opt/kol-skills-platform"
UV_BIN="${UV_BIN:-/opt/uv/uv}"   # uv 安装器把它放在 /opt/uv/uv（非 bin/ 下）
NODE_BIN="/opt/node20/bin"
SYNC_HOURS="${SYNC_HOURS:-2}"          # 交易时段内每 N 小时同步一次
LOG="$DEPLOY_DIR/data/_host_sync.log"

if [ "${1:-}" = "--uninstall" ]; then
    crontab -l 2>/dev/null | grep -v "kol-platform-sync" | crontab - || true
    echo "  ✅ 已移除同步 cron"
    exit 0
fi

echo "=== 1. 准备独立的 Python 3.11 环境 ==="
PY311="$($UV_BIN python find 3.11 2>/dev/null || true)"
if [ -z "$PY311" ] || [ ! -x "$PY311" ]; then
    echo "  安装 Python 3.11..."
    "$UV_BIN" python install 3.11
    PY311="$($UV_BIN python find 3.11)"
fi
echo "  ✅ $PY311"
"$PY311" -V | sed 's/^/     /'

echo ""
echo "=== 2. 创建宿主机专用 venv（装同步所需依赖）==="
VENV="$DEPLOY_DIR/.venv-host"
if [ ! -x "$VENV/bin/python" ]; then
    "$UV_BIN" venv --python "$PY311" "$VENV" 2>&1 | tail -2
fi
"$VENV/bin/python" -V | sed 's/^/     /'
# 同步脚本仅用标准库；如后续需要可在此装依赖
"$UV_BIN" pip install --python "$VENV/bin/python" --quiet requests 2>/dev/null || true

echo ""
echo "=== 3. 生成同步入口脚本 ==="
WRAP="$DEPLOY_DIR/scripts/deploy/_run_host_sync.sh"
mkdir -p "$(dirname "$WRAP")"
cat > "$WRAP" <<EOF
#!/usr/bin/env bash
# 由 setup_host_sync.sh 生成；宿主机同步入口
set -u
export PATH="$NODE_BIN:/usr/local/bin:/usr/bin:/bin"
export TZ=Asia/Shanghai
cd "$DEPLOY_DIR"

# 加载 .env（含 chat_id / 飞书凭据）
if [ -f .env ]; then set -a; . ./.env; set +a; fi

echo "--- \$(date '+%F %T') 开始同步 ---" >> "$LOG"
"$VENV/bin/python" scripts/sync_feishu_auto.py --force >> "$LOG" 2>&1
rc=\$?
echo "--- 退出码 \$rc ---" >> "$LOG"

# 同步完成后，把最新数据推给容器使用（容器直接读同一数据卷，无需拷贝）
exit \$rc
EOF
chmod +x "$WRAP"
echo "  ✅ $WRAP"

echo ""
echo "=== 4. 注册 cron（每 $SYNC_HOURS 小时；仅交易日盘中时段）==="
# 盘中 9-15 点，每 N 小时跑一次；cron 无法直接判断交易日，
# 由 sync_feishu_auto.py 内部的交易日守卫兜底（非交易日会自行跳过）。
CRON_LINE="0 9-15/$SYNC_HOURS * * 1-5 $WRAP >/dev/null 2>&1  # kol-platform-sync"
( crontab -l 2>/dev/null | grep -v "kol-platform-sync" ; echo "$CRON_LINE" ) | crontab -
echo "  ✅ cron 已注册："
crontab -l 2>/dev/null | grep "kol-platform-sync" | sed 's/^/     /'

echo ""
echo "=== 5. 首次执行（验证）==="
bash "$WRAP" || true
echo "  最近日志："
tail -12 "$LOG" 2>/dev/null | sed 's/^/     /'

echo ""
echo "=== 完成 ==="
echo "  日志：$LOG"
echo "  手动触发：bash $WRAP"
echo "  卸载：crontab -l | grep -v kol-platform-sync | crontab -"

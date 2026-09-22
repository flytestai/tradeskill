#!/usr/bin/env bash
# kol-platform 自动部署脚本（CD，pull-based）
#
# 触发方式：cron 每 5 分钟一次；也支持手动执行（直接跑本脚本）。
# 流程：
#   git fetch -> 有新提交则 --ff-only 拉取 -> 按改动重启对应 systemd 服务
#   -> 健康检查 -> 失败自动回滚 -> 飞书通知（best-effort）。
#
# 安全约定：
#   - 只用 --ff-only，绝不覆盖本地未提交改动（冲突则跳过部署）。
#   - 不触碰 .env / data/local_config.env 等 gitignored 机密（git pull 天然不会覆盖它们）。
#   - 回滚用 git reset --hard 到部署前 commit（仅回滚代码，不含 gitignored 运行时状态）。
set -uo pipefail

REPO="/opt/kol-skills-platform"
cd "$REPO" || exit 1
mkdir -p "$REPO/logs"
LOG="$REPO/logs/deploy.log"

# 并发锁：避免多个 cron 实例同时跑
exec 200>"/tmp/kol-deploy.lock"
flock -n 200 || { echo "[$(date '+%F %T')] deploy already running, skip"; exit 0; }

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# ---- 飞书通知（best-effort；凭据放在 gitignored 的 data/_deploy_notify.env）----
notify() {
  local text="$1"
  local envf="$REPO/data/_deploy_notify.env"
  [ -f "$envf" ] || return 0
  # shellcheck disable=SC1090
  source "$envf"
  [ -n "${FEISHU_APP_ID:-}" ] && [ -n "${FEISHU_APP_SECRET:-}" ] && [ -n "${FEISHU_OPEN_ID:-}" ] || return 0
  local token
  token=$(curl -s -X POST 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal' \
    -H 'Content-Type: application/json' \
    -d "{\"app_id\":\"${FEISHU_APP_ID}\",\"app_secret\":\"${FEISHU_APP_SECRET}\"}" | jq -r '.tenant_access_token // empty')
  [ -n "$token" ] || return 0
  curl -s -X POST 'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id' \
    -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
    -d "{\"receive_id\":\"${FEISHU_OPEN_ID}\",\"msg_type\":\"text\",\"content\":$(jq -n --arg t "$text" '{text:$t}' | jq -c .)}" >/dev/null
}

OLD=$(git rev-parse HEAD 2>/dev/null) || { log "not a git repo, exit"; exit 1; }
git fetch origin main -q 2>>"$LOG" || { log "git fetch failed"; exit 1; }
NEW=$(git rev-parse origin/main)

if [ "$OLD" = "$NEW" ]; then
  # 无新提交：静默退出（避免每 5 分钟刷日志）
  exit 0
fi

log "detected new commits: ${OLD:0:8} -> ${NEW:0:8}"
CHANGED=$(git diff --name-only "$OLD" "$NEW")

if ! git merge --ff-only origin/main >>"$LOG" 2>&1; then
  log "PULL_FAILED (local uncommitted changes conflict), skip deploy"
  notify "⚠️ kol-platform 自动部署跳过：本地有未提交改动与远端冲突，请手动处理"
  exit 0
fi

# ---- 判定需要重启的服务 ----
# 运行中的两个 systemd 服务都 import scripts/（api/rest_app.py 与 api.mcp_server），
# 所以 scripts/ 下除 deploy/ 外的改动都保守重启；scripts/deploy/ 只影响部署脚本本身，不重启。
APP_CHANGED=$(echo "$CHANGED" | grep -E '^scripts/' | grep -v '^scripts/deploy/' || true)
RESTART=0
[ -n "$APP_CHANGED" ] && RESTART=1

if [ -n "$CHANGED" ]; then
  log "changed files: $(echo "$CHANGED" | tr '\n' ' ')"
fi

# ---- 语法门禁（fail-fast）：只编译本次改动涉及的 .py，语法错直接回滚、不重启服务 ----
PYBIN="$REPO/.venv-host/bin/python"
[ -x "$PYBIN" ] || PYBIN="$(command -v python3)"
PY_CHANGED=$(echo "$CHANGED" | grep -E '\.py$' || true)
if [ -n "$PY_CHANGED" ]; then
  COMPILE_FAIL=0
  for f in $PY_CHANGED; do
    [ -f "$f" ] || continue  # 已删除/改名的文件跳过，避免误报
    if ! "$PYBIN" -m py_compile "$f" >>"$LOG" 2>&1; then
      log "COMPILE_FAILED: $f"
      COMPILE_FAIL=1
    fi
  done
  if [ "$COMPILE_FAIL" -eq 1 ]; then
    log "syntax gate failed, rolling back to ${OLD:0:8} (no service restart)"
    git reset --hard "$OLD"
    notify "❌ kol-platform 自动部署失败：语法检查未通过，已回滚到 ${OLD:0:8}"
    exit 1
  fi
fi

FAIL=0
if [ "$RESTART" -eq 1 ]; then
  log "restarting kol-platform.service + kol-platform-mcp.service"
  sudo /usr/bin/systemctl restart kol-platform.service || FAIL=1
  sudo /usr/bin/systemctl restart kol-platform-mcp.service || FAIL=1
else
  log "no service-affecting change, skip restart"
fi

sleep 3
if ! systemctl is-active --quiet kol-platform.service; then FAIL=1; log "kol-platform.service NOT active"; fi
if ! systemctl is-active --quiet kol-platform-mcp.service; then FAIL=1; log "kol-platform-mcp.service NOT active"; fi

# 增强健康检查：仅在真正重启过服务时，额外探活 REST /healthz（进程活着 ≠ 接口能响应）
if [ "$RESTART" -eq 1 ]; then
  HZ_URL="${PLATFORM_HEALTH_URL:-http://127.0.0.1:8020/healthz}"
  if ! curl -sf --max-time 5 "$HZ_URL" >/dev/null 2>&1; then
    FAIL=1
    log "REST /healthz 探活失败: $HZ_URL"
  else
    log "REST /healthz 探活通过"
  fi
fi

if [ "$FAIL" -ne 0 ]; then
  log "DEPLOY_FAILED, rolling back to ${OLD:0:8}"
  git reset --hard "$OLD"
  sudo /usr/bin/systemctl restart kol-platform.service || true
  sudo /usr/bin/systemctl restart kol-platform-mcp.service || true
  notify "❌ kol-platform 自动部署失败，已回滚到 ${OLD:0:8}，请查看 logs/deploy.log"
  exit 1
fi

log "DEPLOY_OK -> ${NEW:0:8}"
notify "✅ kol-platform 已自动部署新版本 ${NEW:0:8}"

#!/usr/bin/env bash
# ============================================================================
# 言论库跨机备份：把 sync/ 推送到 GitHub
#
# 为什么需要
#   平台设计上把 `sync/records.jsonl` 作为**跨机共享的言论库**
#   （服务器与 Windows 各自独立 SQLite，通过这个文件互通）。
#   但实测服务器**没有 git 凭据**（无 credential.helper、无 ~/.git-credentials），
#   push 必然失败，且这条链路从未被调度 —— 等于备份不存在。
#
# 与 `sync.py push` 的区别（为什么不直接用 push）
#   `cmd_push()` 会先 `git pull` 再 push。而服务器上的工作区有大量
#   **未跟踪的部署特有文件**（vendor/、data 链接等），一旦 pull 引发冲突
#   或覆盖，可能影响正在运行的服务。
#   本脚本只做「导出 → 提交 sync/ → 推送」，**不做 pull**，风险最小。
#
# 前置条件
#   · Deploy Key 已生成（~/.ssh/github_deploy）
#   · 公钥已加到 GitHub 仓库的 Deploy Keys，且**勾选 write access**
#   · ~/.ssh/config 里有 Host github-kol 指向该密钥
#   · remote 已切到 git@github-kol:flytestai/tradeskill.git
#
# 用法：
#   bash push_sync.sh              # 导出 + 推送
#   bash push_sync.sh --check      # 只检查前置条件（不推送）
# ============================================================================
set -uo pipefail

KOL_DIR="/opt/kol-skills-platform"
PY="$KOL_DIR/.venv-host/bin/python"
GIT_SSH_HOST="github-kol"
REMOTE="git@${GIT_SSH_HOST}:flytestai/tradeskill.git"

cd "$KOL_DIR" || exit 1

# ---- 前置检查 ---------------------------------------------------------------
check_only=0
[ "${1:-}" = "--check" ] && check_only=1

fail=0
if [ ! -f ~/.ssh/github_deploy ]; then
    echo "  ❌ 缺少 Deploy Key: ~/.ssh/github_deploy"; fail=1
fi
if ! grep -q "Host $GIT_SSH_HOST" ~/.ssh/config 2>/dev/null; then
    echo "  ❌ ~/.ssh/config 缺少 Host $GIT_SSH_HOST"; fail=1
fi
# ⚠️ 两个坑（实测）：
#   1. GitHub 对 deploy key 的响应是
#        "Hi <owner>/<repo>! You've successfully authenticated, but GitHub
#         does not provide shell access."
#      **不含** "successfully authenticated"（多了 You've）→ 匹配要宽松。
#   2. 脚本开头有 `set -o pipefail`，而 ssh 因「拒绝 shell 访问」**返回 1**
#      → `ssh ... | grep -q ...` 整条管道会被判为失败，
#      即使 grep 成功匹配也返回非零。故**必须先把输出存变量**再判断。
_ssh_out="$(timeout 25 ssh -T -o BatchMode=yes -o StrictHostKeyChecking=no "git@$GIT_SSH_HOST" 2>&1 || true)"
if ! echo "$_ssh_out" | grep -qE "Hi |successfully authenticated"; then
    echo "  ❌ SSH 认证失败（公钥是否已加到 GitHub 且勾选 write access？）"
    echo "$_ssh_out" | head -2 | sed 's/^/     /'
    fail=1
fi

if [ "$fail" != "0" ]; then
    echo "  → 前置条件未满足，跳过推送"
    exit 1
fi

if [ "$check_only" = "1" ]; then
    echo "  ✅ 前置条件全部满足（Deploy Key / ssh config / 认证）"
    exit 0
fi

# ---- 确保 remote 用 SSH ------------------------------------------------------
cur="$(git remote get-url origin 2>/dev/null || echo '')"
if [ "$cur" != "$REMOTE" ]; then
    git remote set-url origin "$REMOTE" && echo "  → remote 已切换为 $REMOTE"
fi

# ---- 导出（只追加新记录，不重写历史）----------------------------------------
"$PY" scripts/sync.py export >/dev/null 2>&1 || {
    echo "  ⚠️ export 失败，继续尝试推送已有内容"
}

# ---- 提交 + 推送 ------------------------------------------------------------
git add sync/ 2>/dev/null
ts="$(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M')"
if ! git diff --cached --quiet 2>/dev/null; then
    git -c user.name="kol-platform-bot" -c user.email="bot@localhost" \
        commit -m "sync: $ts" -- sync/ >/dev/null 2>&1
fi

if timeout 120 git push origin HEAD 2>&1 | tail -2 | sed 's/^/  /'; then
    n="$(wc -l < sync/records.jsonl 2>/dev/null || echo 0)"
    echo "[$(date '+%F %T')] 推送完成（records.jsonl $n 行）"
    exit 0
else
    echo "  ❌ 推送失败"
    exit 1
fi

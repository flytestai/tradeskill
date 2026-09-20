#!/usr/bin/env bash
# ============================================================================
# Nginx 站点配置部署 —— 带**可靠的**校验与回滚
#
# ⚠️ 本脚本存在的理由（一次真实事故，2026-09-20）
# ----------------------------------------------------------------------------
# 我此前用「临时拼的 ssh 命令」部署 nginx 配置，写法是：
#
#     set -e
#     sudo install -m 644 new.conf /etc/nginx/sites-available/skill-platform
#     if sudo nginx -t 2>&1 | tail -2; then      # ← 致命：管道
#         sudo systemctl reload nginx
#     else
#         sudo cp -a "$BK" ...                   # ← 回滚分支
#     fi
#
# 两个缺陷叠加，导致**该回滚时没回滚**：
#   ① `nginx -t | tail` 的退出码是 **tail 的**（永远 0），不是 nginx 的
#      → `if` 判成"成功"，走进了 reload 分支
#   ② reload 于是失败 → `set -e` **立刻终止整个脚本**
#      → 连 `else` 里的回滚都来不及执行
# 结果：磁盘上的 nginx 配置停在**坏状态**，只因 nginx 进程还持有内存里的旧配置
# 才没立刻断服 —— 但**任何一次重启都会让站点彻底挂掉**。
#
# 本脚本把这些坑一次性修掉：
#   · 校验命令**不经管道**，退出码真实可靠
#   · 用 `nginx -t` 的退出码作为唯一判据，失败**必回滚**
#   · 不依赖 `set -e`（它的提前退出正是上次的帮凶），显式判断每一步
#   · 每一步都打印实际状态码，失败时明确告知回滚结果
#
# 用法（在服务器上执行）：
#   sudo bash scripts/deploy/deploy_nginx.sh scripts/deploy/nginx-skill.conf
#   sudo bash scripts/deploy/deploy_nginx.sh <候选配置> --dry-run
# ============================================================================
set -uo pipefail

CONF_SRC="${1:-}"
BACKUP_DIR="/opt/kol-backups"
TARGET="/etc/nginx/sites-available/skill-platform"
DRY=0
[ "${2:-}" = "--dry-run" ] && DRY=1

if [ -z "$CONF_SRC" ] || [ ! -f "$CONF_SRC" ]; then
    echo "用法: sudo bash $0 <候选配置> [--dry-run]" >&2
    exit 2
fi

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
die() { printf '\033[31m❌ %s\033[0m\n' "$*"; exit 1; }

say "0. 前置检查"
[ -f "$TARGET" ] || die "目标配置不存在：$TARGET（首次部署请手工放置）"
echo "  候选: $CONF_SRC"
echo "  目标: $TARGET"

# CRLF 检查：nginx 配置带 CR 同样会解析失败（与 logrotate 同类问题）
if grep -q $'\r' "$CONF_SRC" 2>/dev/null; then
    die "候选配置含 CR（CRLF 行尾）—— nginx 会解析失败，请先转 LF"
fi
echo "  ✅ 候选配置为 LF 行尾"

say "1. 备份当前配置"
BK="$BACKUP_DIR/nginx-$(date +%Y%m%d-%H%M%S).conf"
cp -a "$TARGET" "$BK" || die "备份失败（拒绝无备份地改配置）"
echo "  ✅ $BK"

if [ "$DRY" = "1" ]; then
    say "dry-run：只校验候选，不落盘"
    STAGE="/tmp/_candidate_$$.conf"
    cp "$CONF_SRC" "$STAGE"
    cp "$STAGE" "$TARGET"
    # ⚠️ 不用管道：管道会把退出码换成最后一个命令的
    nginx -t > /tmp/_ngt_$$.out 2>&1
    rc=$?
    cat /tmp/_ngt_$$.out | tail -3
    cp -a "$BK" "$TARGET"
    rm -f "$STAGE" /tmp/_ngt_$$.out
    if [ "$rc" -eq 0 ]; then
        echo "  ✅ 候选配置语法通过（已还原目标文件）"
        exit 0
    else
        echo "  ❌ 候选配置语法失败（已还原目标文件）"
        exit 1
    fi
fi

say "2. 落盘候选配置"
install -m 644 "$CONF_SRC" "$TARGET" || {
    echo "  落盘失败 → 回滚"; cp -a "$BK" "$TARGET"; die "已回滚"
}

say "3. 语法校验"
# ⚠️ 关键：不经管道，直接取 nginx -t 的退出码
nginx -t > /tmp/_ngt_$$.out 2>&1
rc=$?
cat /tmp/_ngt_$$.out | tail -3
rm -f /tmp/_ngt_$$.out

if [ "$rc" -ne 0 ]; then
    echo "  ❌ 语法失败 → 回滚"
    cp -a "$BK" "$TARGET"
    if nginx -t > /dev/null 2>&1; then
        echo "  ✅ 已回滚，配置恢复可用"
    else
        echo "  🔴 回滚后仍失败 —— 需人工介入（备份在 $BK）"
    fi
    exit 1
fi
echo "  ✅ 语法通过"

say "4. 生效"
if systemctl reload nginx; then
    echo "  ✅ 已 reload"
else
    echo "  ⚠️ reload 失败 → 尝试 restart"
    if systemctl restart nginx; then
        echo "  ✅ 已 restart"
    else
        echo "  ❌ 生效失败 → 回滚配置并重启"
        cp -a "$BK" "$TARGET"
        systemctl restart nginx || true
        die "已回滚；nginx 状态：$(systemctl is-active nginx)"
    fi
fi

say "5. 验证（真实域名 / 未知域名 / 上游）"
_active=$(systemctl is-active nginx)
echo "  nginx: $_active"
[ "$_active" = "active" ] || die "nginx 未在运行"

_run() {  # _run <说明> <curl 参数...>
    local desc="$1"; shift
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' "$@" 2>/dev/null) || code="000"
    printf '  %-34s -> %s\n' "$desc" "$code"
    echo "$code"
}

for h in skill.flytest.com.cn www.flytest.com.cn ai.flytest.com.cn; do
    c=$(_run "$h/healthz" "https://$h/healthz" -k --resolve "$h:443:127.0.0.1")
    [ "$c" = "200" ] || echo "    ⚠️ 真实域名未返回 200，请立即检查"
done

# 未知域名应被 default_server 444 断开（curl 得到 000）
c=$(_run "未知域名（应 000/断开）" "https://nonexistent.example.com/healthz" -k --resolve "nonexistent.example.com:443:127.0.0.1")
[ "$c" = "000" ] && echo "    ✅ Host 白名单生效" || echo "    ⚠️ 未知域名未被拒（期望 000，实得 $c）"

say "完成"
echo "  备份：$BK"
echo "  回滚：sudo cp -a $BK $TARGET && sudo systemctl reload nginx"

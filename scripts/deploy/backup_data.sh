#!/usr/bin/env bash
# ============================================================================
# 关键数据快照备份
#
# 为什么需要（实测结论）
#   平台有一批**不可重建**的数据，此前**完全没有备份**：
#     · kol:      price_alerts.json（用户设的价位提醒）
#                 group_qa_answered.json（问答去重，丢了会重复回复）
#                 level_targets.json（关键位，可从波浪重算但会丢历史）
#                 kol_opinions.db（大V言论库，可重同步但耗时）
#     · trade365: meetings.json（420K，14 场会议推荐 — 量化结果）
#                 review.json（156K，8 次复盘 — 量化结果）
#                 watchlist / holdings / closed_trades（用户交易设置）
#
#   原设计依赖 `sync.py push` 推 GitHub，但实测**服务器没有 git 凭据**
#   （无 credential.helper、无 ~/.git-credentials）→ 推送必然失败。
#   故这里改用**本地快照**：不依赖任何外部凭据，立刻可用。
#
# 策略
#   · 每日快照到 /opt/kol-backups/YYYY-MM-DD/
#   · 保留最近 7 天（rolling）
#   · sqlite 用 .backup 命令（比直接 cp 安全，避免 WAL 中间态）
#   · 备份完校验（文件数 + 大小 + db 可打开）
#
# 用法：
#   bash backup_data.sh              # 常规备份
#   bash backup_data.sh --list       # 看已有快照
#   bash backup_data.sh --verify      # 校验最近快照
# ============================================================================
set -uo pipefail

KOL_DIR="/opt/kol-skills-platform"
T365_DIR="/opt/trade365"
BACKUP_ROOT="/opt/kol-backups"
KEEP_DAYS=7

PY="$KOL_DIR/.venv-host/bin/python"

case "${1:-}" in
    --list)
        echo "=== 已有快照（$BACKUP_ROOT）==="
        [ -d "$BACKUP_ROOT" ] || { echo "  (无)"; exit 0; }
        du -sh "$BACKUP_ROOT"/* 2>/dev/null | sed 's/^/  /'
        exit 0
        ;;
    --verify)
        LATEST="$(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sort | tail -1)"
        [ -n "$LATEST" ] || { echo "  ❌ 无快照可校验"; exit 1; }
        echo "=== 校验最近快照：$LATEST ==="
        n=$(find "$LATEST" -type f | wc -l)
        echo "  文件数: $n"
        du -sh "$LATEST" | sed 's/^/  总大小: /'
        # db 可打开？
        if [ -f "$LATEST/kol/kol_opinions.db" ]; then
            "$PY" -c "
import sqlite3,sys
try:
    c=sqlite3.connect('$LATEST/kol/kol_opinions.db')
    n=c.execute('select count(*) from kol_records').fetchone()[0]
    print('  db 可打开: %d 条记录 ✅' % n)
except Exception as e:
    print('  ❌ db 校验失败:', e); sys.exit(1)
" || exit 1
        fi
        # 关键 json 是否有效
        "$PY" - <<PYEOF
import json, os, glob
bad = []
for f in glob.glob("$LATEST/**/*.json", recursive=True):
    try:
        json.load(open(f, encoding="utf-8"))
    except Exception:
        bad.append(os.path.basename(f))
print("  JSON 校验: %s" % ("全部有效 ✅" if not bad else "❌ 损坏: " + ", ".join(bad)))
PYEOF
        exit 0
        ;;
esac

TS="$(date +%F)"
DEST="$BACKUP_ROOT/$TS"
mkdir -p "$DEST/kol" "$DEST/trade365"

# ⚠️ 权限收紧（2026-09-19 第十轮发现）
#   备份目录此前是 755（任何本地用户可遍历）。而里面包含：
#     · authorized_keys   （可推导出可登录账号）
#     · local_config.env  （飞书 chat_id / open_id）
#     · 新增：SSL 私钥 privkey.pem
#   这些都是敏感材料，目录必须不可被其他用户读取/遍历。
#   注意：cp -a 会保留源文件权限，故此处需**递归**收紧。
chmod 700 "$BACKUP_ROOT" 2>/dev/null || true
chmod 700 "$DEST" 2>/dev/null || true

fail=0

# ---- kol 关键数据 -----------------------------------------------------------
for f in price_alerts.json group_qa_answered.json level_targets.json \
         level_asof.txt group_qa_queue.json; do
    src="$KOL_DIR/data/$f"
    [ -f "$src" ] && cp -a "$src" "$DEST/kol/" || true
done
for f in local_config.env monitor.env; do
    src="$KOL_DIR/data/$f"
    [ -f "$src" ] && cp -a "$src" "$DEST/kol/" 2>/dev/null || true
done

# sqlite 用 .backup（避免 WAL 中间态；直接 cp 可能拿到不一致快照）
if [ -f "$KOL_DIR/data/kol_opinions.db" ]; then
    "$PY" -c "
import sqlite3
src=sqlite3.connect('$KOL_DIR/data/kol_opinions.db')
dst=sqlite3.connect('$DEST/kol/kol_opinions.db')
with dst:
    src.backup(dst)
dst.close(); src.close()
" 2>/dev/null || { echo "  [WARN] db 备份失败"; fail=1; }
fi

# ---- trade365 关键数据 ------------------------------------------------------
for f in meetings.json review.json watchlist.json holdings.json \
         closed_trades.json weights.json; do
    src="$T365_DIR/data/$f"
    [ -f "$src" ] && cp -a "$src" "$DEST/trade365/" || true
done

# ---- 系统级配置 -------------------------------------------------------------
#
# ⚠️ 为什么必须备份（2026-09-19 第七轮审计发现）
#   此前备份只覆盖"业务数据"，**不含任何系统配置**。而系统配置同样会丢、
#   且丢了自己很难重建：
#     · /etc/localtime + /etc/timezone  —— 本轮就改过它（原为 US/Eastern，
#       导致所有定时任务晚 12 小时）。若机器重装而没记下原设定，
#       时区错位问题会再次静默复发。
#     · root crontab  —— 15 条定时任务是整个平台的心跳，丢了两天内无人知。
#     · sshd 配置 + authorized_keys  —— SSH 加固后若丢失，可能再也登不进。
#     · Nginx 站点配置  —— 公网入口（含 MCP/REST 反代与鉴权协作）。
#   数据备份得以恢复，靠的正是这些配置还在。
DEST_SYS="$DEST/system"
mkdir -p "$DEST_SYS"
cp -a /etc/localtime "$DEST_SYS/localtime" 2>/dev/null || true
cp -a /etc/timezone "$DEST_SYS/timezone" 2>/dev/null || true
crontab -l > "$DEST_SYS/root.crontab" 2>/dev/null || true
cp -a /etc/ssh/sshd_config "$DEST_SYS/sshd_config" 2>/dev/null || true
cp -a /root/.ssh/authorized_keys "$DEST_SYS/authorized_keys" 2>/dev/null || true
cp -a /etc/nginx/sites-available/skill-platform "$DEST_SYS/nginx-skill-platform.conf" 2>/dev/null || true
cp -a /etc/fail2ban/jail.local "$DEST_SYS/fail2ban-jail.local" 2>/dev/null || true
cp -a /etc/fail2ban/filter.d/sshd-closed.conf "$DEST_SYS/fail2ban-sshd-closed.conf" 2>/dev/null || true

# SSL 证书 + certbot 续期记录（2026-09-19 第十轮补充）
#
# 为什么需要：证书丢了，HTTPS 立刻中断；而重新签发受 Let's Encrypt 速率
# 限制，且需要域名校验（可能需临时改 DNS/放行 80 端口）。
# 备份证书可让恢复时直接还原、避免重新签发。
#
# 注意：这**包含私钥**（privkey.pem），属敏感材料 ——
# 故本目录权限应与 .env 同等级（600），且不要外传。
# certbot 的 renewal 配置也一并备份：它是自动续期的依据，
# 丢了会导致"证书还在但再也不会自动续"。
if [ -d /etc/letsencrypt ]; then
    # ⚠️ 必须用 tar，不能用 cp -a（2026-09-19 实测踩坑）
    #   /etc/letsencrypt/live/<域名>/*.pem 全部是**符号链接**，指向
    #   ../../archive/<域名>/xxxN.pem。
    #   `cp -a` 会保留符号链接 → 备份里只存下链接，有两个问题：
    #     · 链接是相对路径，依赖目录结构；一旦换机/移动就失效
    #     · 真实私钥内容**没有**被复制，等于没备份
    #   且 `chmod -R` **不跟随符号链接**，
    #   实测备份里 privkey.pem 显示 777（链接自身权限，无意义），
    #   既不安全又误导。
    #
    #   `tar` 默认**解引用**（存真实文件内容），恢复时是普通文件，
    #   权限也随内容一并可控。这才是正确的备份方式。
    ( cd /etc && tar czf "$DEST_SYS/letsencrypt.tar.gz" letsencrypt 2>/dev/null ) || true
    if [ -f "$DEST_SYS/letsencrypt.tar.gz" ]; then
        chmod 600 "$DEST_SYS/letsencrypt.tar.gz"
        # 校验归档可读且确含私钥（避免"备份了但内容不对"）
        #
        # ⚠️ 不能写 `if tar tzf ... | grep -q ...; then`（2026-09-19 实测）
        #   本脚本开头有 `set -uo pipefail`，而 pipefail 下**管道中任一命令
        #   非零则整条管道判失败** —— 即使 grep 匹配成功，只要 tar 有
        #   任何非零输出（警告/权限提示等），就会被判为"未检出私钥"。
        #   实测：归档里确实含 privkey1.pem（tar tzvf 可见），
        #   但该写法仍报「⚠️ 未检出私钥」——**检查项自己说谎**。
        #
        #   这正是本项目已记录过的同一陷阱（见 push_sync.sh 第 52 行注释：
        #   「ssh 因拒绝 shell 访问返回 1 → 我原先 grep 的字面量匹配不到」）。
        #   正确做法：先把输出存进变量再判断（也不要 `|| true` 掩盖真实失败）。
        _tar_list="$(tar tzf "$DEST_SYS/letsencrypt.tar.gz" 2>/dev/null || true)"
        if printf '%s\n' "$_tar_list" | grep -q 'archive/.*privkey'; then
            certok="含私钥 ✅"
        else
            certok="⚠️ 未检出私钥"
        fi
        unset _tar_list
    else
        certok="⚠️ 归档创建失败"
    fi
fi
certbot_certs=$(ls -1 /etc/letsencrypt/live 2>/dev/null | grep -v README | wc -l)
echo "  SSL 证书: $certbot_certs 个域名（tar 归档 ${certok:-未备份}）"

# 记录当时的时区与时间，便于恢复后核对
{
  echo "# 备份时刻的宿主状态（用于恢复后核对）"
  echo "timezone: $(cat /etc/timezone 2>/dev/null)"
  echo "date:     $(date '+%F %T %Z')"
  echo "hostname: $(hostname)"
} > "$DEST_SYS/_meta.txt" 2>/dev/null || true

sysfiles=$(ls -1 "$DEST_SYS" 2>/dev/null | wc -l)
echo "  系统配置: $sysfiles 个文件 -> $DEST_SYS"

# ---- 轮转：只保留最近 N 天 --------------------------------------------------
if [ -d "$BACKUP_ROOT" ]; then
    # 排除 _pre-restore-*（恢复前安全副本，单独保留，不参与快照轮转）
    ls -1dt "$BACKUP_ROOT"/*/ 2>/dev/null | grep -v '/_pre-restore'         | tail -n +$((KEEP_DAYS + 1)) | while read -r old; do
        rm -rf "$old"
    done
fi

# ---- 结果 -------------------------------------------------------------------
n=$(find "$DEST" -type f | wc -l)
sz=$(du -sh "$DEST" 2>/dev/null | cut -f1)
echo "[$(date '+%F %T')] 备份完成: $DEST  文件 $n 个 / $sz"
[ "$fail" = "0" ] || echo "  ⚠️ 有部分失败，请检查"
exit $fail

#!/usr/bin/env bash
# ============================================================================
# 完整备份（tar 打包 + 7 天轮转）—— 每日 03:00 由 cron 调用
#
# 为什么从「cron 行内命令」提升为独立脚本
# ---------------------------------------------------------------------------
# 这段逻辑原先直接写在 crontab 的命令列里：
#     0 3 * * * cd /opt/kol-skills-platform && tar czf \
#         /opt/kol-backups/kol-backup-$(date +\%F).tar.gz ... ; find ... -delete
# 有两个**互相叠加**的坑，且都是静默的：
#
# ① `%` 必须转义，且没有任何机制帮你转
#    原行写的是 `\%F`（转义过，正确）。但一旦有人照抄时写成 `%F`，
#    crontab 会把**第一个裸 `%`** 之后的内容当作 stdin 而非命令的一部分：
#        tar czf .../kol-backup-$(date +        ← 命令在此截断
#        F).tar.gz ... ; find ... -delete       ← 整段变成标准输入
#    后果：备份文件名残缺、`find ... -delete` 那半句根本没执行
#    → 归档无限累积、磁盘慢慢涨满。而且它**不会报错**。
#
# ② 行内的 `&&` / `;` 由 **cron 自己的 shell** 解释，不经过任何 runner
#    cron 是 `sh -c "<整行>"` 执行的，所以这行拆成三段：
#        cd /opt/kol-skills-platform && tar czf ...   （第 1 段）
#        find ... -delete                             （第 2 段）
#    它确实能跑 —— 但**没有任何日志、校验、退出码回收**：
#    打包失败、磁盘写满、删除失败，全都无从知晓。
#    （这也正是「备份看起来一直在跑、出事了才发现没有备份」的典型成因。）
#
# 提升为独立脚本后：crontab 里只有一行「runner 调脚本」，
# 不含 `%`、不含 shell 元字符，且可以随时手工执行验证。
# 另外这里还补了两道原来没有的保障：**打包后校验归档可读**、
# **输出条目数与体积**（让「备份是否真的成功」在日志里可见）。
#
# 用法：
#   bash scripts/deploy/full_backup.sh            # 正常备份
#   bash scripts/deploy/full_backup.sh --dry-run  # 只打印将要执行的动作
# ============================================================================
set -uo pipefail

SKILL_DIR="/opt/kol-skills-platform"
BACKUP_DIR="/opt/kol-backups"
KEEP_DAYS=7
STAMP="$(date +%F)"
ARCHIVE="$BACKUP_DIR/kol-backup-$STAMP.tar.gz"

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

say() { printf '  %s\n' "$*"; }

if [ ! -d "$SKILL_DIR" ]; then
    echo "❌ 未找到 $SKILL_DIR" >&2
    exit 1
fi
mkdir -p "$BACKUP_DIR" || { echo "❌ 无法创建 $BACKUP_DIR" >&2; exit 1; }

say "备份目录：$BACKUP_DIR"
say "目标归档：$ARCHIVE"

if [ "$DRY" = "1" ]; then
    say "[dry-run] tar czf $ARCHIVE --exclude=.venv-host --exclude=node_modules data scripts sync"
    say "[dry-run] find $BACKUP_DIR -name 'kol-backup-*.tar.gz' -mtime +$KEEP_DAYS -delete"
    exit 0
fi

# ---- ① 打包 ----------------------------------------------------------------
# ⚠️ 打包失败必须**显式报错退出**：这是数据安全的最后一道防线，
#    静默失败等于「以为有备份、其实没有」。故不用 `2>/dev/null` 掩盖。
cd "$SKILL_DIR" || exit 1
if ! tar czf "$ARCHIVE" \
        --exclude=.venv-host --exclude=node_modules \
        data scripts sync 2>/tmp/full_backup_err.$$; then
    echo "❌ 打包失败：" >&2
    cat /tmp/full_backup_err.$$ >&2
    rm -f /tmp/full_backup_err.$$
    exit 2
fi
rm -f /tmp/full_backup_err.$$

# ---- ② 校验产物（防「tar 退出码 0 但文件是空的/损坏的」）--------------------
if [ ! -s "$ARCHIVE" ]; then
    echo "❌ 归档为空：$ARCHIVE" >&2
    exit 3
fi
if ! tar tzf "$ARCHIVE" >/dev/null 2>&1; then
    echo "❌ 归档损坏（tar tzf 失败）：$ARCHIVE" >&2
    exit 4
fi
_size="$(du -h "$ARCHIVE" | cut -f1)"
_n="$(tar tzf "$ARCHIVE" 2>/dev/null | wc -l)"
say "✅ 打包完成：$ARCHIVE（$_size，$_n 个条目）"

# ---- ③ 轮转（保留最近 KEEP_DAYS 天）----------------------------------------
_deleted="$(find "$BACKUP_DIR" -name 'kol-backup-*.tar.gz' -mtime +"$KEEP_DAYS" -print -delete | wc -l)"
say "✅ 已清理 $_deleted 个超过 $KEEP_DAYS 天的旧归档"

# 顺带报告剩余归档，便于在日志里一眼看出轮转是否真的在工作
say "当前归档（最近 5 个）："
ls -1t "$BACKUP_DIR"/kol-backup-*.tar.gz 2>/dev/null | head -5 | sed 's/^/    /'

exit 0

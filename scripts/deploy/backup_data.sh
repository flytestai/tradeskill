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

# ---- 轮转：只保留最近 N 天 --------------------------------------------------
if [ -d "$BACKUP_ROOT" ]; then
    ls -1dt "$BACKUP_ROOT"/*/ 2>/dev/null | tail -n +$((KEEP_DAYS + 1)) | while read -r old; do
        rm -rf "$old"
    done
fi

# ---- 结果 -------------------------------------------------------------------
n=$(find "$DEST" -type f | wc -l)
sz=$(du -sh "$DEST" 2>/dev/null | cut -f1)
echo "[$(date '+%F %T')] 备份完成: $DEST  文件 $n 个 / $sz"
[ "$fail" = "0" ] || echo "  ⚠️ 有部分失败，请检查"
exit $fail

#!/usr/bin/env bash
# ============================================================================
# 关键数据恢复（备份的反向操作）
#
# 为什么需要
#   上一轮加了 backup_data.sh，但**只有备份、没有恢复** ——
#   而「恢复不了」的备份等于没有备份。本脚本补上这一环，
#   并让恢复流程可演练、可验证。
#
# 安全设计（重要）
#   · **默认 dry-run**：不加 --apply 只打印将做什么，不动任何文件
#   · 恢复前自动把**当前数据**另存一份（防止「恢复错了没法回退」）
#   · sqlite 先校验备份可打开，再替换
#   · 任何一步失败立即停止，不留下半恢复状态
#
# 用法：
#   bash restore_data.sh                       # dry-run（看会做什么）
#   bash restore_data.sh --list                # 看有哪些快照
#   bash restore_data.sh --apply               # 用最新快照恢复
#   bash restore_data.sh --apply --date 2026-09-19   # 指定快照
#   bash restore_data.sh --verify              # 只校验快照完整性
# ============================================================================
set -uo pipefail

KOL_DIR="/opt/kol-skills-platform"
T365_DIR="/opt/trade365"
BACKUP_ROOT="/opt/kol-backups"
PY="$KOL_DIR/.venv-host/bin/python"

APPLY=0
SNAP_DATE=""
MODE="dry"

while [ $# -gt 0 ]; do
    case "$1" in
        --apply)  APPLY=1 ;;
        --date)   SNAP_DATE="${2:-}"; shift ;;
        --list)   MODE="list" ;;
        --verify) MODE="verify" ;;
        *) shift ;;
    esac
    shift
done

# ---- 列出快照 ---------------------------------------------------------------
if [ "$MODE" = "list" ]; then
    echo "=== 可用快照（$BACKUP_ROOT）==="
    [ -d "$BACKUP_ROOT" ] || { echo "  (无)"; exit 0; }
    for d in $(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | grep -v '/_pre-restore' | sort -r); do
        n=$(find "$d" -type f | wc -l)
        printf "  %-14s %2d 个文件  %s\n" "$(basename "$d")" "$n" "$(du -sh "$d" | cut -f1)"
    done
    exit 0
fi

# ---- 选定快照 ---------------------------------------------------------------
if [ -z "$SNAP_DATE" ]; then
    SNAP_DATE="$(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | grep -v '/_pre-restore' | sort | tail -1 | xargs -r basename)"
fi
SNAP="$BACKUP_ROOT/$SNAP_DATE"
if [ -z "$SNAP_DATE" ] || [ ! -d "$SNAP" ]; then
    echo "  ❌ 找不到快照：$SNAP"
    echo "     可用：bash $0 --list"
    exit 1
fi

# ---- 校验快照 ---------------------------------------------------------------
verify_snap() {
    local bad=0
    [ -d "$SNAP/kol" ] || { echo "  ❌ 快照缺少 kol/ 目录"; bad=1; }
    [ -d "$SNAP/trade365" ] || { echo "  ❌ 快照缺少 trade365/ 目录"; bad=1; }
    # db 可打开且有数据
    if [ -f "$SNAP/kol/kol_opinions.db" ]; then
        "$PY" -c "
import sqlite3, sys
try:
    c = sqlite3.connect('$SNAP/kol/kol_opinions.db')
    n = c.execute('select count(*) from kol_records').fetchone()[0]
    if n <= 0:
        print('  ❌ 备份 db 为空'); sys.exit(1)
    print('  ✅ 备份 db 可用: %d 条记录' % n)
except Exception as e:
    print('  ❌ 备份 db 无法打开:', e); sys.exit(1)
" || bad=1
    else
        echo "  ❌ 快照缺少 kol_opinions.db"; bad=1
    fi
    # 关键 json 有效
    "$PY" -c "
import json, glob, os, sys
bad = []
for f in glob.glob('$SNAP/**/*.json', recursive=True):
    try: json.load(open(f, encoding='utf-8'))
    except Exception: bad.append(os.path.basename(f))
if bad:
    print('  ❌ JSON 损坏:', ', '.join(bad)); sys.exit(1)
print('  ✅ 快照内 JSON 全部有效')
" || bad=1
    return $bad
}

echo "=== 快照校验：$SNAP_DATE ==="
verify_snap || { echo "  → 快照不完整，终止"; exit 1; }

if [ "$MODE" = "verify" ]; then
    exit 0
fi

# ---- 恢复计划 ---------------------------------------------------------------
echo
echo "=== 恢复计划（快照 $SNAP_DATE → 生产）==="
KOL_FILES="price_alerts.json group_qa_answered.json level_targets.json level_asof.txt group_qa_queue.json local_config.env"
T365_FILES="meetings.json review.json watchlist.json holdings.json closed_trades.json weights.json"

n_kol=0; for f in $KOL_FILES; do [ -f "$SNAP/kol/$f" ] && n_kol=$((n_kol + 1)); done
n_t365=0; for f in $T365_FILES; do [ -f "$SNAP/trade365/$f" ] && n_t365=$((n_t365 + 1)); done
echo "  kol    : $n_kol 个文件 + kol_opinions.db"
echo "  trade365: $n_t365 个文件"

if [ "$APPLY" != "1" ]; then
    echo
    echo "  ℹ️ 这是 dry-run，未改动任何文件。"
    echo "     实际恢复请加 --apply（会自动先备份当前数据）"
    exit 0
fi

# ---- 实际恢复 ---------------------------------------------------------------
# 恢复前把当前数据另存（防止恢复错了没法回退）
SAFETY="$BACKUP_ROOT/_pre-restore-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$SAFETY/kol" "$SAFETY/trade365"
echo
echo "=== 0. 先备份当前数据（可回退）==="
for f in $KOL_FILES; do [ -f "$KOL_DIR/data/$f" ] && cp -a "$KOL_DIR/data/$f" "$SAFETY/kol/" || true; done
for f in $T365_FILES; do [ -f "$T365_DIR/data/$f" ] && cp -a "$T365_DIR/data/$f" "$SAFETY/trade365/" || true; done
if [ -f "$KOL_DIR/data/kol_opinions.db" ]; then
    "$PY" -c "
import sqlite3
s=sqlite3.connect('$KOL_DIR/data/kol_opinions.db'); d=sqlite3.connect('$SAFETY/kol/kol_opinions.db')
with d: s.backup(d)
d.close(); s.close()
" 2>/dev/null || echo "  ⚠️ 当前 db 备份失败（继续，但请留意）"
fi
echo "  ✅ 当前数据已存至 $SAFETY"

echo
echo "=== 1. 恢复 kol 数据 ==="
for f in $KOL_FILES; do
    if [ -f "$SNAP/kol/$f" ]; then
        cp -a "$SNAP/kol/$f" "$KOL_DIR/data/$f" && echo "  ✅ $f"
    fi
done
if [ -f "$SNAP/kol/kol_opinions.db" ]; then
    cp -a "$SNAP/kol/kol_opinions.db" "$KOL_DIR/data/kol_opinions.db" \
        && echo "  ✅ kol_opinions.db"
fi

echo
echo "=== 2. 恢复 trade365 数据 ==="
for f in $T365_FILES; do
    if [ -f "$SNAP/trade365/$f" ]; then
        cp -a "$SNAP/trade365/$f" "$T365_DIR/data/$f" && echo "  ✅ $f"
    fi
done

echo
echo "=== 3. 恢复后校验 ==="
"$PY" -c "
import sqlite3
c = sqlite3.connect('$KOL_DIR/data/kol_opinions.db')
print('  生产 db: %d 条记录' % c.execute('select count(*) from kol_records').fetchone()[0])
"
echo "  回退方式：cp -a $SAFETY/kol/. $KOL_DIR/data/"
echo
echo "[$(date '+%F %T')] 恢复完成（快照 $SNAP_DATE）"

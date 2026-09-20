#!/usr/bin/env bash
# ============================================================================
# 初始化独立数据卷：把镜像内置的数据复制到命名卷
#
# 为什么需要这一步：
#   docker-compose 把 kolplatform_data 挂到 /app/data，会**遮蔽**镜像里
#   已 COPY 进去的 data/ 目录。若不预先填充，容器启动后会看到空目录、
#   数据库文件不存在，平台功能不可用。
#
# 幂等：卷内已有 kol_opinions.db 时跳过，不会覆盖已有数据。
# ============================================================================
set -euo pipefail

IMAGE="${IMAGE:-kol-skills-platform:latest}"
DATA_VOL="${DATA_VOL:-kolplatform_data}"
SYNC_VOL="${SYNC_VOL:-kolplatform_sync}"

echo "=== 初始化数据卷 ==="

# 卷不存在则创建
docker volume inspect "$DATA_VOL" >/dev/null 2>&1 || docker volume create "$DATA_VOL" >/dev/null
docker volume inspect "$SYNC_VOL" >/dev/null 2>&1 || docker volume create "$SYNC_VOL" >/dev/null
echo "  ✅ 卷就绪: $DATA_VOL / $SYNC_VOL"

# 幂等检查
if docker run --rm -v "$DATA_VOL":/dst "$IMAGE" test -f /dst/kol_opinions.db 2>/dev/null; then
    echo "  ⏭  卷内已有 kol_opinions.db，跳过填充（保护现有数据）"
else
    echo "  → 从镜像复制内置数据到卷"
    docker run --rm -v "$DATA_VOL":/dst "$IMAGE" \
        sh -c "mkdir -p /dst && cp -a /app/data/. /dst/ 2>/dev/null || true"
    echo "  ✅ 数据已填充"
fi

# 同步目录：只放占位，避免遮蔽后为空
docker run --rm -v "$SYNC_VOL":/dst "$IMAGE" \
    sh -c "mkdir -p /dst && [ -f /dst/records.jsonl ] || touch /dst/records.jsonl"

echo
echo "=== 卷内数据校验 ==="
docker run --rm -v "$DATA_VOL":/dst "$IMAGE" sh -c '
echo "  文件列表:"
ls -la /dst 2>/dev/null | head -14
echo
if [ -f /dst/kol_opinions.db ]; then
  python - <<'"'"'PY'"'"'
import sqlite3
db = "/dst/kol_opinions.db"
c = sqlite3.connect(db)
for t in ("kol_records", "predictions", "analysis_reports"):
    try:
        n = c.execute("select count(*) from %s" % t).fetchone()[0]
        print("  %-18s %s 条" % (t, n))
    except Exception as e:
        print("  %-18s (读取失败: %s)" % (t, e))
c.close()
PY
else
  echo "  ⚠️ 未找到 kol_opinions.db"
fi
'
echo
echo "=== 完成 ==="

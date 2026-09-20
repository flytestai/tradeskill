#!/usr/bin/env bash
# ============================================================================
# 数据路径统一校验：确认容器与宿主机共享同一份数据
#
# 为什么需要（CRITICAL）
# ---------------------
# 平台是「容器 + 宿主机」混合形态：
#   · 容器（无 node）  → REST/MCP、发飞书消息
#   · 宿主机（有 node）→ 读群消息、12 个定时任务
# 所有脚本（23 个）都用 SKILL_DIR/data 作为数据目录。若容器挂的是
# Docker 卷而宿主机写源码目录，就会出现**两份互不可见的数据**：
#   宿主机同步拉到的新言论，容器 API 永远看不到（已实测复现）。
#
# ⚠️ 为什么不推荐「data 指向卷的软链」
#   本仓库 git 跟踪了 data/ 下的若干配置（alert_levels.json 等），
#   一旦执行 `git reset --hard` / `git checkout`，git 会重建普通目录，
#   **软链被覆盖**，问题复发（已实测踩坑）。
#
# ✅ 采用的方案：容器用 **bind mount** 挂宿主 data 目录
#      -v /opt/kol-skills-platform/data:/app/data
#   宿主机与容器天然共享同一目录，且不受 git 操作影响。
#   本脚本负责**校验**该方案是否生效，不再改动文件系统。
#
# 用法：
#   sudo bash scripts/deploy/setup_data_link.sh          # 校验（默认）
#   sudo bash scripts/deploy/setup_data_link.sh --verify # 同默认
# ============================================================================
set -uo pipefail

DEPLOY_DIR="/opt/kol-skills-platform"
VOL="kolplatform_data"
VOL_PATH="/var/lib/docker/volumes/${VOL}/_data"
DATA_LINK="$DEPLOY_DIR/data"
VERIFY_ONLY=0
[ "${1:-}" = "--verify" ] && VERIFY_ONLY=1

echo "=== 数据路径一致性校验（bind mount 方案）==="

REST="kolplatform-rest"
DATA_DIR="/opt/kol-skills-platform/data"

if ! docker inspect "$REST" >/dev/null 2>&1; then
    echo "  ⚠️ 容器 $REST 不存在，无法校验"
    exit 1
fi

# 取出容器 /app/data 的挂载源
src="$(docker inspect "$REST"     --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}' 2>/dev/null)"

if [ "$src" = "$DATA_DIR" ]; then
    echo "  ✅ 使用 bind mount：$src -> /app/data"
    n=$(ls -1 "$DATA_DIR" 2>/dev/null | wc -l)
    echo "     文件数: $n"
    if [ -f "$DATA_DIR/kol_opinions.db" ]; then
        cnt=$(python3 - <<'PY' 2>/dev/null || echo "?"
import sqlite3
c = sqlite3.connect("/opt/kol-skills-platform/data/kol_opinions.db")
print(c.execute("select count(*) from kol_records").fetchone()[0])
PY
)
        echo "     数据库: $cnt 条"
    fi
    exit 0
fi

if [ "$src" = "" ]; then
    echo "  🔴 /app/data 未挂载！容器看到的是镜像内置数据（与宿主机隔离）"
    echo "     修复：用 -v $DATA_DIR:/app/data 重启容器"
    exit 2
fi

echo "  🔴 挂载源不是宿主 data 目录（当前: $src）"
echo "     → 宿主机与容器数据可能不一致"
echo "     修复：用 -v $DATA_DIR:/app/data 重启容器"
exit 2

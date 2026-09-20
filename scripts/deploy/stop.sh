#!/usr/bin/env bash
# ============================================================================
# 停止 KOL Skills Platform 容器（保留数据卷与镜像，便于快速恢复）
#
#   bash scripts/deploy/stop.sh              # 停止并移除容器（数据保留）
#   bash scripts/deploy/stop.sh --keep       # 仅停止，不移除容器
# ============================================================================
set -euo pipefail

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

for c in kolplatform-rest kolplatform-mcp; do
    if docker ps -a --format '{{.Names}}' | grep -qx "$c"; then
        if [ "$KEEP" = "1" ]; then
            echo "  → 停止 $c"
            docker stop "$c" >/dev/null 2>&1 || true
        else
            echo "  → 停止并移除 $c"
            docker rm -f "$c" >/dev/null 2>&1 || true
        fi
    else
        echo "  ℹ️  $c 不存在"
    fi
done

echo
echo "=== 当前状态 ==="
docker ps -a --filter "name=kolplatform" --format '  {{.Names}}  {{.Status}}' || true
echo
echo "数据卷 kolplatform_data / kolplatform_sync 已保留（如需彻底清理见 uninstall.sh）"
echo "重新启动：bash scripts/deploy/start.sh"

#!/usr/bin/env bash
# ============================================================================
# 彻底卸载 KOL Skills Platform（仅清理本项目资源，不触碰宿主其他服务）
#
# ⚠️ 会删除数据卷（含 748 条言论记录），仅在确认不需要时执行。
#
#   bash scripts/deploy/uninstall.sh           # 交互确认
#   bash scripts/deploy/uninstall.sh --yes     # 跳过确认
#   bash scripts/deploy/uninstall.sh --keep-data  # 保留数据卷
# ============================================================================
set -euo pipefail

YES=0
KEEP_DATA=0
for a in "$@"; do
    case "$a" in
        --yes) YES=1 ;;
        --keep-data) KEEP_DATA=1 ;;
    esac
done

echo "本操作将移除："
echo "  - 容器 kolplatform-rest / kolplatform-mcp"
echo "  - 镜像 kol-skills-platform:latest / :mcp"
[ "$KEEP_DATA" = "0" ] && echo "  - 数据卷 kolplatform_data / kolplatform_sync  ⚠️ 含全部言论数据"
echo "不会触碰：flytest-* 容器、nginx、/opt/flytest、宿主 Python"
echo

if [ "$YES" != "1" ]; then
    read -r -p "确认继续? [y/N] " ans
    [ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "已取消"; exit 0; }
fi

echo
echo "=== 移除容器 ==="
for c in kolplatform-rest kolplatform-mcp; do
    docker rm -f "$c" >/dev/null 2>&1 && echo "  ✅ 已移除 $c" || echo "  ℹ️  $c 不存在"
done

echo
echo "=== 移除镜像 ==="
for img in kol-skills-platform:latest kol-skills-platform:mcp; do
    if docker image inspect "$img" >/dev/null 2>&1; then
        docker rmi -f "$img" >/dev/null 2>&1 && echo "  ✅ 已移除 $img" || echo "  ⚠️  $img 移除失败（可能被占用）"
    fi
done

if [ "$KEEP_DATA" = "0" ]; then
    echo
    echo "=== 移除数据卷 ==="
    for v in kolplatform_data kolplatform_sync; do
        docker volume rm "$v" >/dev/null 2>&1 && echo "  ✅ 已移除 $v" || echo "  ℹ️  $v 不存在"
    done
else
    echo
    echo "  ⏭  按 --keep-data 保留了数据卷"
fi

echo
echo "=== 剩余资源检查 ==="
docker ps -a --filter "name=kolplatform" --format '  {{.Names}}' || true
docker ps --format '  {{.Names}}  {{.Status}}' | head -8
echo
echo "完成。如无需保留代码，可手动删除目录：rm -rf /opt/kol-skills-platform"

#!/usr/bin/env bash
# ============================================================================
# 统一数据路径：让宿主机与容器读写同一份数据
#
# 为什么需要（CRITICAL）
# ---------------------
# 本平台的运行形态是「容器 + 宿主机」混合：
#   · 容器（无 node）  → 提供 REST/MCP、发送飞书消息
#   · 宿主机（有 node）→ 读飞书群消息、跑 12 个定时任务
#
# 而所有脚本（23 个）都用 `SKILL_DIR/data` 作为数据目录，
# 容器的数据卷却挂在容器外 —— 结果是**两份互不可见的数据**：
#
#   宿主机写 /opt/kol-skills-platform/data/kol_opinions.db  ← 同步/播报写这里
#   容器读 kolplatform_data 卷                              ← API 从卷里读
#
# 后果：宿主机同步拉到的新言论，**容器 API 永远看不到**。
#       实测已复现并确认（宿主机插入记录 → 容器查询不到）。
#
# 解法：把源码目录的 data 换成**指向数据卷的符号链接**，
#       使宿主机与容器共享同一物理目录，彻底消除双份数据。
#
# 用法：
#   sudo bash scripts/deploy/setup_data_link.sh          # 建立/校验软链
#   sudo bash scripts/deploy/setup_data_link.sh --verify # 只校验不改动
# ============================================================================
set -uo pipefail

DEPLOY_DIR="/opt/kol-skills-platform"
VOL="kolplatform_data"
VOL_PATH="/var/lib/docker/volumes/${VOL}/_data"
DATA_LINK="$DEPLOY_DIR/data"
VERIFY_ONLY=0
[ "${1:-}" = "--verify" ] && VERIFY_ONLY=1

echo "=== 数据路径统一（宿主机 ⇄ 容器）==="
echo "  目标卷路径: $VOL_PATH"

# ---- 前置检查 ----
if ! docker volume inspect "$VOL" >/dev/null 2>&1; then
    echo "  ❌ 数据卷 $VOL 不存在，请先运行 setup_volume.sh"
    exit 1
fi
if [ ! -d "$VOL_PATH" ]; then
    echo "  ❌ 卷路径不存在: $VOL_PATH"
    exit 1
fi

# ---- 校验模式 ----
if [ "$VERIFY_ONLY" = "1" ]; then
    if [ -L "$DATA_LINK" ]; then
        target="$(readlink "$DATA_LINK")"
        if [ "$target" = "$VOL_PATH" ]; then
            echo "  ✅ data 已正确指向数据卷"
            n=$(ls -1 "$DATA_LINK" 2>/dev/null | wc -l)
            echo "     文件数: $n"
            exit 0
        fi
        echo "  ⚠️ data 是软链但指向 $target（期望 $VOL_PATH）"
        exit 2
    fi
    echo "  ⚠️ data 是普通目录，未指向数据卷（宿主机与容器数据将不一致）"
    exit 2
fi

# ---- 已正确则跳过 ----
if [ -L "$DATA_LINK" ] && [ "$(readlink "$DATA_LINK")" = "$VOL_PATH" ]; then
    echo "  ⏭  data 已正确指向数据卷，无需改动"
    exit 0
fi

# ---- 备份（安全第一）----
BK="/root/kol-data-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK"
if [ -d "$DATA_LINK" ] && [ ! -L "$DATA_LINK" ]; then
    cp -a "$DATA_LINK" "$BK/data.before" 2>/dev/null || true
    echo "  ✅ 已备份原 data → $BK/data.before"
fi

# ---- 停容器（避免迁移时写入冲突）----
STOPPED=0
for c in kolplatform-rest kolplatform-mcp; do
    if docker ps --format '{{.Names}}' | grep -qx "$c"; then
        docker rm -f "$c" >/dev/null 2>&1 && { echo "  ⏸  已停容器 $c"; STOPPED=1; }
    fi
done

# ---- 把宿主机 data 中「卷里没有」的文件补进卷（不覆盖卷内已有）----
if [ -d "$DATA_LINK" ] && [ ! -L "$DATA_LINK" ]; then
    added=0
    for f in "$DATA_LINK"/* "$DATA_LINK"/.[!.]*; do
        [ -e "$f" ] || continue
        b="$(basename "$f")"
        if [ ! -e "$VOL_PATH/$b" ]; then
            cp -a "$f" "$VOL_PATH/$b" 2>/dev/null && added=$((added+1))
        fi
    done
    echo "  ✅ 补入卷内缺失文件: $added 个"

    # 原目录改名保留（便于排查），再建软链
    mv "$DATA_LINK" "${DATA_LINK}.hostbak-$(date +%H%M%S)" 2>/dev/null
fi

# ---- 建立软链 ----
ln -sfn "$VOL_PATH" "$DATA_LINK"
echo "  ✅ 软链已建立: data -> $VOL_PATH"

# ---- 校验 ----
echo ""
echo "=== 校验 ==="
ls -ld "$DATA_LINK" | sed 's/^/  /'
if [ -f "$DATA_LINK/kol_opinions.db" ]; then
    cnt=$(python3 - <<'PY' 2>/dev/null || echo "?"
import sqlite3
c = sqlite3.connect("/opt/kol-skills-platform/data/kol_opinions.db")
print(c.execute("select count(*) from kol_records").fetchone()[0])
PY
)
    echo "  通过软链读取数据库: $cnt 条"
fi

# ---- 重启容器 ----
if [ "$STOPPED" = "1" ]; then
    echo ""
    echo "=== 重启容器 ==="
    cd "$DEPLOY_DIR" && bash scripts/deploy/start.sh 2>&1 | grep -E "REST|配置" | head -2
    if docker image inspect kol-skills-platform:mcp >/dev/null 2>&1; then
        docker rm -f kolplatform-mcp >/dev/null 2>&1
        docker run -d --name kolplatform-mcp --restart unless-stopped --no-healthcheck \
            -p 127.0.0.1:8021:8000 --env-file .env.runtime \
            -e PLATFORM_HOST=0.0.0.0 -e PLATFORM_MCP_PORT=8000 \
            -v "${VOL}":/app/data:ro -v kolplatform_sync:/app/sync \
            --memory 400m kol-skills-platform:mcp \
            python scripts/api/mcp_server.py --http --host 0.0.0.0 --port 8000 >/dev/null \
            && echo "  ✅ MCP 已重启"
    fi
fi

echo ""
echo "=== 完成 ==="
echo "  宿主机与容器现在读写同一份数据（$VOL_PATH）"
echo "  校验命令: bash $0 --verify"

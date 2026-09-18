#!/usr/bin/env bash
# ============================================================================
# 启动 KOL Skills Platform 容器（完全隔离）
#
# 隔离要点：
#   - 容器名 kolplatform-*，与现有 flytest-* / cli-proxy-api 无命名冲突
#   - 端口绑定 127.0.0.1:8020（宿主已占用 22/80/443/1455/8010/8085/8317/8912/…）
#   - 数据用 **bind mount** 挂宿主 data/ 目录（与宿主机脚本共享同一份数据）
#   - 宿主机 Python/系统包完全不动，依赖全在镜像内
#
# 用法：
#   bash scripts/deploy/start.sh            # 启动 REST
#   bash scripts/deploy/start.sh --with-mcp # 同时启动 MCP（需先构建 mcp 镜像）
#   bash scripts/deploy/start.sh --restart  # 重启（先删旧容器）
# ============================================================================
set -euo pipefail

IMAGE="${IMAGE:-kol-skills-platform:latest}"
MCP_IMAGE="${MCP_IMAGE:-kol-skills-platform:mcp}"
REST_NAME="kolplatform-rest"
MCP_NAME="kolplatform-mcp"
REST_PORT="${REST_PORT:-8020}"
MCP_PORT="${MCP_PORT:-8021}"
# ⚠️ 数据目录用 **bind mount**（而非 Docker 卷）：
#    宿主机脚本（同步/播报）与容器必须共享同一份数据。
#    用卷会导致「宿主机写源码目录、容器读卷」→ 两份互不可见的数据（已实测踩坑）。
#    也**不能**用「data 指向卷的软链」—— 本仓库 git 跟踪了 data/ 下的配置文件，
#    git reset/checkout 会重建普通目录并覆盖软链（已实测踩坑）。
SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="${ENV_FILE:-$SKILL_DIR/.env}"
ENV_RUNTIME="$SKILL_DIR/.env.runtime"

WITH_MCP=0
RESTART=0
for a in "$@"; do
    case "$a" in
        --with-mcp) WITH_MCP=1 ;;
        --restart)  RESTART=1 ;;
    esac
done

cd /opt/kol-skills-platform

# ---------------------------------------------------------------------------
# 生成干净的 env 文件（去掉注释与空行）
#   Docker 18.09 的 --env-file 对注释/空行的容错较差，预先过滤更稳。
# ---------------------------------------------------------------------------
if [ -f "$ENV_FILE" ]; then
    grep -vE '^\s*#|^\s*$' "$ENV_FILE" | sed 's/\r$//' > "$ENV_RUNTIME"
    chmod 600 "$ENV_RUNTIME"
    echo "  ✅ 运行时配置: $ENV_RUNTIME ($(wc -l < "$ENV_RUNTIME") 项)"
else
    echo "  ⚠️  未找到 $ENV_FILE，将以无鉴权模式启动"
    : > "$ENV_RUNTIME"
fi

# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------
docker image inspect "$IMAGE" >/dev/null 2>&1 || {
    echo "  ❌ 镜像不存在: $IMAGE"; echo "     请先构建: docker build -f scripts/deploy/Dockerfile -t $IMAGE ."; exit 1; }

# 数据目录必须有数据库，否则容器起来也是空库
[ -f "$SKILL_DIR/data/kol_opinions.db" ] || {
    echo "  ❌ 缺少 $SKILL_DIR/data/kol_opinions.db"; exit 1; }

# 数据目录不应是软链：本仓库 git 跟踪 data/ 下的配置，
# git reset/checkout 会重建普通目录并覆盖软链，导致数据不一致复发。
if [ -L "$SKILL_DIR/data" ]; then
    echo "  ⚠️  data 是软链 —— git 操作可能覆盖它，建议改为真实目录"
fi

echo "  ✅ 前置检查通过"

# ---------------------------------------------------------------------------
# 启动 REST
# ---------------------------------------------------------------------------
if docker ps -a --format '{{.Names}}' | grep -qx "$REST_NAME"; then
    if [ "$RESTART" = "1" ]; then
        echo "  → 移除旧容器 $REST_NAME"
        docker rm -f "$REST_NAME" >/dev/null
    else
        echo "  ℹ️  $REST_NAME 已存在；如需重建请加 --restart"
        docker start "$REST_NAME" >/dev/null 2>&1 || true
        docker ps --filter "name=$REST_NAME" --format '  {{.Names}}  {{.Status}}  {{.Ports}}'
        exit 0
    fi
fi

echo "  → 启动 $REST_NAME (127.0.0.1:$REST_PORT -> 8000)"
docker run -d \
    --name "$REST_NAME" \
    --restart unless-stopped \
    -p "127.0.0.1:${REST_PORT}:8000" \
    --env-file "$ENV_RUNTIME" \
    -e PLATFORM_HOST=0.0.0.0 \
    -e PLATFORM_PORT=8000 \
    -v "$SKILL_DIR/data":/app/data \
    -v "$SKILL_DIR/sync":/app/sync \
    --memory 320m --memory-swap 700m --cpus 0.8 \
    --log-opt max-size=10m --log-opt max-file=3 \
    "$IMAGE" >/dev/null

# ---------------------------------------------------------------------------
# 可选：启动 MCP
# ---------------------------------------------------------------------------
if [ "$WITH_MCP" = "1" ]; then
    if docker image inspect "$MCP_IMAGE" >/dev/null 2>&1; then
        docker rm -f "$MCP_NAME" >/dev/null 2>&1 || true
        # --no-healthcheck：MCP 镜像继承了 Dockerfile 的 HEALTHCHECK（探 /healthz），
        # 但 MCP 只暴露 /mcp（POST + SSE），探 /healthz 恒为 404，
        # 会把容器误标为 unhealthy。故此处显式关闭健康检查。
        echo "  → 启动 $MCP_NAME (127.0.0.1:$MCP_PORT -> 8000)"
        docker run -d \
            --name "$MCP_NAME" \
            --restart unless-stopped \
            --no-healthcheck \
            -p "127.0.0.1:${MCP_PORT}:8000" \
            --env-file "$ENV_RUNTIME" \
            -e PLATFORM_HOST=0.0.0.0 \
            -v "$SKILL_DIR/data":/app/data:ro \
            -v "$SKILL_DIR/sync":/app/sync \
            --memory 400m --memory-swap 800m --cpus 0.8 \
            --log-opt max-size=10m --log-opt max-file=3 \
            "$MCP_IMAGE" \
            python scripts/api/mcp_server.py --http --host 0.0.0.0 --port 8000 >/dev/null
    else
        echo "  ⚠️  MCP 镜像不存在（$MCP_IMAGE），跳过。"
        echo "     构建：docker build -f scripts/deploy/Dockerfile --build-arg INSTALL_MCP=1 -t $MCP_IMAGE ."
    fi
fi

# ---------------------------------------------------------------------------
# 等待并自检
# ---------------------------------------------------------------------------
echo "  → 等待服务就绪..."
sleep 8

KEY="$(grep -E '^PLATFORM_API_KEYS=' "$ENV_RUNTIME" 2>/dev/null | cut -d= -f2- | cut -d, -f1 || true)"

echo
echo "=== 容器状态 ==="
docker ps --filter "name=kolplatform" --format '  {{.Names}}  {{.Status}}  {{.Ports}}'

echo
echo "=== 健康检查 ==="
if command -v curl >/dev/null 2>&1; then
    if [ -n "$KEY" ]; then
        curl -sS -m 20 -H "X-API-Key: $KEY" "http://127.0.0.1:${REST_PORT}/healthz" \
            | head -c 500
    else
        curl -sS -m 20 "http://127.0.0.1:${REST_PORT}/healthz" | head -c 500
    fi
    echo
else
    echo "  (无 curl，跳过)"
fi

echo
echo "=== 启动完成 ==="
echo "  REST : http://127.0.0.1:${REST_PORT}"
[ "$WITH_MCP" = "1" ] && echo "  MCP  : http://127.0.0.1:${MCP_PORT}"
echo "  日志 : docker logs -f $REST_NAME"
echo "  停止 : bash scripts/deploy/stop.sh"

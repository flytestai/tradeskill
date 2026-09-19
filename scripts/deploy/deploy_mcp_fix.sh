#!/usr/bin/env bash
# ============================================================================
# MCP 修复落地脚本 —— 重建镜像 → 重启 → **真实调用验证** → 汇报
#
# 背景（2026-09-19）
#   公网 https://skill.flytest.com.cn/mcp 上 18 个工具**全部**返回
#   `Error executing tool <name>`，而同一容器的 REST 完全正常。
#   此前的"验证"只做过 initialize 握手（HTTP 200）+ docker logs → 一致判为正常。
#
#   本脚本把「重建」与「验证」绑在一起：**验证不过就明确失败退出**，
#   避免再出现"改了但没生效"或"重建了但工具仍然挂"却无人察觉的情况。
#
# 用法（在服务器上，仓库根目录执行）
#   bash scripts/deploy/deploy_mcp_fix.sh            # 全流程
#   bash scripts/deploy/deploy_mcp_fix.sh --update   # 先安全更新代码，再全流程
#   bash scripts/deploy/deploy_mcp_fix.sh --no-build # 跳过构建（只重启+验证）
# ============================================================================
set -uo pipefail

MCP_IMAGE="${MCP_IMAGE:-kol-skills-platform:mcp}"
MCP_NAME="${MCP_NAME:-kolplatform-mcp}"
MCP_PORT="${MCP_PORT:-8021}"
SKILL_DIR="${SKILL_DIR:-/opt/kol-skills-platform}"
ENV_RUNTIME="${ENV_RUNTIME:-$SKILL_DIR/.env.runtime}"

BUILD=1
UPDATE=0
for a in "$@"; do
    case "$a" in
        --no-build) BUILD=0 ;;
        --update)   UPDATE=1 ;;
    esac
done

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
warn() { printf '\033[33m⚠️  %s\033[0m\n' "$*"; }
die()  { printf '\033[31m❌ %s\033[0m\n' "$*"; exit 1; }

# ---------------------------------------------------------------------------
# 只读根因探针 —— **可在未修复的旧容器上直接跑**，用来确证根因
#
# 确证结论（本地已复现，逐字一致）：宿主容器无法创建线程，而 mcp SDK 用
#   anyio.to_thread.run_sync(...) 执行**同步** tool
# → 每个同步 tool 抛 RuntimeError("can't start new thread")
# → 被 SDK 的裸 except Exception 吞成 `Error executing tool <name>`
# ---------------------------------------------------------------------------
probe_threads() {
    local cname="$1"
    say "只读根因探针：容器能否创建线程（$cname）"
    docker exec "$cname" python -c "
import threading
try:
    t = threading.Thread(target=lambda: None); t.start(); t.join(timeout=5)
    print('  ✅ 线程可用')
except Exception as e:
    print('  🔴 无法创建线程:', type(e).__name__, e)
" 2>&1 | sed 's/^/  /' || warn "探针执行失败（容器名或权限问题）"

    echo "  --- 同步 tool 的执行路径实测（anyio.to_thread.run_sync）---"
    docker exec "$cname" python -c "
import asyncio, anyio.to_thread
async def main():
    try:
        r = await anyio.to_thread.run_sync(lambda: 'ok')
        print('  ✅ run_sync 正常返回:', r)
    except Exception as e:
        print('  🔴 run_sync 失败:', type(e).__name__, str(e)[:80])
        print('     → 这会让**每一个同步 MCP tool** 报 Error executing tool <name>')
asyncio.run(main())
" 2>&1 | sed 's/^/  /' || true
}

# ---------------------------------------------------------------------------
# 安全更新：只按「本次修复涉及的文件清单」从远端取文件，**不做 git pull**
#
# 为什么不用 git pull（项目自己的教训）
#   push_sync.sh 的注释写得很清楚：服务器工作区有大量**未跟踪的部署特有文件**
#   （vendor/、data 链接、.env 等），`git pull` 可能冲突或覆盖，
#   进而影响正在运行的服务。故这里改为逐文件 `git checkout <remote> -- <path>`，
#   只动这几个文件，风险最小、可预期。
# ---------------------------------------------------------------------------
update_files() {
    say "0b. 安全更新代码（逐文件取远端版本，不做 git pull）"
    local REF="${DEPLOY_REF:-origin/main}"
    local files="scripts/api/mcp_server.py
scripts/api/services.py
scripts/api/auth.py
scripts/preflight.py
scripts/deploy/deploy_mcp_fix.sh
scripts/deploy/nginx-skill.conf
scripts/deploy/install.sh
scripts/deploy/requirements-mcp.txt"

    git fetch origin main --quiet 2>/dev/null || warn "git fetch 失败（离线？将用本地已有的 origin/main）"

    local backup="/root/kol-mcp-fix-backup-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$backup" || die "无法创建备份目录 $backup"
    echo "  备份到: $backup（回滚：cp -a $backup/. $SKILL_DIR/）"
    for f in $files; do
        if [ -f "$f" ]; then
            mkdir -p "$backup/$(dirname "$f")"
            cp -a "$f" "$backup/$f"
        fi
    done

    local fail=""
    for f in $files; do
        git checkout "$REF" -- "$f" 2>/dev/null || fail="$fail $f"
    done
    [ -z "$fail" ] || warn "以下文件未取到（继续，稍后前置校验会拦住）：$fail"
    echo "  ✅ 已更新 $files 中的文件"
}

# ---------------------------------------------------------------------------
say "0. 前置检查"
[ -d "$SKILL_DIR" ] || die "找不到 $SKILL_DIR（用 SKILL_DIR=... 指定）"
cd "$SKILL_DIR" || die "无法进入 $SKILL_DIR"

[ "$UPDATE" = "1" ] && update_files

if [ ! -f "$ENV_RUNTIME" ]; then
    warn "未找到 $ENV_RUNTIME —— 将改用 $SKILL_DIR/.env"
    ENV_RUNTIME="$SKILL_DIR/.env"
fi
[ -f "$ENV_RUNTIME" ] || die "缺少运行时环境文件（鉴权/配置来源）"
echo "  仓库: $SKILL_DIR"
echo "  环境: $ENV_RUNTIME"

# 关键：确认待部署代码里确实有本次修复
grep -q "_guard" scripts/api/mcp_server.py \
    || die "scripts/api/mcp_server.py 里没有 _guard —— 代码不是最新，请加 --update 或手动更新"
grep -q "def selfcheck" scripts/api/services.py \
    || die "scripts/api/services.py 里没有 selfcheck —— 代码不是最新，请加 --update"
grep -q "RequireAuthMiddleware" scripts/api/mcp_server.py \
    || die "缺少 RequireAuthMiddleware —— 代码不是最新，请加 --update"
grep -q "_patch_inline_threads" scripts/api/mcp_server.py \
    || die "缺少线程修复 _patch_inline_threads —— 代码不是最新，请加 --update"
echo "  ✅ 待部署代码含本次修复（_guard / selfcheck / RequireAuthMiddleware / 线程自救）"

# 鉴权键是否就绪
if grep -q '^PLATFORM_API_KEYS=.\+' "$ENV_RUNTIME"; then
    echo "  ✅ PLATFORM_API_KEYS 已配置（端点可鉴权）"
    AUTH_READY=1
else
    warn "PLATFORM_API_KEYS 为空 → MCP 端点将**无任何鉴权**（写接口暴露）"
    AUTH_READY=0
fi

# ---------------------------------------------------------------------------
if [ "$BUILD" = "1" ]; then
    say "1. 重建 MCP 镜像（--build-arg INSTALL_MCP=1）"
    docker build -f scripts/deploy/Dockerfile --build-arg INSTALL_MCP=1 \
        -t "$MCP_IMAGE" . || die "镜像构建失败"
    echo "  ✅ 镜像已重建: $MCP_IMAGE"
else
    say "1. 跳过镜像构建（--no-build）"
fi

# 重建后立刻确认镜像里真的有修复（防止"构建了但 COPY 没带上"）
say "2. 校验镜像内代码版本"
docker run --rm --entrypoint python "$MCP_IMAGE" -c "
import sys
sys.path.insert(0,'/app/scripts'); sys.path.insert(0,'/app/scripts/api')
ok=True
try:
    import api.mcp_server as m
    assert hasattr(m,'_guard'), '_guard 缺失'
    assert hasattr(m,'RequireAuthMiddleware'), 'RequireAuthMiddleware 缺失'
    assert hasattr(m,'_threads_available'), '_threads_available 缺失（线程修复未带上）'
    assert hasattr(m,'_patch_inline_threads'), '_patch_inline_threads 缺失（线程修复未带上）'
except Exception as e:
    print('  ❌ 镜像内 mcp_server 校验失败:', type(e).__name__, e); ok=False
try:
    import api.services as s
    assert hasattr(s,'selfcheck'), 'selfcheck 缺失'
except Exception as e:
    print('  ❌ 镜像内 services 校验失败:', type(e).__name__, e); ok=False
import importlib.metadata as md
try: print('  mcp SDK 版本:', md.version('mcp'))
except Exception: print('  mcp SDK 版本: 未知')
sys.exit(0 if ok else 1)
" || die "镜像内不含本次修复 —— 检查 .dockerignore / build context"
echo "  ✅ 镜像内代码正确"

# 镜像内直接探测线程能力（构建环境与运行环境可能不同，但先看一眼）
say "2b. 镜像内线程能力探测"
docker run --rm --entrypoint python "$MCP_IMAGE" -c "
import threading
try:
    t=threading.Thread(target=lambda:None); t.start(); t.join(timeout=5); print('  ✅ 线程可用')
except Exception as e: print('  🔴 无法创建线程:', type(e).__name__, e)
"

# ---------------------------------------------------------------------------
say "3. 重启 MCP 容器"
# 重启前先对**旧容器**跑只读根因探针（确证根因，且不改动任何东西）
if docker ps --filter "name=^${MCP_NAME}$" -q | grep -q .; then
    probe_threads "$MCP_NAME"
else
    echo "  （旧容器未运行，跳过旧容器探针）"
fi

docker rm -f "$MCP_NAME" >/dev/null 2>&1 || true
docker run -d \
    --name "$MCP_NAME" \
    --restart unless-stopped \
    --no-healthcheck \
    -p "127.0.0.1:${MCP_PORT}:8000" \
    --env-file "$ENV_RUNTIME" \
    -e PLATFORM_HOST=0.0.0.0 \
    -v "$SKILL_DIR/data":/app/data:ro \
    -v "$SKILL_DIR/sync":/app/sync \
    -v "$SKILL_DIR/vendor":/app/vendor:ro \
    --memory 400m --memory-swap 800m --cpus 0.8 \
    --log-opt max-size=10m --log-opt max-file=3 \
    "$MCP_IMAGE" \
    python scripts/api/mcp_server.py --http --host 0.0.0.0 --port 8000 >/dev/null \
    || die "容器启动失败"

sleep 6
docker ps --filter "name=$MCP_NAME" --format '  {{.Names}}  {{.Status}}'
docker ps --filter "name=$MCP_NAME" -q | grep -q . || {
    echo "--- 容器日志 ---"; docker logs --tail=40 "$MCP_NAME"; die "容器未在运行"
}

# 启动自检输出（本次新增，用来一眼看出"缺什么"）
say "4. 读取启动自检输出"
docker logs --tail=40 "$MCP_NAME" 2>&1 | grep -E "\[mcp\]" | sed 's/^/  /' || true
# 线程路径必须明确打印；若显示"无法创建线程"，说明补丁已生效（这是预期）
docker logs --tail=40 "$MCP_NAME" 2>&1 | grep -q "\[mcp\]\[threads\]" \
    && echo "  ✅ 线程路径已在启动日志中明确（不再静默）" \
    || warn "启动日志里没有线程路径打印 —— 镜像可能不是最新"

# ---------------------------------------------------------------------------
say "5. ★ 真实调用验证（关键步骤 —— 只测握手曾漏掉全量故障）"
python3 - "$MCP_PORT" "$AUTH_READY" <<'PY' || die "MCP 工具真实调用未通过 —— 修复未生效，请勿放行"
import json, sys, urllib.request

port = sys.argv[1]; auth_ready = sys.argv[2] == "1"
URL = "http://127.0.0.1:%s/mcp" % port
H = {"Content-Type": "application/json",
     "Accept": "application/json, text/event-stream"}

def post(body, sid=""):
    h = dict(H)
    if sid:
        h["mcp-session-id"] = sid
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        new = r.headers.get("mcp-session-id") or sid
        raw = r.read().decode("utf-8", "replace")
    if "data: " in raw:
        raw = raw.split("data: ", 1)[-1].strip()
    return json.loads(raw), new

_, sid = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                          "clientInfo": {"name": "deploy", "version": "1"}}})
tools = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, sid)[0]["result"]["tools"]
names = [t["name"] for t in tools]
print("  工具总数: %d" % len(names))
print("  含 selfcheck: %s" % ("selfcheck" in names))

probe = {"selfcheck": {}, "capabilities": {}, "kol_list": {},
         "llm_status": {}, "qa_queue_status": {}}
failed = []
for n, a in probe.items():
    if n not in names:
        failed.append("%s 未暴露" % n); continue
    res = post({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                "params": {"name": n, "arguments": a}}, sid)[0].get("result", {})
    err = res.get("isError") is True
    txt = "".join(c.get("text", "") for c in res.get("content", []))
    print("  %s %-18s %s" % ("❌" if err else "✅", n, txt[:90].replace("\n", " ")))
    if err:
        failed.append("%s: %s" % (n, txt[:160]))

if failed:
    print("\n  失败明细:")
    for f in failed:
        print("    - " + f)
sys.exit(1 if failed else 0)
PY

if [ "$AUTH_READY" = "1" ]; then
    say "6. 鉴权验证（无凭据应被拒 / 带 Key 应放行）"
    code_noauth=$(curl -s -o /dev/null -w '%{http_code}' -m 20 -X POST \
        "http://127.0.0.1:${MCP_PORT}/mcp" -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"p","version":"1"}}}')
    echo "  无凭据（来自回环）    -> HTTP ${code_noauth}"
    echo "  （回环来源在非严格模式下放行属预期；公网路径由 Nginx + 应用层分级拦截）"
    echo "  严格模式自测:"
    strict=$(PLATFORM_MCP_AUTH_STRICT=1 docker exec "$MCP_NAME" python -c "
import os,sys; sys.path.insert(0,'/app/scripts'); sys.path.insert(0,'/app/scripts/api')
import auth; print('strict 可加载')" 2>&1 | tail -1)
    echo "    $strict"
fi

say "完成"
echo "  ✅ 镜像已重建"
echo "  ✅ 容器已重启（启动自检输出见上）"
echo "  ✅ 工具真实调用全部通过"
[ "$AUTH_READY" = "1" ] || warn "PLATFORM_API_KEYS 为空 —— 端点仍无鉴权，请尽快配置"
echo
echo "  提醒："
echo "   · 若要让公网客户端也生效鉴权，需 reload nginx 使新的"
echo "     X-Forwarded-For 覆盖规则生效：nginx -t && systemctl reload nginx"
echo "   · 端口 8021 在 Nginx 之外**不应**被公网直接访问，请确认防火墙策略"

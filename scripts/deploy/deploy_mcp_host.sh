#!/usr/bin/env bash
# ============================================================================
# MCP 服务部署（宿主 venv + systemd）—— 可重复执行、带**真实调用验证**
#
# 为什么不用 Docker（与 deploy_mcp_fix.sh 的分工）
# ---------------------------------------------------------------------------
# 仓库里原有方案是 docker-compose 的 `--profile mcp`（见 docker-compose.yml）。
# 那套在**当前这台**服务器上不成立，实测原因三条：
#   1) 宿主仅 956MB 内存，REST + MCP + trade365 已是常态占用；
#      再起一个容器要多一份 Python 运行时与镜像预算，得不偿失
#   2) docker 当前**没有任何镜像**（重建后未拉取），从零 build 需装构建依赖
#   3) 日志要落 /var/log/kol-platform（容器里没有该目录，旧单元因此直接起不来）
# 而宿主本来就有 `.venv-host`（REST/定时任务都在用），装一个 `mcp` 包即可，
# 与 kol-platform.service 同一套运维方式（systemctl / journalctl），
# preflight 也能直接打通（它探的就是 127.0.0.1:8021）。
#
# 所以：**宿主 systemd 为生产形态**；deploy_mcp_fix.sh 保留给需要容器隔离的场景。
#
# ⚠️ 为什么验证必须是「真实调用」而不是「能连通」
#   2026-09-19 的血案：公网 MCP 上 18 个工具**全部**返回
#   `Error executing tool <name>`，而 REST 完全正常。
#   当时的"验证"只做了 initialize 握手（HTTP 200）+ docker logs → 判为正常。
#   本脚本把「部署」与「逐工具真实调用」绑死：**验证不过就明确失败退出**，
#   避免再出现「部署了但工具其实是挂的」却无人察觉。
#
# 用法（在服务器上，仓库根目录执行）：
#   bash scripts/deploy/deploy_mcp_host.sh              # 安装依赖 → 装单元 → 启动 → 验证
#   bash scripts/deploy/deploy_mcp_host.sh --no-install # 跳过 pip（只重装单元+验证）
#   bash scripts/deploy/deploy_mcp_host.sh --uninstall  # 停止并移除单元
# ============================================================================
set -uo pipefail

SKILL_DIR="${SKILL_DIR:-/opt/kol-skills-platform}"
VENV="$SKILL_DIR/.venv-host"
PY="$VENV/bin/python"
PORT="${MCP_PORT:-8021}"
UNIT_NAME="kol-platform-mcp.service"
UNIT_SRC="$SKILL_DIR/scripts/deploy/systemd/$UNIT_NAME"
UNIT_DST="/etc/systemd/system/$UNIT_NAME"
REQ="$SKILL_DIR/scripts/deploy/requirements-mcp.txt"

INSTALL=1
for a in "$@"; do
    case "$a" in
        --no-install) INSTALL=0 ;;
        --uninstall)
            sudo systemctl disable --now kol-platform-mcp 2>/dev/null || true
            sudo rm -f "$UNIT_DST"
            sudo systemctl daemon-reload
            echo "  ✅ 已卸载 MCP 服务（单元已移除）"
            exit 0 ;;
    esac
done

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m⚠️  %s\033[0m\n' "$*"; }
die()  { printf '\033[31m❌ %s\033[0m\n' "$*"; exit 1; }

say "0. 前置检查"
[ -x "$PY" ] || die "未找到 $VENV（REST 也在用它，请先修好基础环境）"
[ -f "$UNIT_SRC" ] || die "未找到 $UNIT_SRC"
[ -f "$SKILL_DIR/.env" ] || warn ".env 不存在 —— MCP 会缺飞书凭据与 API Key"
echo "  ✅ venv=$VENV"
echo "  ✅ 单元=$UNIT_SRC"
# 若端口已被别的进程占用，先暴露出来（否则 systemd 会以「启动失败」告终，
# 而真实原因只是端口冲突，日志里不一定看得清）
if ss -lntp 2>/dev/null | grep -q ":${PORT} "; then
    warn "端口 $PORT 已被占用，占用者："
    ss -lntp 2>/dev/null | grep ":${PORT} " | sed 's/^/     /'
    die "请先释放端口（或改 MCP_PORT）"
fi

if [ "$INSTALL" = "1" ]; then
    say "1. 安装 MCP SDK 到宿主 venv"
    # ⚠️ 依赖版本钉在 requirements-mcp.txt（mcp>=2.0,<3.0）：
    #    2.x 对**未预料异常**统一脱敏为 `Error executing tool <name>`，
    #    不锁版本会导致同类故障继续以同样方式静默。
    #
    # ⚠️ 这个 venv 是 **uv 创建**的（pyvenv.cfg 里有 `uv = ...`），
    #    **没有 pip**（实测 `No module named pip`，`bin/` 下也没有 pip）。
    #    故必须走 uv；`python -m ensurepip` 也能补 pip，但会把 uv 管理
    #    的依赖树搞乱，这里不采用。
    if [ -x "$VENV/bin/pip" ]; then
        "$VENV/bin/pip" install -q -r "$REQ" || die "pip 安装失败"
        echo "  方式：venv 自带 pip"
    else
        UV=""
        for c in "$HOME/.local/bin/uv" /usr/local/bin/uv "$(command -v uv 2>/dev/null)"; do
            [ -n "$c" ] && [ -x "$c" ] && UV="$c" && break
        done
        [ -n "$UV" ] || die "venv 无 pip，且找不到 uv —— 无法安装依赖"
        # VIRTUAL_ENV 让 uv 装进这个 venv（而不是另建一个）
        VIRTUAL_ENV="$VENV" "$UV" pip install -q -r "$REQ" || die "uv pip 安装失败"
        echo "  方式：uv pip（venv 由 uv 创建，无 pip）"
    fi
fi
"$PY" -c "import mcp, sys; print('  ✅ mcp SDK', getattr(mcp,'__version__','?'))" \
    || die "venv 内无法 import mcp"
# import 得到不等于**能用**：mcp_server 依赖的 ASGI 栈是另一组包
"$PY" -c "import anyio, starlette, uvicorn; print('  ✅ ASGI 栈齐备')" \
    || die "缺少 anyio/starlette/uvicorn —— --http 传输起不来"

say "2. 安装 systemd 单元"
sudo install -m 644 "$UNIT_SRC" "$UNIT_DST" || die "安装单元失败"
sudo systemctl daemon-reload || die "daemon-reload 失败"
echo "  ✅ $UNIT_DST"

say "3. 启动服务"
sudo systemctl enable --now kol-platform-mcp >/dev/null 2>&1 || true
sudo systemctl restart kol-platform-mcp || true
# 等端口就绪（最多 20s）—— 用「端口在听」而不是固定 sleep，
# 避免机器慢时误判失败、机器快时白等
for i in $(seq 1 20); do
    ss -lntp 2>/dev/null | grep -q ":${PORT} " && break
    sleep 1
done
if ! ss -lntp 2>/dev/null | grep -q ":${PORT} "; then
    echo "--- 服务状态 ---"; sudo systemctl status kol-platform-mcp --no-pager -l | tail -20
    echo "--- 最近日志 ---"; sudo journalctl -u kol-platform-mcp -n 40 --no-pager
    die "MCP 未监听 $PORT"
fi
echo "  ✅ 已监听 127.0.0.1:$PORT"

say "3b. 启动自检输出（一眼看出缺什么）"
sudo journalctl -u kol-platform-mcp -n 60 --no-pager 2>/dev/null | grep -E "\[mcp\]" | sed 's/^/  /' || true

say "4. ★ 真实调用验证（关键步骤 —— 只测握手曾漏掉全量故障）"
"$PY" - "$PORT" <<'PY' || die "MCP 工具真实调用未通过 —— 请勿认为部署成功"
import json, sys, urllib.request

port = sys.argv[1]
URL = "http://127.0.0.1:%s/mcp" % port
H = {"Content-Type": "application/json",
     "Accept": "application/json, text/event-stream"}

def post(body, sid=""):
    h = dict(H)
    if sid:
        h["mcp-session-id"] = sid
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        new = r.headers.get("mcp-session-id") or sid
        raw = r.read().decode("utf-8", "replace")
    if "data: " in raw:
        raw = raw.split("data: ", 1)[-1].strip()
    return json.loads(raw), new

_, sid = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                          "clientInfo": {"name": "deploy-host", "version": "1"}}})
tools = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
              "params": {}}, sid)[0]["result"]["tools"]
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

say "5. 崩溃自愈确认（重启策略真的生效吗）"
# ⚠️ 只声明 Restart=always 不够 —— 要实测「杀掉会被拉起来」，
#    否则「自愈」只是配置文件上的一行字。
_old_pid=$(systemctl show -p MainPID --value kol-platform-mcp 2>/dev/null || echo 0)
if [ "$_old_pid" != "0" ]; then
    sudo kill -9 "$_old_pid" 2>/dev/null || true
    sleep 6
    _new_pid=$(systemctl show -p MainPID --value kol-platform-mcp 2>/dev/null || echo 0)
    if [ "$_new_pid" != "0" ] && [ "$_new_pid" != "$_old_pid" ]; then
        echo "  ✅ 进程被杀后已自动拉起（$_old_pid → $_new_pid）"
    else
        warn "未能确认自愈（旧 PID=$_old_pid 新 PID=$_new_pid）—— 请查 journalctl"
    fi
else
    warn "拿不到 MainPID，跳过自愈验证"
fi

say "完成"
echo "  ✅ mcp SDK 已装（宿主 venv，版本区间 mcp>=2.0,<3.0）"
echo "  ✅ systemd 单元已装：$UNIT_DST"
echo "  ✅ 服务在跑：127.0.0.1:$PORT（仅本机）"
echo "  ✅ 工具真实调用全部通过"
echo
echo "  常用命令："
echo "   · 状态：sudo systemctl status kol-platform-mcp"
echo "   · 日志：sudo journalctl -u kol-platform-mcp -f"
echo "   · 验证：curl -s http://127.0.0.1:${PORT}/mcp   （需 MCP 协议头，见脚本第 4 步）"
echo
echo "  ⚠️ 当前**只监听本机**。若要让公网/跨机 Agent 调用，需在 Nginx 增加"
echo "     location /mcp 反代到 8021，并评估鉴权（PLATFORM_MCP_AUTH_MODE）——"
echo "     这是暴露面变更，请单独决策，本脚本不擅自开公网。"

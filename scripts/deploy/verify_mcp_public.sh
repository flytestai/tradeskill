#!/usr/bin/env bash
# ============================================================================
# MCP 公网端点验收脚本（幂等、只读、可反复执行）
#
# 为什么需要它
# ---------------------------------------------------------------------------
# 这条链路上「部署成功」与「真的能用」之间有 4 层独立的坑，每一层都能让
# 端点**看起来正常却实际不可用**，且症状各不相同：
#
#   ① 端口在听、本机 curl 200 —— 但 Nginx 没有 /mcp 路由
#        → 公网 404（落到 REST 上）
#   ② 路由配了 —— 但 MCP SDK 的 DNS-rebinding 保护只白名单 localhost
#        → 公网 421 Invalid Host header
#   ③ 路由通了 —— 但鉴权模式与客户端不匹配
#        → enforce 下无 Key 客户端 401
#   ④ 全都通了 —— 但 access_log 写在了「只 return 404」的 server 块里
#        → 日志文件恒为 0 字节，出事后无从追查
#
# 只测其中一层都会漏。故本脚本**逐层断言**，并把「真实调用工具」作为终点
# （历史教训：只做 initialize 握手曾漏掉「18 个工具全挂」的故障）。
#
# 用法（在服务器上执行）：
#   bash scripts/deploy/verify_mcp_public.sh
#   MCP_API_KEY=xxx bash scripts/deploy/verify_mcp_public.sh   # 附带验 Key
# ============================================================================
set -uo pipefail

HOST="${MCP_HOST:-skill.flytest.com.cn}"
PORT="${MCP_PORT:-8021}"
ENV_FILE="${MCP_ENV_FILE:-/opt/kol-skills-platform/.env}"
PY="${PY:-/opt/kol-skills-platform/.venv-host/bin/python}"

# API Key 的取用顺序（2026-09-20 补：enforce 模式上线后，本脚本必须自带 Key）
#   ① 显式 MCP_API_KEY 环境变量（最高优先级，便于测"带正确 Key / 带错误 Key"）
#   ② 从 .env 的 PLATFORM_API_KEYS 自动读取（取第一个）
#      ⚠️ 这是**必须的**：切到 enforce 后，本脚本作为每日 cron
#         （mcp-public-check）若不带 Key，会每天报「公网 initialize 失败」——
#         一个永远红的定时任务等于没有监控。
#   ③ 都取不到 → 只做「可达性」验证，并把 401 解释成「预期行为而非故障」
KEY="${MCP_API_KEY:-}"
if [ -z "$KEY" ] && [ -f "$ENV_FILE" ]; then
    KEY="$(grep -E '^PLATFORM_API_KEYS=' "$ENV_FILE" 2>/dev/null \
           | head -1 | cut -d= -f2- | tr -d '"' | cut -d, -f1 | tr -d '[:space:]')"
    [ -n "$KEY" ] && echo "  （已从 $ENV_FILE 自动读取 API Key）"
fi

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  ✅ %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  ❌ %s\n' "$*"; }

echo "== MCP 公网端点验收（$HOST）=="

# ---- 层 1：上游进程在听 -----------------------------------------------------
if ss -lntp 2>/dev/null | grep -q "127.0.0.1:${PORT} "; then
    ok "上游 MCP 监听 127.0.0.1:${PORT}"
else
    bad "上游 MCP 未监听 ${PORT}（systemctl status kol-platform-mcp）"
    echo "  后续检查已无意义，中止"; exit 1
fi

# ---- 层 2：Nginx 路由存在且指向正确上游 -------------------------------------
CONF=/etc/nginx/sites-available/skill-platform
if grep -q "location \^~ /mcp" "$CONF" 2>/dev/null || grep -q "location .*/mcp" "$CONF" 2>/dev/null; then
    ok "Nginx 有 /mcp location"
else
    bad "Nginx 无 /mcp location（公网 /mcp 会落到 REST → 404）"
fi
if grep -q "proxy_pass http://127.0.0.1:${PORT}/mcp" "$CONF" 2>/dev/null; then
    ok "反代指向 127.0.0.1:${PORT}/mcp"
else
    bad "反代未指向 127.0.0.1:${PORT}/mcp"
fi
# XFF 必须「覆盖」而非「追加」，否则可被伪造绕过内网判定
if grep -qE 'X-Forwarded-For\s+\$remote_addr' "$CONF" 2>/dev/null; then
    ok "X-Forwarded-For 为覆盖式设置（防伪造）"
else
    bad "X-Forwarded-For 不是覆盖式（可能存在伪造绕过）"
fi
# SSE 必需
if grep -q "proxy_buffering off" "$CONF" 2>/dev/null; then
    ok "proxy_buffering off（SSE 流式必需）"
else
    bad "缺少 proxy_buffering off —— MCP 流式响应会被挂起"
fi

# ---- 层 3：Host 白名单（防 421）---------------------------------------------
# ⚠️ 必须区分 401 与 421：
#    切到 enforce 后，不带 Key 的 initialize 会返回 401 —— 那是**预期行为**，
#    不是「Host 白名单缺域名」。早先这里把两者混为一谈，统一报
#    「常见原因：421」，会把人往完全错误的方向引。
#    故返回 0=200 / 2=401 / 3=421 / 1=其他。
"$PY" - "$HOST" "$KEY" <<'PY'
import sys, json, urllib.request, urllib.error, ssl
host, key = sys.argv[1], sys.argv[2]
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
body = {"jsonrpc":"2.0","id":1,"method":"initialize","params":{
    "protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"v","version":"1"}}}
h = {"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
if key: h["X-API-Key"] = key
req = urllib.request.Request("https://%s/mcp" % host, data=json.dumps(body).encode(),
                             headers=h, method="POST")
try:
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        sys.exit(0 if r.status == 200 else 1)
except urllib.error.HTTPError as e:
    if e.code == 401: sys.exit(2)
    if e.code == 421: sys.exit(3)
    sys.exit(1)
PY
_rc=$?
case "$_rc" in
  0) ok "公网 initialize 通过（Host 白名单已含该域名，无 421）" ;;
  2) if [ -n "$KEY" ]; then
         bad "带 Key 仍返回 401 —— Key 可能已失效或 .env 里已被更换"
     else
         bad "公网 initialize 返回 401（enforce 模式且本脚本未取到 Key）"
     fi ;;
  3) bad "公网 initialize 返回 421 —— Host 白名单未含该域名（改 api/mcp_server.py 白名单）" ;;
  *) bad "公网 initialize 失败（非 401/421，见 'nginx -t' 与 journalctl -u kol-platform-mcp）" ;;
esac

# ---- 层 4：真实工具调用（终点断言）-----------------------------------------
"$PY" - "$HOST" "$KEY" <<'PY'
import sys, json, ssl, urllib.request, urllib.error
host, key = sys.argv[1], sys.argv[2]
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
URL = "https://%s/mcp" % host
H = {"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
def post(b, sid=""):
    h = dict(H)
    if sid: h["mcp-session-id"] = sid
    if key: h["X-API-Key"] = key
    req = urllib.request.Request(URL, data=json.dumps(b).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40, context=ctx) as r:
            s = r.headers.get("mcp-session-id") or sid
            raw = r.read().decode("utf-8","replace"); code = r.status
    except urllib.error.HTTPError as e:
        return None, sid, e.code
    if "data: " in raw: raw = raw.split("data: ",1)[-1].strip()
    try:    return json.loads(raw), s, code
    except Exception: return None, s, code
INIT = {"jsonrpc":"2.0","id":1,"method":"initialize","params":{
    "protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"1"}}}
_, sid, code = post(INIT)
if code == 401:
    print("  ⚠️  initialize 返回 401 —— 当前是 enforce 模式且未提供有效 Key")
    print("      （若确实是预期行为，请带 MCP_API_KEY=... 重跑本脚本）")
    raise SystemExit(1)
if code != 200:
    print("  ❌ initialize HTTP %s" % code); raise SystemExit(1)
r, sid, _ = post({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}, sid)
names = [t["name"] for t in (r or {}).get("result",{}).get("tools",[])]
if not names:
    print("  ❌ tools/list 未返回工具"); raise SystemExit(1)
print("  ✅ tools/list 返回 %d 个工具" % len(names))
r, sid, _ = post({"jsonrpc":"2.0","id":3,"method":"tools/call",
                  "params":{"name":"kol_list","arguments":{}}}, sid)
res = (r or {}).get("result",{})
txt = "".join(c.get("text","") for c in res.get("content",[]))
if res.get("isError") or not txt:
    print("  ❌ 真实调用 kol_list 失败: %s" % txt[:120]); raise SystemExit(1)
print("  ✅ 真实调用 kol_list → %s" % txt[:70].replace("\n"," "))
PY
if [ $? -eq 0 ]; then ok "真实工具调用通过（4/4 层）"; else bad "真实工具调用未通过"; fi

# ---- 层 5：未知 Host 应被拒（REST 与 MCP 都该拒）---------------------------
# ⚠️ 为什么要查 REST：原先主 server 的 server_name 以 `_` 结尾（兜底名），
#    任何域名只要解析到本机 IP 都会被接进主 server —— 实测未知 Host 返回 200，
#    而 MCP 端点是 421。两者姿态不一致，REST 侧少一层纵深防御（2026-09-20 已修：
#    主块去掉 `_`，另加 default_server 返回 444）。
#    这里同时断言两者，防止将来有人把 `_` 加回去。
# ⚠️ 取状态码的写法很讲究（我在这里连踩两坑）：
#    · `code=$(curl -w '%{http_code}' ... || echo 000)` 在连接被 444 断开时，
#      curl **已经打印了 000**，`|| echo 000` 再补一次 → 得到 "000000"。
#    · 不要用 `$(函数(){...}; 函数 ...)` 这种形态，某些 shell 下会解析成
#      「命令 `_:` 未找到」。
#    正解：先赋值、判空，不叠 `||` 的补输出。
_c_unknown=$(curl -s -o /dev/null -w '%{http_code}' \
    --max-time 15 -k \
    --resolve "nonexistent.invalid:443:127.0.0.1" \
    "https://nonexistent.invalid/healthz" 2>/dev/null)
[ -z "$_c_unknown" ] && _c_unknown="000"
if [ "$_c_unknown" = "000" ]; then
    ok "未知 Host 被拒（REST 侧 default_server 444 生效）"
else
    bad "未知 Host 未被拒（实得 $_c_unknown，期望 000/断开）—— 检查 nginx 主 server 的 server_name 是否又带了兜底 _"
fi

# ---- 附：日志是否真的在写（第 ④ 类坑）--------------------------------------
LOG=/var/log/nginx/skill-platform.access.log
if [ -f "$LOG" ]; then
    n=$(wc -l < "$LOG" 2>/dev/null || echo 0)
    if [ "$n" -gt 0 ]; then
        ok "站点访问日志有内容（$n 行）"
    else
        bad "站点访问日志为 0 行 —— 指令可能写在了不处理请求的 server 块里"
    fi
else
    bad "无站点访问日志文件（该站点请求会混进全局日志，难追查）"
fi

echo
echo "通过 $PASS 项，失败 $FAIL 项"
[ "$FAIL" -eq 0 ] && echo "🟢 MCP 公网链路正常" || { echo "🔴 存在问题，见上"; exit 1; }

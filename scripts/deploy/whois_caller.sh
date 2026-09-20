#!/usr/bin/env bash
# ============================================================================
# 「这个来源 IP 是不是我自己？」—— 用唯一标记流量反查日志
#
# ⚠️ 为什么需要这个工具（一次真实的误判，2026-09-20）
# ----------------------------------------------------------------------------
# 我在服务器日志里看到来源 `198.51.100.11` 反复调用 MCP 端点，
# 就断言「这是一台外部设备，请你确认它是谁」—— **错了，那就是我自己**。
#
# 我的判断方法：`curl https://api.ipify.org` 得到本机公网 IP 是 `198.51.100.10`，
# 与日志里的 IP 不一致 → 推论「不是我」。
#
# 错在哪：**运营商出口 IP 是一个池**，不同连接会随机落到不同地址。
# 实测同一台机器：
#     api.ipify.org  -> 198.51.100.10
#     ipinfo.io      -> 198.51.100.11
#     ifconfig.me    -> 198.51.100.10
#   而我拿其中**一次**的结果当了定论。
#
# 正确方法（本脚本实现的）：**主动制造一次带唯一标记的流量，再去日志里找它**。
# 标记放在 User-Agent 与一个自定义头里（双保险：有的日志/中间件只记 UA）。
# 若日志里该标记出现在 IP X 下 → X 就是我（或我这个出口）。
#
# 用法（在**服务器**上执行，因为它要读服务器日志）：
#   bash scripts/deploy/whois_caller.sh              # 默认探测 MCP 端点
#   bash scripts/deploy/whois_caller.sh /healthz     # 探测其他路径
#
# 输出示例：
#   标记: kol-probe-7f3a9c-20260920T125500
#   本机出口 IP（多源交叉）: 198.51.100.10 / 198.51.100.11
#   日志中该标记出现于: 198.51.100.11   ← 结论：这是**我自己**
# ============================================================================
set -uo pipefail

PATH_TO_HIT="${1:-/mcp}"
HOST="${PROBE_HOST:-skill.flytest.com.cn}"
UA="kol-probe-$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')-$(date +%Y%m%dT%H%M%S)"
XTAG="kol-probe-$(date +%s)-$$"

LOG_SITE=/var/log/nginx/skill-platform.access.log
LOG_GLOBAL=/var/log/nginx/access.log

echo "== 出口 IP 溯源探针 =="
echo "  标记(UA)     : $UA"
echo "  标记(自定义头): $XTAG"
echo "  目标          : https://$HOST$PATH_TO_HIT"

# ---- 1) 多源交叉查「本机出口 IP」——重点在于展示它们**可能不一致** ----
echo
echo "-- 本机出口 IP（多源交叉；不一致是正常的，运营商出口是 IP 池）--"
for u in https://api.ipify.org https://ipinfo.io/ip https://ifconfig.me/ip https://icanhazip.com; do
    ip=$(curl -s --max-time 10 "$u" 2>/dev/null | tr -d '[:space:]')
    printf '   %-26s -> %s\n' "$u" "${ip:-(取不到)}"
done

# ---- 2) 发一次带唯一标记的请求 ----
echo
echo "-- 发出带标记的请求 --"
if [ "$PATH_TO_HIT" = "/mcp" ]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -k \
        -X POST "https://$HOST$PATH_TO_HIT" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -H "User-Agent: $UA" \
        -H "X-Probe-Tag: $XTAG" \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' 2>/dev/null)
else
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -k \
        -H "User-Agent: $UA" -H "X-Probe-Tag: $XTAG" \
        "https://$HOST$PATH_TO_HIT" 2>/dev/null)
fi
[ -z "$code" ] && code="000(连接被断开)"
echo "   HTTP $code"
sleep 3   # 等日志落盘

# ---- 3) 回查日志：标记落在哪个 IP 下 ----
echo
echo "-- 日志中该标记出现的来源 IP --"
found=0
for f in "$LOG_SITE" "$LOG_GLOBAL"; do
    [ -f "$f" ] || continue
    # 同时按 UA 和自定义头找（自定义头一般不在 access_log 里，UA 一定会记）
    hits=$(grep -F "$UA" "$f" 2>/dev/null | awk '{print $1}' | sort -u)
    if [ -n "$hits" ]; then
        found=1
        echo "   文件: $f"
        for ip in $hits; do echo "     ✅ 来源 IP = $ip"; done
    fi
done

if [ "$found" = "0" ]; then
    echo "   ⚠️ 日志里没找到这个标记。可能原因："
    echo "      · 请求没到达（路径/端口不对，或用例被 default_server 444 拒了）"
    echo "      · 该站点的 access_log 未启用，且请求落在别的日志文件里"
    echo "      建议: sudo grep -rF '$UA' /var/log/nginx/ 2>/dev/null"
fi

echo
echo "== 结论 =="
echo "  上面列出的 IP = **本机出口 IP 之一**（可能不止一个）。"
echo "  ⚠️ 因此：在日志里看到陌生 IP 时，**不要**只凭「与 ipify 查到的不一致」"
echo "     就断定是外部设备 —— 先用本脚本反查一次。"

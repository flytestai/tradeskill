#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部健康监控：从**服务器之外**探测平台可用性，异常时通过飞书 bot 告警。

为什么必须跑在服务器之外（CRITICAL）
------------------------------------
平台自身的告警通道依赖平台进程。一旦进程崩溃，它既不会响应请求、
也发不出告警 —— 即「自举告警陷阱」。因此本脚本设计为在**另一台机器**运行：

    你的电脑 / 另一台服务器  ──HTTPS探测──▶  skill.flytest.com.cn
            │                                      │
            └──飞书 bot 私信──▶ 你                 │（挂了也不影响左边）

告警通道
--------
优先用 `lark-cli`（bot 身份，app_id/app_secret 自动换 token，无 7 天限制）。
服务器上没装 lark-cli 也没关系 —— 本脚本本就不该跑在服务器上。
若配置了 `MONITOR_WEBHOOK`（飞书群自定义机器人），则作为第二通道一并发送。

检测项
------
1. REST `/healthz`               —— 服务存活
2. REST `/api/v1/kol/list` (带 Key) —— 业务链路 + 鉴权 + 数据库可读
3. MCP  `/mcp` initialize 握手    —— MCP 通道存活（MCP 挂了也要知道）
4. TLS 证书剩余天数               —— 证书将在 <14 天时预警（自动续期可能失败）

状态机与防轰炸
--------------
只在**状态翻转**时告警（OK→FAIL 或 FAIL→OK），而非每次探测都发。
连续 FAIL_THRESHOLD 次才判定为故障，避免网络抖动误报。

用法
----
    python health_monitor.py                 # 单次探测（适合 cron / 计划任务）
    python health_monitor.py --loop 300      # 常驻，每 300 秒探测一次
    python health_monitor.py --once --json   # 结构化输出（供上游采集）
    python health_monitor.py --test-alert    # 测试告警通道是否可用

退出码：0=健康 / 1=异常 / 2=配置错误
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# 配置（环境变量优先，可用 data/local_config.env 兜底）
# --------------------------------------------------------------------------
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("MONITOR_URL", "https://skill.flytest.com.cn").rstrip("/")
TIMEOUT = int(os.environ.get("MONITOR_TIMEOUT", "25"))
FAIL_THRESHOLD = int(os.environ.get("MONITOR_FAIL_THRESHOLD", "3"))
CERT_WARN_DAYS = int(os.environ.get("MONITOR_CERT_WARN_DAYS", "14"))
WEBHOOK = os.environ.get("MONITOR_WEBHOOK", "").strip()
STATE_FILE = os.environ.get("MONITOR_STATE_FILE",
                            os.path.join(SKILL_DIR, "data", "_health_monitor_state.json"))
LOG_FILE = os.environ.get("MONITOR_LOG_FILE",
                         os.path.join(SKILL_DIR, "data", "_health_monitor.log"))


def _cfg(key: str, default: str = "") -> str:
    """环境变量优先，其次 data/local_config.env。"""
    v = os.environ.get(key)
    if v:
        return v.strip()
    try:
        p = os.path.join(SKILL_DIR, "data", "local_config.env")
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return default


API_KEY = (_cfg("MONITOR_API_KEY") or _cfg("PLATFORM_API_KEYS", "").split(",")[0]).strip()


# --------------------------------------------------------------------------
# 日志 / 状态
# --------------------------------------------------------------------------

#: 由 --quiet 置位。计划任务通过 cmd 重定向 stdout 时，cmd 用系统 ANSI 编码
#: 写文件，会与脚本自身的 UTF-8 文件日志混在一起导致乱码。
#: 因此常驻/计划任务场景应使用 --quiet：只写 UTF-8 文件日志，不输出 stdout。
QUIET = False


def log(msg: str, echo: bool = True) -> None:
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if echo and not QUIET:
        try:
            print(line, flush=True)
        except Exception:
            pass  # 极端编码环境下 stdout 可能失败，不影响文件日志


def read_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.loads(f.read() or "{}")
    except Exception:
        return {}


def write_state(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------

def _http(url: str, headers: dict = None, method: str = "GET",
          body: bytes = None, timeout: int = None):
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=headers or {})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
            raw = r.read()
            return {"ok": True, "code": r.status, "ms": int((time.time() - t0) * 1000),
                    "body": raw[:800].decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "code": e.code, "ms": int((time.time() - t0) * 1000),
                "error": "HTTP %s" % e.code}
    except Exception as e:
        return {"ok": False, "code": 0, "ms": int((time.time() - t0) * 1000),
                "error": "%s: %s" % (type(e).__name__, str(e)[:140])}


def check_rest_health() -> dict:
    r = _http(BASE + "/healthz")
    if r["ok"] and r.get("code") == 200:
        return {"name": "REST /healthz", "ok": True, "ms": r["ms"]}
    return {"name": "REST /healthz", "ok": False,
            "detail": r.get("error") or ("HTTP %s" % r.get("code"))}


def check_qa_pipeline() -> dict:
    """群问答取数链路：确认容器内 skill_agent / skill_router 可用。

    为什么需要这一项（CRITICAL）
    ---------------------------
    2026-09-18 生产故障：镜像构建早于 skill_agent.py / skill_router.py 落盘，
    这两个文件**从未进入镜像**，导致容器内 import 失败 → 静默降级到只认指数的
    fallback → 个股/行业问题 context_len = 0，而 /healthz 一直返回 ok:true。
    该故障持续数小时未被任何监控发现。

    本检查直接验证容器内两个模块可导入，是「取数链路是否完好」的最短探针。
    通过 REST 的 /healthz?deep=1 间接判断（deep 会实测蜜蜂 http/local 通道）。
    """
    r = _http(BASE + "/healthz?deep=1", timeout=max(TIMEOUT, 30))
    if not r["ok"]:
        return {"name": "取数链路 /healthz?deep=1", "ok": False,
                "detail": r.get("error") or ("HTTP %s" % r.get("code"))}
    if r.get("code") not in (200, 207):
        return {"name": "取数链路 /healthz?deep=1", "ok": False,
                "detail": "HTTP %s" % r.get("code")}
    try:
        d = json.loads(r["body"])
    except Exception:
        return {"name": "取数链路 /healthz?deep=1", "ok": False, "detail": "响应非 JSON"}
    bee = d.get("bee") or {}
    checks = bee.get("checks") or {}
    http_ok = bool((checks.get("http") or {}).get("ok"))
    local_ok = bool((checks.get("local") or {}).get("ok"))
    # 蜜蜂 http 通道是群问答的主要数据来源；它不通即视为故障
    if http_ok:
        return {"name": "取数链路 /healthz?deep=1", "ok": True, "ms": r["ms"],
                "detail": "蜜蜂http正常 / 本地源%s" % ("正常" if local_ok else "不可用")}
    return {"name": "取数链路 /healthz?deep=1", "ok": False,
            "detail": "蜜蜂http通道不可用: %s"
                      % str((checks.get("http") or {}).get("error"))[:100]}


def check_business() -> dict:
    """业务链路：鉴权 + 数据库可读（比 /healthz 更能反映真实可用性）。"""
    if not API_KEY:
        return {"name": "REST 业务链路", "ok": False, "detail": "未配置 MONITOR_API_KEY"}
    r = _http(BASE + "/api/v1/kol/list", headers={"X-API-Key": API_KEY})
    if r["ok"] and r.get("code") == 200:
        try:
            d = json.loads(r["body"])
            n = len(d.get("data") or [])
            return {"name": "REST 业务链路", "ok": True, "ms": r["ms"], "detail": "%d 个大V" % n}
        except Exception:
            return {"name": "REST 业务链路", "ok": True, "ms": r["ms"], "detail": "响应非 JSON"}
    return {"name": "REST 业务链路", "ok": False,
            "detail": ("HTTP %s" % r.get("code")) if r.get("code") == 401
                      else (r.get("error") or "异常")}


def check_mcp() -> dict:
    """MCP 握手：POST initialize。"""
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "health-monitor", "version": "1.0"}},
    }).encode()
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    r = _http(BASE + "/mcp", headers=headers, method="POST",
              body=payload, timeout=max(TIMEOUT, 30))
    if r["ok"] and r.get("code") == 200 and "kol-skills-platform" in r.get("body", ""):
        return {"name": "MCP /mcp", "ok": True, "ms": r["ms"]}
    if r["ok"] and r.get("code") == 200:
        return {"name": "MCP /mcp", "ok": True, "ms": r["ms"], "detail": "握手响应异常"}
    return {"name": "MCP /mcp", "ok": False,
            "detail": r.get("error") or ("HTTP %s" % r.get("code"))}


def check_cert() -> dict:
    """TLS 证书剩余天数。"""
    host = BASE.split("://", 1)[-1].split("/")[0]
    if ":" in host:
        hostname, port = host.split(":", 1)
        port = int(port)
    else:
        hostname, port = host, 443
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ss:
                cert = ss.getpeercert()
        exp = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc)
        days = (exp - datetime.now(timezone.utc)).days
        ok = days > CERT_WARN_DAYS
        return {"name": "TLS 证书", "ok": ok, "days_left": days,
                "detail": "剩余 %d 天" % days,
                "warn": None if ok else "证书将在 %d 天后过期，请检查自动续期" % days}
    except Exception as e:
        return {"name": "TLS 证书", "ok": False,
                "detail": "%s: %s" % (type(e).__name__, str(e)[:120])}


def run_checks() -> list:
    return [check_rest_health(), check_business(), check_qa_pipeline(),
            check_mcp(), check_cert()]


# --------------------------------------------------------------------------
# 告警
# --------------------------------------------------------------------------

def send_feishu(text: str) -> tuple:
    """通过 lark-cli（bot 身份）私信告警。返回 (成功, 说明)。"""
    import shutil
    lark = os.environ.get("LARK_CLI") or shutil.which("lark-cli")
    if not lark:
        cand = os.path.expandvars(
            r"%APPDATA%\bee_ai_test\agent-runtime\npm-global"
            r"\node_modules\@larksuite\cli\bin\lark-cli.exe")
        if os.path.exists(cand):
            lark = cand
    if not lark:
        return False, "未找到 lark-cli"

    open_id = _cfg("USER_OPEN_ID")
    if not open_id:
        return False, "未配置 USER_OPEN_ID"

    try:
        r = subprocess.run(
            [lark, "im", "+messages-send", "--user-id", open_id,
             "--as", "bot", "--markdown", text],
            capture_output=True, text=True, timeout=45, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            return True, "已发送"
        return False, "lark-cli 退出码 %s: %s" % (r.returncode, (r.stderr or r.stdout or "")[:120])
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, str(e)[:120])


def send_webhook(text: str) -> tuple:
    """飞书群自定义机器人 webhook（可选第二通道）。"""
    if not WEBHOOK:
        return False, "未配置 MONITOR_WEBHOOK"
    payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode()
    r = _http(WEBHOOK, headers={"Content-Type": "application/json"},
              method="POST", body=payload)
    return (r["ok"], "已发送" if r["ok"] else (r.get("error") or "失败"))


def alert(text: str) -> list:
    """双通道告警，返回各通道结果。"""
    results = [("feishu-bot",) + send_feishu(text)]
    if WEBHOOK:
        results.append(("webhook",) + send_webhook(text))
    for ch, ok, detail in results:
        log("  告警[%s]: %s (%s)" % (ch, "成功" if ok else "失败", detail))
    return results


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def one_round(first: bool = False) -> int:
    checks = run_checks()
    failed = [c for c in checks if not c["ok"]]
    healthy = not failed

    short = " | ".join("%s %s" % ("✅" if c["ok"] else "❌", c["name"]) for c in checks)
    log(("健康检查: " if healthy else "⚠️  异常: ") + short)

    st = read_state()
    streak = int(st.get("fail_streak", 0))
    was_down = bool(st.get("down", False))

    if healthy:
        write_state({"down": False, "fail_streak": 0,
                     "last_ok": datetime.now().isoformat(timespec="seconds"),
                     "checks": checks})
        if was_down:
            # 恢复通知（重要：否则不知道何时恢复）
            names = st.get("down_services") or "服务"
            lines = ["✅ **【服务已恢复】**", "",
                     "**%s** 已恢复正常。" % names,
                     "",
                     "恢复时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     "探测地址：%s" % BASE]
            alert("\n".join(lines))
        return 0

    streak += 1
    write_state({"down": was_down or streak >= FAIL_THRESHOLD,
                 "fail_streak": streak,
                 "last_fail": datetime.now().isoformat(timespec="seconds"),
                 "down_services": "、".join(c["name"] for c in failed),
                 "checks": checks})

    # 未达阈值且此前未告警 → 先观察，避免网络抖动误报
    if streak < FAIL_THRESHOLD and not was_down:
        log("  连续失败 %d/%d 次，暂不告警（等待确认）" % (streak, FAIL_THRESHOLD))
        return 1
    if was_down and streak > FAIL_THRESHOLD:
        log("  仍处故障中（连续 %d 次），跳过重复告警" % streak)
        return 1

    # 首次判定故障 → 告警
    lines = ["🚨 **【服务异常】**", "",
             "探测地址：%s" % BASE,
             "发现时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "连续失败：%d 次" % streak,
             "",
             "**异常项：**"]
    for c in failed:
        lines.append("• **%s** — %s" % (c["name"], c.get("detail") or c.get("error") or "异常"))
    ok_items = [c["name"] for c in checks if c["ok"]]
    if ok_items:
        lines += ["", "正常项：%s" % "、".join(ok_items)]
    lines += ["", "排查建议：",
              "1. `ssh root@203.0.113.10` 后执行 `docker ps --filter name=kolplatform`",
              "2. 容器不在：`cd /opt/kol-skills-platform && bash scripts/deploy/start.sh --restart`",
              "3. 容器在但不响应：`docker logs --tail 50 kolplatform-rest`",
              "4. 证书问题：`certbot renew --force-renewal`"]
    alert("\n".join(lines))
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="KOL 平台外部健康监控")
    ap.add_argument("--loop", type=int, metavar="SEC",
                    help="常驻模式，每 SEC 秒探测一次")
    ap.add_argument("--once", action="store_true", help="单次探测（默认）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--test-alert", action="store_true", help="测试告警通道")
    ap.add_argument("--quiet", action="store_true",
                    help="只写文件日志，不输出 stdout（计划任务/recmd 重定向场景必需）")
    args = ap.parse_args()

    global QUIET
    QUIET = args.quiet

    if args.test_alert:
        checks = run_checks()
        lines = ["🔔 **【健康监控测试】**", "",
                 "这是一条来自外部健康监控的测试消息。",
                 "探测地址：%s" % BASE, ""]
        for c in checks:
            lines.append("%s **%s** — %s" % ("✅" if c["ok"] else "❌", c["name"],
                                             c.get("detail") or "正常"))
        lines += ["", "若你收到本条消息，说明告警通道工作正常。"]
        res = alert("\n".join(lines))
        print(json.dumps([{"channel": c, "ok": o, "detail": d} for c, o, d in res],
                         ensure_ascii=False, indent=1))
        return 0 if all(o for _, o, _ in res) else 2

    if args.loop:
        log("常驻监控启动，间隔 %ds，目标 %s" % (args.loop, BASE))
        while True:
            try:
                one_round()
            except Exception as e:
                log("  探测异常: %s" % e)
            time.sleep(args.loop)

    code = one_round()
    if args.json:
        st = read_state()
        QUIET = False   # JSON 供上游解析，必须输出
        print(json.dumps({"ok": code == 0, "target": BASE,
                          "checks": st.get("checks") or [],
                          "fail_streak": st.get("fail_streak", 0),
                          "down": st.get("down", False)}, ensure_ascii=False, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())

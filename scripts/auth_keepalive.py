#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书 OAuth 授权保活脚本（零 token）。

背景
----
lark-cli 的用户身份走 OAuth refresh token，`refreshExpiresAt` 是**滑动窗口**
（= 最近一次成功刷新的时间 + 7 天）。只要在窗口内至少成功调用一次用户身份 API，
窗口就会自动顺延 7 天；一旦静默超过 7 天，refresh token 失效，必须人工扫码重新授权。

用真实数据验证过（2026-09-18）：
  - 首次授权 2026-08-26 15:46 → 若固定 7 天，9/2 就该失效；
  - 实际日志显示 9/11、9/14~9/18 每天 09:00/10:5x/12:5x/14:4x 各有一次
    `path=/open-apis/authen/v2/oauth/token` 调用，refreshExpiresAt 始终是
    最近一次调用 + 7 天 → 确认滑动窗口。

为什么需要本脚本
----------------
盘中 30 秒轮的 `sync_feishu_auto.py` 受 `is_group_sync_time()` 门控
（仅交易日 9:00-16:00），**节假日与周末完全不跑**：

    节前最后交易日 2026-09-30 16:00 最后一次刷新
      → refreshExpiresAt = 2026-10-07 16:00
    节后首个交易日 2026-10-08 09:00 首次调用
      → 相差 17 小时，已过期 ❌

本脚本注册为 supervisor 的 PERIODIC 任务（与 `is_trading_day()` 无关，天天跑），
每天调用一次只读用户身份 API，把刷新窗口持续顺延。

设计要点
--------
- **先用 `auth status` 判断，仅在临近刷新时才真正调用 API**：避免周末/假期
  每天都打飞书接口，减少无谓请求与风控面。
- **失败告警走 bot 身份**（`notify_feishu.sh --as bot`），bot token 由
  app_id/app_secret 自动获取、无 7 天限制，因此即便用户 refresh token 已失效，
  告警仍能送达。
- 日志与状态落在 `data/_auth_keepalive.log` / `data/_auth_keepalive_state.txt`。

用法
----
    python scripts/auth_keepalive.py                 # 巡检 + 按需刷新
    python scripts/auth_keepalive.py --force         # 不管窗口，强制调一次
    python scripts/auth_keepalive.py --json          # 结构化输出
    python scripts/auth_keepalive.py --dry-run       # 只检查不调用
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

# 北京时间（日志时间戳与 cron 的 TZ=Asia/Shanghai 对齐）
try:
    from common import beijing_now as _bj_now
except Exception:
    def _bj_now():
        from datetime import datetime, timezone, timedelta
        return datetime.now(timezone(timedelta(hours=8)))


SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

LOG_FILE = os.path.join(SKILL_DIR, "data", "_auth_keepalive.log")
STATE_FILE = os.path.join(SKILL_DIR, "data", "_auth_keepalive_state.txt")

# 保活策略（两层判定，满足其一即刷新）：
#
#  ① 距上次成功刷新超过 KEEPALIVE_EVERY_HOURS 小时 → 刷新。
#     这是**主判定**。实测 access token 生命周期仅约 2 小时（09:00→10:55→12:50→14:45，
#     间隔稳定为 1:55:03），只有它过期后发起调用才会真正触发 refresh，
#     把 refreshExpiresAt 顺延为「now + 7 天」。
#     设 20 小时 → 每天保活一次，且间隔必然 > 2h，确保每次都能真正刷新。
#     ⚠️ 假期（中秋 9/25、国庆 10/1-10/7）盘中轮询受交易日门控完全不跑，
#        必须靠这一条维持刷新窗口。
#
#  ② 剩余天数 <= REFRESH_WHEN_DAYS_LEFT → 刷新。
#     兜底：万一 ① 因进程长期停滞而没跑，剩余不足 3 天时立即补刷。
KEEPALIVE_EVERY_HOURS = 20
REFRESH_WHEN_DAYS_LEFT = 3

BASH = shutil.which("bash") or "bash"


def log(msg: str, echo: bool = True) -> None:
    line = "[%s] %s" % (_bj_now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if echo:
        print(line, flush=True)


def _posix_path(p: str) -> str:
    """把 Windows 路径转成 Git Bash 可识别的形式。"""
    p = str(p).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if m:
        return "/%s/%s" % (m.group(1).lower(), m.group(2))
    return p


def find_lark_cli() -> str:
    """定位 lark-cli，优先原生 exe（避免 POSIX 包装脚本的 node 子进程不退出）。"""
    exe = os.path.expandvars(
        r"%APPDATA%\bee_ai_test\agent-runtime\npm-global"
        r"\node_modules\@larksuite\cli\bin\lark-cli.exe")
    if os.path.exists(exe):
        return exe
    for c in (os.path.expandvars(r"%APPDATA%\bee_ai_test\agent-runtime\npm-global\lark-cli.cmd"),
              os.path.expandvars(r"%APPDATA%\npm\lark-cli.cmd")):
        if os.path.exists(c):
            return c
    return shutil.which("lark-cli") or "lark-cli"


def _run(cmd_list, timeout=45):
    """跑一条 lark-cli 命令，返回 (returncode, stdout)。"""
    try:
        r = subprocess.run(cmd_list, capture_output=True, text=True,
                           timeout=timeout, cwd=SKILL_DIR,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "")
    except subprocess.TimeoutExpired:
        return 124, ""
    except Exception as e:
        log("执行失败 %s: %s" % (cmd_list[:3], e))
        return 1, ""


def _parse_json(text: str):
    """lark-cli 有时在 JSON 前后混入提示行，取第一个 '{' 到最后一个 '}'。"""
    if not text:
        return None
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(text[i:j + 1])
    except Exception:
        return None


def read_status(lark_cli):
    """读取用户授权状态，返回 dict（含 refresh_expires_at / days_left）。"""
    code, out = _run([lark_cli, "auth", "status", "--json"], timeout=40)
    data = _parse_json(out)
    if not data:
        return {"ok": False, "error": "auth status 无有效输出(exit=%s)" % code}
    user = (data.get("identities") or {}).get("user") or {}
    expires = user.get("refreshExpiresAt") or user.get("expiresAt") or ""
    if not expires:
        return {"ok": False, "error": "未检测到用户授权"}
    try:
        exp = datetime.fromisoformat(expires)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone(timedelta(hours=8)))
    except Exception:
        return {"ok": False, "error": "无法解析到期时间: %s" % expires}
    now8 = datetime.now(timezone(timedelta(hours=8)))
    return {
        "ok": True,
        "token_status": user.get("tokenStatus") or "",
        "refresh_expires_at": expires,
        "expires_at": user.get("expiresAt") or "",
        "days_left": (exp - now8).total_seconds() / 86400.0,
        "open_id": user.get("openId") or "",
    }


def refresh_window(lark_cli, chat_id: str):
    """调用一次只读的用户身份 API，触发 token 刷新、把窗口顺延 7 天。"""
    if not chat_id:
        return False, "缺少 WU2198_CHAT_ID，无法触发刷新"
    cmd = [lark_cli, "im", "+chat-messages-list", "--chat-id", chat_id,
           "--as", "user", "--order", "desc", "--page-limit", "1",
           "--no-reactions", "--json"]
    code, out = _run(cmd, timeout=90)
    data = _parse_json(out)
    if data is None:
        return False, "刷新调用无有效输出(exit=%s)" % code
    if data.get("ok"):
        return True, "ok"
    err = (data.get("error") or {})
    return False, "%s/%s: %s" % (err.get("type", "?"), err.get("code", "?"),
                                 str(err.get("message", ""))[:120])


def alert(msg: str, once_key: str = ""):
    """通过飞书机器人（bot 身份）私信告警。bot token 无 7 天限制，故授权失效时仍可达。"""
    script = os.path.join(SKILL_DIR, "scripts", "notify_feishu.sh")
    if not os.path.exists(script):
        log("告警脚本缺失: %s" % script)
        return
    # 去重：同一触发键只提醒一次（alert_once_private.sh 支持状态重置）
    once = os.path.join(SKILL_DIR, "scripts", "alert_once_private.sh")
    if once_key and os.path.exists(once):
        cmd = ["bash", _posix_path(once), once_key, "below", msg]
    else:
        cmd = ["bash", _posix_path(script), msg]
    subprocess.run(cmd, cwd=SKILL_DIR, timeout=40,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


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
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def get_chat_id() -> str:
    env = os.path.join(SKILL_DIR, "data", "local_config.env")
    try:
        with open(env, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("WU2198_CHAT_ID="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="飞书 OAuth 授权保活")
    ap.add_argument("--force", action="store_true", help="不管窗口剩余多久都调用一次")
    ap.add_argument("--dry-run", action="store_true", help="只检查不调用")
    ap.add_argument("--json", action="store_true", help="结构化输出")
    args = ap.parse_args()

    lark_cli = find_lark_cli()
    st = read_status(lark_cli)
    result = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **st}

    if not st.get("ok"):
        # 读不到状态：可能是未授权，也可能是 lark-cli 异常，不动状态、下轮重试
        result["action"] = "status_failed"
        log("⚠️ 读取授权状态失败: %s" % st.get("error"))
        if "未检测到用户授权" in str(st.get("error")):
            alert("🚨 **【飞书授权失效】**\n未检测到用户授权，请执行 `lark-cli auth login` 重新扫码授权。",
                  once_key="auth-keepalive-missing")
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        return 0

    days = st["days_left"]
    result["action"] = "none"

    if days <= 0:
        result["action"] = "expired_alert"
        log("🚨 授权已过期 %.1f 天（%s）" % (-days, st["refresh_expires_at"]))
        alert("🚨 **【飞书授权已过期】**\nrefresh token 已于 %s 过期。\n"
              "请执行 `lark-cli auth login` 重新扫码授权，否则群消息同步将中断。"
              % st["refresh_expires_at"][:19], once_key="auth-keepalive-expired")
    else:
        stt = read_state()
        now_ts = time.time()
        last_ts = float(stt.get("last_refresh_ts") or 0)
        retry_after = float(stt.get("retry_after_ts") or 0)
        hours_since = (now_ts - last_ts) / 3600.0 if last_ts else 1e9
        result["hours_since_refresh"] = round(hours_since, 2) if last_ts else None

        if now_ts < retry_after:
            # 上次调用未让窗口顺延（access token 尚未过期），等待其自然过期后再试
            need, why = False, "等待 access token 过期后重试（%.1f 小时后）" % ((retry_after - now_ts) / 3600.0)
        elif args.force or hours_since >= KEEPALIVE_EVERY_HOURS or days <= REFRESH_WHEN_DAYS_LEFT:
            need, why = True, "到期保活" if days > REFRESH_WHEN_DAYS_LEFT else "剩余不足 %d 天" % REFRESH_WHEN_DAYS_LEFT
        else:
            need, why = False, "距上次刷新不足 %d 小时" % KEEPALIVE_EVERY_HOURS

        result["need"] = need
        if args.dry_run:
            result["action"] = "dry_run"
            log("dry-run: 剩余 %.1f 天 / 距上次刷新 %s → %s（%s）"
                % (days, ("%.1f 小时" % hours_since) if last_ts else "无记录",
                   "需要刷新" if need else "无需刷新", why))
        elif need:
            before_exp = st["refresh_expires_at"]
            ok, msg = refresh_window(lark_cli, get_chat_id())
            if not ok:
                result["action"] = "refresh_failed"
                result["error"] = msg
                log("⚠️ 刷新调用失败: %s（下轮重试）" % msg)
                stt["fail_streak"] = int(stt.get("fail_streak", 0)) + 1
                stt["last_check_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                write_state(stt)
                if stt["fail_streak"] >= 3:
                    alert("🚨 **【飞书授权刷新连续失败 %d 次】**\n最后错误：%s\n"
                          "授权将于 %s 过期，请尽快检查或重新执行 `lark-cli auth login`。"
                          % (stt["fail_streak"], msg, st["refresh_expires_at"][:19]),
                          once_key="auth-keepalive-fail")
            else:
                st2 = read_status(lark_cli)
                after_exp = st2.get("refresh_expires_at") or ""
                moved = bool(after_exp) and after_exp != before_exp
                result["after_refresh_expires_at"] = after_exp
                result["after_days_left"] = st2.get("days_left")
                result["window_moved"] = moved
                if moved:
                    result["action"] = "refreshed"
                    log("✅ 窗口已顺延 %s → %s（剩余 %.1f 天）"
                        % (before_exp[:19], after_exp[:19], st2.get("days_left") or 0))
                    write_state({"last_refresh_ts": now_ts,
                                 "last_refresh_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "refresh_expires_at": after_exp,
                                 "retry_after_ts": 0,
                                 "fail_streak": 0})
                else:
                    # 调用成功但窗口未动：access token 仍在有效期内，刷新接口未被触发。
                    # 等它过期（生命周期约 2 小时）再试，避免此处误记成功、拖到 7 天后才发现。
                    r = now_ts + 2.5 * 3600
                    result["action"] = "refresh_pending"
                    log("⏳ 调用成功但窗口未变（access token 未过期），%.1f 小时后重试"
                        % ((r - now_ts) / 3600.0))
                    write_state({"last_refresh_ts": last_ts,
                                 "last_refresh_at": stt.get("last_refresh_at", ""),
                                 "refresh_expires_at": after_exp or before_exp,
                                 "retry_after_ts": r,
                                 "fail_streak": 0})
        else:
            log("授权正常：剩余 %.1f 天，%s，无需刷新" % (days, why))
            stt.update({"fail_streak": 0,
                        "last_check_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "refresh_expires_at": st["refresh_expires_at"]})
            write_state(stt)

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

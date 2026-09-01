#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取飞书群里「用户 @机器人」的自然语言指令（价格提醒设置的输入通道）。

输出格式：每行一条 JSON {create_time, sender, text}
只输出「普通用户发送 + 文本里含 @（提到机器人）」的消息，并记录水位避免重复输出。

用法:
  python fetch_mentions.py --chat-id oc_xxx      # 指定群
  python fetch_mentions.py --reset               # 重置水位
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

from common import find_bash

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(SKILL_DIR, "data", "_mentions_state.json")
LOCAL_ENV = os.path.join(SKILL_DIR, "data", "local_config.env")

DEFAULT_CHAT_ID = ""


def _env_value(key, default=""):
    try:
        with open(LOCAL_ENV, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip()
    except Exception:
        pass
    return default


def find_lark_cli():
    p = shutil.which("lark-cli")
    if p:
        return p
    for c in (os.path.expandvars(r"%APPDATA%\npm\lark-cli"),
              os.path.expandvars(r"%APPDATA%\npm\lark-cli.cmd")):
        if c and os.path.exists(c):
            return c
    return "lark-cli"


def load_watermark():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f).get("last_message_time", "")
        except Exception:
            pass
    return ""


def save_watermark(t):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"last_message_time": t}, f, ensure_ascii=False)


def extract_text(msg):
    if msg.get("msg_type") != "text":
        return ""
    c = msg.get("content", "")
    if isinstance(c, str) and c.strip().startswith("{"):
        try:
            o = json.loads(c)
            return (o.get("text", "") or "").strip()
        except Exception:
            return c.strip()
    return c.strip() if isinstance(c, str) else ""


def fetch_messages(chat_id, start_iso=None):
    # 通过 Git Bash 调用 POSIX 版 lark-cli（Windows 的 .cmd 版本「输出后进程不退出」会卡死），
    # 输出重定向到文件再读取（避免管道卡死），timeout -k 3 兜底强杀。
    tmp = os.path.join(SKILL_DIR, "data", "_lark_chat_out.json")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    parts = ["timeout", "-k", "3", "60", "lark-cli", "im", "+chat-messages-list",
             "--chat-id", chat_id, "--as", "user", "--order", "asc",
             "--page-all", "--page-limit", "200", "--no-reactions", "--json"]
    if start_iso:
        parts += ["--start", start_iso]
    cmd = " ".join(shlex.quote(p) for p in parts) + " > data/_lark_chat_out.json 2>&1"
    try:
        subprocess.run([find_bash(), "-c", cmd], capture_output=True, timeout=75, cwd=SKILL_DIR)
    except subprocess.TimeoutExpired:
        print("[ERROR] lark-cli 拉取超时", file=sys.stderr)
        return []
    except Exception as e:
        print("[ERROR] lark-cli 拉取失败: %s" % str(e)[:200], file=sys.stderr)
        return []
    try:
        with open(tmp, "r", encoding="utf-8") as f:
            out = f.read()
    except Exception:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    if not data.get("ok"):
        return []
    return data.get("data", {}).get("messages", []) or []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", default="")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--process", action="store_true", help="处理@指令：解析→设置提醒→群确认")
    args = ap.parse_args()

    if args.reset:
        if os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)
            print("[OK] 水位已重置")
        return

    chat_id = args.chat_id or _env_value("VIP_PUSH_CHAT_ID", "") or DEFAULT_CHAT_ID
    if not chat_id:
        print("[ERROR] 未指定 --chat-id，且 local_config.env 无 VIP_PUSH_CHAT_ID", file=sys.stderr)
        sys.exit(1)

    wm = load_watermark()
    start_iso = None
    if wm:
        from datetime import datetime
        try:
            start_iso = datetime.strptime(wm, "%Y-%m-%d %H:%M").strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"
        except Exception:
            pass

    msgs = fetch_messages(chat_id, start_iso)
    new_wm = wm
    for m in msgs:
        sender = (m.get("sender") or {})
        stype = sender.get("sender_type", "")
        if stype in ("app", "bot"):
            continue  # 跳过机器人自己
        text = extract_text(m)
        ct = m.get("create_time", "")
        if ct > new_wm:
            new_wm = ct
        if not text or "@" not in text:
            continue  # 只保留 @ 机器人 的指令
        # 去掉 @xxx 提及（含机器人名），避免机器人名里的数字被误当成价格
        text = re.sub(r"@\S+\s*", "", text).strip()
        if not text:
            continue
        if args.process:
            _process_one(ct, sender.get("name", ""), text)
        else:
            print(json.dumps({"create_time": ct, "sender": sender.get("name", ""), "text": text},
                             ensure_ascii=False))
    if new_wm != wm:
        save_watermark(new_wm)


def _process_one(ct, sender, text):
    """解析一条@指令 → 设置提醒 → 群确认（非价格指令/解析失败时保持安静）。"""
    from price_alerts import parse_alert_text, add_alert, query_price
    r = parse_alert_text(text)
    if not r:
        # 不是价格提醒指令（聊天、提问等），不打扰群里
        return
    target, cond, price, price2 = r
    # 校验标的是否能查到价
    res = query_price(target)
    if res is None:
        msg = f"⚠️ 查不到「{target}」的行情，请确认标的名/代码（指令：{text[:60]}）"
        print("[WARN] " + msg)
        _notify(msg)
        return
    cur_price, name, chg = res
    add_alert(target, cond, price, price2, note=text, chat_id="")
    cond_txt = {"below": "跌破", "above": "突破/涨到", "range": "区间"}[cond]
    rng = f"{price}~{price2}" if price2 else str(price)
    msg = f"✅ 已设置提醒：{name} {cond_txt} {rng} 就提醒（当前 {cur_price}）"
    print("[OK] " + msg)
    _notify(msg)


def _notify(msg):
    import subprocess
    try:
        subprocess.run([find_bash(), os.path.join(SKILL_DIR, "scripts", "notify_group.sh"), msg],
                       capture_output=True, timeout=30, cwd=SKILL_DIR)
    except Exception:
        pass


if __name__ == "__main__":
    main()

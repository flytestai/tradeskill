#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给群消息加/取消「敲键盘(Typing)」表情回复，用作「机器人正在处理」的互动提示。

同时维护 data/_typing_state.json（message_id -> 加表情时间戳），
提供 cleanup 兜底清理：超过 N 分钟还没被取消的敲键盘表情自动清掉。

用法:
  python react.py add <message_id>          # 加 Typing 表情（记录时间戳）
  python react.py remove <message_id>       # 取消本机器人加的 Typing 表情
  python react.py cleanup [--max-age 10]    # 清理超过 N 分钟未取消的残留表情
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time

from common import find_bash

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = find_bash()
EMOJI = "Typing"  # 飞书「敲键盘/正在输入」表情
STATE_FILE = os.path.join(SKILL_DIR, "data", "_typing_state.json")


def _run_lark(cmd_parts, timeout=30):
    try:
        cmd = " ".join(shlex.quote(p) for p in cmd_parts)
        r = subprocess.run([BASH, "-c", cmd],
                           capture_output=True, timeout=timeout, cwd=SKILL_DIR)
    except Exception:
        return {}
    out = (r.stdout or b"") + (r.stderr or b"")
    try:
        return json.loads(out.decode("utf-8", "ignore"))
    except Exception:
        return {}


def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {}


def _save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        pass


def _clear_state(message_id):
    state = _load_state()
    if message_id in state:
        del state[message_id]
        _save_state(state)


def add_typing(message_id):
    """加 Typing 表情，成功返回 reaction_id，失败返回空串。"""
    if not message_id:
        return ""
    cmd = ["timeout", "-k", "3", "30", "lark-cli", "im", "reactions", "create",
           "--params", json.dumps({"message_id": message_id}),
           "--data", json.dumps({"reaction_type": {"emoji_type": EMOJI}}),
           "--as", "bot"]
    data = _run_lark(cmd)
    rid = (data.get("data") or {}).get("reaction_id", "") if data.get("ok") else ""
    if rid:
        state = _load_state()
        state[message_id] = time.time()
        _save_state(state)
    return rid


def remove_typing(message_id):
    """取消本机器人（operator_type=app）加在该消息上的 Typing 表情，成功返回 True。"""
    if not message_id:
        return False
    # 1) 列出该消息上的 Typing 表情
    cmd = ["timeout", "-k", "3", "30", "lark-cli", "im", "reactions", "list",
           "--params", json.dumps({"message_id": message_id, "reaction_type": EMOJI}),
           "--as", "bot"]
    data = _run_lark(cmd)
    if not data.get("ok"):
        return False  # 拉取失败，保留状态待重试
    items = (data.get("data") or {}).get("items") or []
    rid = ""
    for it in items:
        op = it.get("operator") or {}
        if op.get("operator_type") == "app":
            rid = it.get("reaction_id", "")
            break
    if not rid:
        # 已经没有本机器人加的 Typing 表情，视为已清理
        _clear_state(message_id)
        return False
    # 2) 删除
    cmd = ["timeout", "-k", "3", "30", "lark-cli", "im", "reactions", "delete",
           "--params", json.dumps({"message_id": message_id, "reaction_id": rid}),
           "--as", "bot"]
    data = _run_lark(cmd)
    if data.get("ok"):
        _clear_state(message_id)
        return True
    return False  # 删除失败，保留状态待重试


def cleanup_typing(max_age_seconds=600):
    """清理超过 max_age_seconds 还没取消的敲键盘表情，返回实际清除数量。"""
    state = _load_state()
    if not state:
        return 0
    now = time.time()
    cleaned = 0
    for mid in list(state.keys()):
        try:
            age = now - float(state[mid])
        except Exception:
            age = max_age_seconds + 1
        if age >= max_age_seconds:
            if remove_typing(mid):
                cleaned += 1
    return cleaned


def main():
    ap = argparse.ArgumentParser(description="消息表情互动：加/取消/兜底清理「敲键盘(Typing)」表情")
    ap.add_argument("cmd", choices=["add", "remove", "cleanup"], help="add/remove/cleanup")
    ap.add_argument("message_id", nargs="?", default="", help="目标消息 message_id（om_xxx，cleanup 不需要）")
    ap.add_argument("--max-age", type=int, default=10, help="cleanup 用：超过多少分钟视为残留（默认10）")
    args = ap.parse_args()

    if args.cmd == "add":
        rid = add_typing(args.message_id)
        if rid:
            print("[OK] 已加 Typing 表情 %s" % rid)
        else:
            print("[WARN] 加 Typing 表情失败", file=sys.stderr)
            sys.exit(1)
    elif args.cmd == "remove":
        if remove_typing(args.message_id):
            print("[OK] 已取消 Typing 表情")
        else:
            print("[INFO] 未找到需要取消的 Typing 表情")
    elif args.cmd == "cleanup":
        n = cleanup_typing(max_age_seconds=args.max_age * 60)
        print("[OK] 兜底清理完成，清除 %d 个残留敲键盘表情" % n)


if __name__ == "__main__":
    main()

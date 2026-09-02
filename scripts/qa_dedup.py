#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""荔枝群通用问答去重：记录「已回答过的问题」，避免重复回答。

键 = md5(提问人 open_id + 归一化问题文本)；归一化去掉所有空白并转小写。
同一用户重复提同一个问题 → 只回答一次；不同用户问同一问题 → 各自回答。

用法:
  python qa_dedup.py list     # 列出已回答的问题
  python qa_dedup.py clear    # 清空去重记录（想重新回答时使用）
"""
import argparse
import hashlib
import json
import os
import re

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSWERED_FILE = os.path.join(SKILL_DIR, "data", "group_qa_answered.json")


def _norm(s):
    return re.sub(r"\s+", "", s or "").lower()


def question_key(sender_id, text):
    raw = "%s|%s" % (sender_id or "", _norm(text))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def load():
    if os.path.exists(ANSWERED_FILE):
        try:
            with open(ANSWERED_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {}


def is_answered(sender_id, text, answered=None):
    if answered is None:
        answered = load()
    return question_key(sender_id, text) in answered


def mark_answered(sender_id, text, sender="", answered_at=""):
    d = load()
    key = question_key(sender_id, text)
    d[key] = {
        "sender_id": sender_id or "",
        "sender": sender or "",
        "question": (text or "")[:500],
        "answered_at": answered_at or "",
    }
    try:
        os.makedirs(os.path.dirname(ANSWERED_FILE), exist_ok=True)
        with open(ANSWERED_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return key


def clear():
    try:
        if os.path.exists(ANSWERED_FILE):
            os.remove(ANSWERED_FILE)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="通用问答去重记录管理")
    ap.add_argument("cmd", choices=["list", "clear"], help="list=列出 / clear=清空")
    args = ap.parse_args()
    if args.cmd == "list":
        d = load()
        if not d:
            print("[INFO] 暂无已回答记录")
            return
        print("已回答记录（共 %d 条）：" % len(d))
        for k, v in d.items():
            print("  - [%s] @%s: %s" % (v.get("answered_at", "")[:16], v.get("sender") or "-",
                                        (v.get("question") or "")[:60]))
    elif args.cmd == "clear":
        clear()
        print("[OK] 已清空去重记录")


if __name__ == "__main__":
    main()

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
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSWERED_FILE = os.path.join(SKILL_DIR, "data", "group_qa_answered.json")


def _norm(s):
    return re.sub(r"\s+", "", s or "").lower()


def question_key(sender_id, text):
    raw = "%s|%s" % (sender_id or "", _norm(text))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


# ⚠️ 原子写 + 损坏可见（详见 safe_json）
#    去重记录一旦因文件损坏被静默当成空，**所有历史问题都会被重复回答**。
try:
    from safe_json import read_json, write_json
except Exception:
    def read_json(path, default=None, **kw):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default if default is not None else {}

    def write_json(path, data, indent=2):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=indent)
            return True
        except Exception:
            return False


def _on_corrupt(exc, path, backup):
    try:
        sys.stderr.write("[ERROR] 去重记录损坏: %s（已备份至 %s）: %s\n"
                         % (path, backup or "无", exc))
    except Exception:
        pass


def load():
    d = read_json(ANSWERED_FILE, default={}, on_error=_on_corrupt)
    return d if isinstance(d, dict) else {}


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
    if not write_json(ANSWERED_FILE, d):
        # 写失败必须留痕：否则下轮会重复回答同一问题
        try:
            sys.stderr.write("[ERROR] 去重记录写入失败: %s\n" % ANSWERED_FILE)
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

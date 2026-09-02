#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""荔枝群问答队列：sync_litchi_auto.py 写入，Bee 定时任务读取并逐条处理。

队列文件：data/group_qa_queue.json（JSON 数组，每项含 message_id/sender/sender_id/text/...）。

用法:
  python qa_queue.py peek               # 输出待处理队列（无则输出 []）
  python qa_queue.py done <message_id>  # 处理完一条后移除
  python qa_queue.py clear              # 清空队列
"""
import argparse
import json
import os

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE_FILE = os.path.join(SKILL_DIR, "data", "group_qa_queue.json")


def load():
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, list):
                return d
        except Exception:
            pass
    return []


def save(items):
    try:
        os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def append_item(item):
    items = load()
    if item.get("message_id") and any(it.get("message_id") == item.get("message_id") for it in items):
        return False
    items.append(item)
    save(items)
    return True


def remove_item(message_id):
    items = load()
    new = [it for it in items if it.get("message_id") != message_id]
    if len(new) != len(items):
        save(new)
        return True
    return False


def clear():
    save([])


def main():
    ap = argparse.ArgumentParser(description="荔枝群问答队列管理")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("peek", help="输出待处理队列")
    p_done = sub.add_parser("done", help="移除一条已处理项")
    p_done.add_argument("message_id")
    sub.add_parser("clear", help="清空队列")

    args = ap.parse_args()
    if args.cmd == "peek":
        print(json.dumps(load(), ensure_ascii=False))
    elif args.cmd == "done":
        if remove_item(args.message_id):
            print("[OK] 已移除 %s" % args.message_id)
        else:
            print("[INFO] 队列中无 %s" % args.message_id)
    elif args.cmd == "clear":
        clear()
        print("[OK] 队列已清空")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

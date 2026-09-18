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
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE_FILE = os.path.join(SKILL_DIR, "data", "group_qa_queue.json")


# ⚠️ 原子写 + 损坏可见（详见 safe_json 模块说明）
#    此前用裸 json.dump 写入：进程被杀/磁盘满会留下截断的 JSON，
#    而 load() 的 `except: pass` 会把它当成空列表 —— **整条待处理队列静默丢失**。
#    实测：截断的队列文件 → load() 返回 []。
try:
    from safe_json import read_json, write_json
except Exception:                       # 兜底：退化为原实现
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
    """队列文件损坏时必须留痕：这是「问题被丢掉」级别的故障。"""
    try:
        sys.stderr.write("[ERROR] 问答队列文件损坏: %s（已备份至 %s）: %s\n"
                         % (path, backup or "无", exc))
    except Exception:
        pass


def load():
    d = read_json(QUEUE_FILE, default=[], on_error=_on_corrupt)
    return d if isinstance(d, list) else []


def save(items):
    if not write_json(QUEUE_FILE, items):
        try:
            sys.stderr.write("[ERROR] 问答队列写入失败: %s\n" % QUEUE_FILE)
        except Exception:
            pass
        return False
    return True


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

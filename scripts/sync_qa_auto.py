#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一问答捕获：消费 im.message.receive_v1 事件，同时处理群聊@机器人和私信。

合并原 sync_litchi_auto.py（群聊轮询）与 sync_dm_auto.py（私信监听）为单一轮询任务：
  - 群聊（chat_type=group）：只保留用户 @机器人 的文本消息 → chat_type=group
  - 私信（chat_type=p2p）：用户发给机器人的文本消息 → chat_type=p2p
  - 两者共用 data/group_qa_queue.json，交给 Bee 定时任务统一处理回复

用法:
  python sync_qa_auto.py            # 前台长连接，消费事件并入队
  python sync_qa_auto.py --dry-run  # 只打印，不入队
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

import qa_queue

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TEST_KEYWORDS = ["转发测试", "同步测试", "test", "TEST"]


def find_lark_cli():
    p = shutil.which("lark-cli")
    if p:
        return p
    for c in (os.path.expandvars(r"%APPDATA%\npm\lark-cli"),
              os.path.expandvars(r"%APPDATA%\npm\lark-cli.cmd")):
        if c and os.path.exists(c):
            return c
    return "lark-cli"


def ms_to_dt(ms):
    """毫秒时间戳字符串 → 'YYYY-MM-DD HH:MM:SS'（北京时间）。"""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def is_test(text):
    return any(k in (text or "") for k in TEST_KEYWORDS)


def clean_group_text(content, mentions):
    """去掉群消息里的 @mention，返回纯问题文本（与 trade365 / sync_litchi 一致）。"""
    t = content or ""
    for m in (mentions or []):
        key = m.get("key") or ""
        if key:
            t = t.replace(key, "")
    t = re.sub(r"@\S+\s*", "", t).strip()
    return t


def handle_event(obj, dry_run=False):
    """处理一条 im.message.receive_v1 事件，入队群聊@机器人或私信消息。"""
    o = obj.get("event", obj) if isinstance(obj, dict) else obj
    if not isinstance(o, dict):
        return
    if o.get("sender_type") != "user":
        return
    if o.get("message_type") != "text":
        return
    content = (o.get("content") or "").strip()
    if not content or is_test(content):
        return

    chat_type = o.get("chat_type") or ""
    mentions = o.get("mentions") or []

    if chat_type == "p2p":
        text = content  # 私信全文即问题
    elif chat_type == "group":
        if "@" not in content and not mentions:
            return  # 群聊只保留 @机器人
        text = clean_group_text(content, mentions)
        if not text or is_test(text):
            return
    else:
        return

    message_id = o.get("message_id") or o.get("id") or ""
    if not message_id:
        return

    item = {
        "message_id": message_id,
        "sender": o.get("sender_id") or "",
        "sender_id": o.get("sender_id") or "",
        "text": text,
        "create_time": ms_to_dt(o.get("create_time") or o.get("timestamp") or ""),
        "chat_id": o.get("chat_id") or "",
        "chat_type": chat_type,
    }
    if dry_run:
        print("[DRY] %s 入队: %s" % (chat_type, json.dumps(item, ensure_ascii=False)))
        return
    if qa_queue.append_item(item):
        print("[QA] %s 已入队 %s: %s" % (chat_type, item["sender_id"], text[:40]))


def consume(dry_run=False):
    lark = find_lark_cli()
    backoff = 5
    while True:
        try:
            cmd = [lark, "event", "consume", "im.message.receive_v1", "--as", "bot"]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                                    encoding="utf-8", creationflags=NO_WINDOW)
            print("[QA] 统一问答监听已启动（群聊@机器人 + 私信）")
            backoff = 5
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    handle_event(obj, dry_run=dry_run)
                except Exception as e:
                    print("[QA] 处理事件异常: %s" % str(e)[:200], file=sys.stderr)
        except Exception as e:
            print("[QA] 消费异常: %s" % str(e)[:200], file=sys.stderr)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


def main():
    ap = argparse.ArgumentParser(description="统一问答捕获：群聊@机器人 + 私信")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不入队")
    args = ap.parse_args()
    consume(dry_run=args.dry_run)


if __name__ == "__main__":
    main()

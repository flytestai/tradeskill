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

注意（历史故障）:
  lark-cli 的 `event consume` 把 stdin EOF 当作退出信号（"stdin closed — shutting down"）。
  早期版本这里用 stdin=DEVNULL 启动，导致消费者 0 秒即退出、被 supervisor 无限重启，
  日志里只剩反复的“统一问答监听已启动”，任何 @机器人 / 私信都收不到。
  现在统一用 stdin=PIPE 并保持打开，进程可长期驻留；另有空闲看门狗兜底重启。
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

import qa_queue
from common import find_bash

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BASH = find_bash()

TEST_KEYWORDS = ["转发测试", "同步测试", "test", "TEST"]


def _posix_path(path):
    """把 Windows 路径转换为 Git Bash 可执行的 POSIX 路径。"""
    p = (path or "").replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", p):
        return "/" + p[0].lower() + p[2:]
    return p


def _prefer_posix_cli(path):
    """优先使用 npm 生成的 POSIX 启动脚本，避免直接 Popen .cmd 触发 WinError 193。"""
    if path and path.lower().endswith(".cmd"):
        posix = path[:-4]
        if os.path.exists(posix):
            return posix
    return path


def find_lark_cli():
    candidates = (
        os.path.expandvars(r"%APPDATA%\bee_ai_test\agent-runtime\npm-global\lark-cli"),
        os.path.expandvars(r"%APPDATA%\bee_ai_test\agent-runtime\npm-global\lark-cli.cmd"),
        os.path.expandvars(r"%APPDATA%\npm\lark-cli"),
        os.path.expandvars(r"%APPDATA%\npm\lark-cli.cmd"),
    )
    for c in candidates:
        if c and os.path.exists(c):
            return _prefer_posix_cli(c)
    p = shutil.which("lark-cli")
    return _prefer_posix_cli(p) if p else "lark-cli"


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


def _reader(proc, idle_seconds, stop_evt):
    """前台读取事件；超过 idle_seconds 没收到任何输出（也含心跳）则判定假死，杀掉以便重连。"""
    try:
        for line in proc.stdout:
            idle_seconds["t"] = time.time()
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                handle_event(obj, idle_seconds.get("dry_run", False))
            except Exception as e:
                print("[QA] 处理事件异常: %s" % str(e)[:200], file=sys.stderr)
    except Exception as e:
        print("[QA] 读取异常: %s" % str(e)[:200], file=sys.stderr)
    finally:
        stop_evt.set()


def consume(dry_run=False):
    lark = _posix_path(find_lark_cli())
    backoff = 5
    idle_timeout = 1800.0  # 30 分钟无任何输出视为连接假死，主动重连
    while True:
        proc = None
        try:
            # Windows 不能直接 Popen npm 的 .cmd/无扩展脚本；统一经 Git Bash 启动，
            # 否则会反复出现 WinError 193，进程看似存活但实际上收不到事件。
            #
            # 关键：lark-cli event consume 把 stdin EOF 当退出信号，必须给一个保持打开的管道，
            # 绝不能再用 DEVNULL，否则秒退并陷入「启动-退出-重启」死循环，收不到任何消息。
            cli_cmd = " ".join(shlex.quote(x) for x in [lark, "event", "consume", "im.message.receive_v1", "--as", "bot"])
            proc = subprocess.Popen(
                [BASH, "-c", "exec " + cli_cmd],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=NO_WINDOW,
            )
            print("[QA] 统一问答监听已启动（群聊@机器人 + 私信）")
            backoff = 5
            state = {"t": time.time(), "dry_run": dry_run}
            stop_evt = threading.Event()
            th = threading.Thread(target=_reader, args=(proc, state, stop_evt), daemon=True)
            th.start()
            last_start = time.time()
            while not stop_evt.is_set():
                time.sleep(1)
                if proc.poll() is not None:
                    break
                if time.time() - state["t"] > idle_timeout:
                    print("[QA] 监听空闲超过 %.0f 分钟，主动重连" % (idle_timeout / 60), file=sys.stderr)
                    break
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=10)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            stop_evt.set()
            if time.time() - last_start < 60:
                # 秒退（典型原因：stdin EOF / 鉴权失效）——打印一次便于排查
                print("[QA] 监听连接仅存活 %.0f 秒即退出，5 秒后重连" % (time.time() - last_start), file=sys.stderr)
        except Exception as e:
            print("[QA] 消费异常: %s" % str(e)[:200], file=sys.stderr)
        finally:
            if proc is not None:
                for s in (proc.stdin, proc.stdout):
                    try:
                        if s is not None:
                            s.close()
                    except Exception:
                        pass
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


def main():
    ap = argparse.ArgumentParser(description="统一问答捕获：群聊@机器人 + 私信")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不入队")
    args = ap.parse_args()
    consume(dry_run=args.dry_run)


if __name__ == "__main__":
    main()

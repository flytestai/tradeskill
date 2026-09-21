#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""荔枝群「用户@机器人」消息 30 秒轮询（脚本实现，替代每分钟 Bee 定时任务）。

与 sync_feishu_auto.py 的 30 秒轮询循环同构：
  - 通过 lark-cli（用户授权）按水位增量拉取「荔枝种植交流群」的新消息
  - 只保留「普通用户 @机器人」的文本消息，跳过机器人自己 / 测试消息 / 已回答过的问题
  - 给每条新@消息加「敲键盘(Typing)」表情（表示机器人正在处理）
  - 把新@消息追加到 data/group_qa_queue.json 队列，交给 Bee 定时任务去解析并回复

用法:
  python sync_litchi_auto.py                        # 跑一轮
  python sync_litchi_auto.py --loop --interval 30   # 后台 30 秒轮询循环（文件锁防重复）
  python sync_litchi_auto.py --reset-watermark      # 重置水位（下次全量）
  python sync_litchi_auto.py --dry-run              # 只预览，不写水位/队列/表情
"""
import argparse
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

from common import find_bash
import qa_dedup
import qa_queue
import react

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

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = find_bash()

LOCAL_ENV = os.path.join(SKILL_DIR, "data", "local_config.env")
LOOP_STALE_SEC = 180  # 锁超过 180 秒未心跳视为残留，可被接管

TEST_KEYWORDS = ["转发测试", "同步测试", "test", "TEST"]

# 群配置：group -> (chat_id 的 env key, 水位文件名, 锁文件名, 旧水位文件名)
GROUPS = {
    "litchi": ("VIP_PUSH_CHAT_ID", "_litchi_watermark.json", "_litchi_loop.lock", "_mentions_state.json"),
    "review": ("REVIEW_CHAT_ID", "_review_watermark.json", "_review_loop.lock", ""),
}

# 运行时由 apply_group() 按 --group 填充
DEFAULT_CHAT_ID = ""
WATERMARK_FILE = ""
LEGACY_WATERMARK_FILE = ""
LOOP_LOCK_FILE = ""


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


def apply_group(group):
    global DEFAULT_CHAT_ID, WATERMARK_FILE, LEGACY_WATERMARK_FILE, LOOP_LOCK_FILE
    chat_key, wm, lock, legacy = GROUPS.get(group, GROUPS["litchi"])
    DEFAULT_CHAT_ID = _env_value(chat_key, "")
    WATERMARK_FILE = os.path.join(SKILL_DIR, "data", wm)
    LEGACY_WATERMARK_FILE = os.path.join(SKILL_DIR, "data", legacy) if legacy else ""
    LOOP_LOCK_FILE = os.path.join(SKILL_DIR, "data", lock)


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
    """读取水位；若新水位文件不存在，从旧的 fetch_mentions 水位迁移，避免重放历史。"""
    # ⚠️ 水位读写必须安全（详见 safe_json）
    #    水位文件若因写一半而损坏、又被静默当成空，机器人会**重新拉取全部
    #    历史消息并重复回复**。故：原子写 + 损坏留证并告警，绝不静默降级。
    for path in (WATERMARK_FILE, LEGACY_WATERMARK_FILE):
        if os.path.exists(path):
            d = read_json(path, default=None,
                          on_error=lambda e, _p, b: print(
                              "[ERROR] 水位文件损坏: %s（已备份 %s）: %s" % (_p, b or "无", e)))
            if isinstance(d, dict):
                return d.get("last_message_time", "")
    return ""


def save_watermark(t):
    if not t:
        return
    if not write_json(WATERMARK_FILE, {"last_message_time": t}):
        print("[WARN] 保存水位失败: %s" % WATERMARK_FILE)


def to_iso(ts):
    ts = (ts or "").strip()
    if not ts:
        return None
    if "T" in ts:
        s = ts.replace("Z", "").replace("z", "")
        if "+" in s:
            s = s.split("+", 1)[0]
        s = s[:19]
        if len(s) == 19:
            return s + "+08:00"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(ts, fmt).strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"
        except Exception:
            continue
    return None


def extract_text(msg):
    if msg.get("msg_type") != "text":
        return ""
    c = msg.get("content", "")
    if isinstance(c, str) and c.strip().startswith("{"):
        try:
            return (json.loads(c).get("text", "") or "").strip()
        except Exception:
            return c.strip()
    return c.strip() if isinstance(c, str) else ""


def fetch_messages_since(chat_id, start_iso=None):
    """通过 lark-cli 拉取 start_iso 之后的消息（升序，自动分页），失败返回 None。

    直接调用 lark-cli（与 sync_feishu_auto.py 一致），避免 bash -c 重定向在后台循环里卡住。
    """
    lark_cli = find_lark_cli()
    cmd = [lark_cli, "im", "+chat-messages-list",
           "--chat-id", chat_id, "--as", "user", "--order", "asc",
           "--page-all", "--page-limit", "200", "--no-reactions", "--json"]
    if start_iso:
        cmd += ["--start", start_iso]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding='utf-8', errors='replace')
    except Exception as e:
        print("[ERROR] lark-cli 拉取异常: %s" % str(e)[:200], file=sys.stderr)
        return None
    if r.returncode != 0:
        print("[ERROR] lark-cli 拉取失败: %s" % (r.stderr or r.stdout)[:200], file=sys.stderr)
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        print("[ERROR] lark-cli 输出解析失败", file=sys.stderr)
        return None
    if not data.get("ok"):
        print("[ERROR] lark-cli 返回异常: %s" % json.dumps(data.get("error", {}), ensure_ascii=False)[:200], file=sys.stderr)
        return None
    return data.get("data", {}).get("messages", []) or []


_OWN_LOCK_TS = None  # 本进程写入锁的时间戳，用于释放时校验归属


def _acquire_lock():
    global _OWN_LOCK_TS
    ts = str(time.time())
    try:
        fd = os.open(LOOP_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, ts.encode())
        os.close(fd)
        _OWN_LOCK_TS = ts
        return True
    except FileExistsError:
        last = 0.0
        try:
            with open(LOOP_LOCK_FILE) as f:
                last = float(f.read().strip() or "0")
        except Exception:
            last = 0.0
        # 空/零/读不到 → 视为「他人正在创建或持有」，绝不接管（避免创建竞态导致多进程同时跑）
        if last <= 0 or time.time() - last < LOOP_STALE_SEC:
            return False
        try:
            os.remove(LOOP_LOCK_FILE)
        except Exception:
            return False
        try:
            fd = os.open(LOOP_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, ts.encode())
            os.close(fd)
            _OWN_LOCK_TS = ts
            return True
        except FileExistsError:
            return False
    except Exception:
        return True


def _touch_lock():
    """刷新锁心跳。

    Windows 上 O_EXCL 持有的是独占句柄，Python 的 open(...,"w") 打不开它，
    心跳会静默失败、锁被误判过期，多个循环同时接管 → 重复入队。
    必须走底层 os.open(O_TRUNC) 写同一个文件。
    """
    global _OWN_LOCK_TS
    ts = str(time.time())
    try:
        fd = os.open(LOOP_LOCK_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        os.write(fd, ts.encode())
        os.close(fd)
        _OWN_LOCK_TS = ts
    except Exception:
        pass


def _release_lock():
    """只删除仍属于本进程的锁，避免误删后来接管者的锁。"""
    try:
        with open(LOOP_LOCK_FILE, "r") as f:
            raw = f.read().strip()
    except Exception:
        return
    if _OWN_LOCK_TS is not None and raw == _OWN_LOCK_TS:
        try:
            os.remove(LOOP_LOCK_FILE)
        except Exception:
            pass


def run_once(dry_run=False):
    chat_id = DEFAULT_CHAT_ID
    if not chat_id:
        print("[CONFIG] 未配置 data/local_config.env 的 VIP_PUSH_CHAT_ID", file=sys.stderr)
        return 1

    watermark = load_watermark()
    start_iso = to_iso(watermark) if watermark else None
    messages = fetch_messages_since(chat_id, start_iso)
    if messages is None:
        print("[FAIL] 拉取失败，本轮结束")
        return 1

    answered = qa_dedup.load()
    new_wm = watermark
    queued = 0
    for m in messages:
        ct = m.get("create_time", "")
        if ct and ct > new_wm:
            new_wm = ct
        sender = m.get("sender") or {}
        stype = sender.get("sender_type", "")
        if stype in ("app", "bot"):
            continue  # 跳过机器人自己
        text = extract_text(m)
        if not text or "@" not in text:
            continue  # 只保留 @机器人 的消息
        text = re.sub(r"@\S+\s*", "", text).strip()
        if not text:
            continue
        sender_name = sender.get("name", "")
        sender_id = sender.get("id") or sender.get("open_id") or ""
        message_id = m.get("message_id", "")
        if any(kw in text for kw in TEST_KEYWORDS):
            continue
        if qa_dedup.is_answered_recently(chat_id, sender_id, text, answered):
            continue  # 近期已回答过的问题不再入队（超过窗口则重新回答）
        item = {
            "message_id": message_id,
            "sender": sender_name,
            "sender_id": sender_id,
            "text": text,
            "create_time": ct,
            "chat_id": chat_id,
            "status": "pending",
        }
        if not dry_run:
            if message_id:
                react.add_typing(message_id)
            if qa_queue.append_item(item):
                queued += 1
                print("[QUEUE] %s @%s: %s" % (ct, sender_name, text[:60]))
        else:
            queued += 1
            print("[DRY] %s @%s: %s" % (ct, sender_name, text[:60]))

    # 水位前移 1 分钟，避免 --start 边界重复拉到同一条
    if new_wm and new_wm != watermark:
        try:
            new_wm = (datetime.strptime(new_wm, "%Y-%m-%d %H:%M") + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    if not dry_run and new_wm:
        save_watermark(new_wm)

    print("[INFO] 本轮：拉到 %d 条消息，新入队 %d 条，水位 %s" % (len(messages), queued, new_wm or watermark))
    return 0


def run_loop(interval):
    interval = max(5, interval)
    if not _acquire_lock():
        print("[LOOP] 已有荔枝群轮询循环在运行，本次跳过")
        return
    print("[LOOP] 荔枝群 30 秒轮询循环启动：每 %d 秒一次" % interval)
    try:
        while True:
            _touch_lock()
            t0 = time.time()
            try:
                run_once(dry_run=False)
            except Exception as e:
                print("[LOOP] 单次轮询异常: %s" % e)
            time.sleep(max(0, interval - (time.time() - t0)))
    finally:
        _release_lock()


def main():
    ap = argparse.ArgumentParser(description="荔枝群@机器人消息 30 秒轮询")
    ap.add_argument("--loop", action="store_true", help="后台轮询循环模式")
    ap.add_argument("--interval", type=int, default=30, help="--loop 模式轮询间隔秒数（默认30）")
    ap.add_argument("--reset-watermark", action="store_true", help="重置增量水位")
    ap.add_argument("--dry-run", action="store_true", help="只预览不写入")
    ap.add_argument("--group", choices=list(GROUPS.keys()), default="litchi",
                    help="群标识（litchi=荔枝群，review=每日复盘群）")
    args = ap.parse_args()
    apply_group(args.group)

    if args.reset_watermark:
        for p in (WATERMARK_FILE, LEGACY_WATERMARK_FILE):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        print("[OK] 已重置水位")
        return

    if args.loop:
        run_loop(args.interval)
        return

    sys.exit(run_once(dry_run=args.dry_run))


if __name__ == "__main__":
    main()

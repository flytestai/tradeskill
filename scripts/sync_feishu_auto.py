#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wu2198 五号群 消息自动同步（合并版：飞书群拉取 + 大V发言分析）

功能：
  1. 通过 lark-cli（OAuth 用户授权）拉取 wu2198五号群 最新的机器人消息（即 wu2198 的发言）
  2. 按 97% 文本相似度去重后，增量导入 kol-opinion-analyzer 的 SQLite 数据库
  3. 测试消息自动跳过
  4. 已导入过的消息不会重复导入
  5. 报告本次同步结果（新增导入条数、跳过条数）
  6. 有新增时自动导出 JSON 并推送到 GitHub

用法:
  python sync_feishu_auto.py                # 正常同步（盘中 + 交易日守卫）
  python sync_feishu_auto.py --force        # 忽略盘中/交易日守卫，强制同步
  python sync_feishu_auto.py --no-push      # 不同步到 GitHub
  python sync_feishu_auto.py --dry-run      # 只预览，不写库不推送
  python sync_feishu_auto.py --page-size 100
"""
import argparse
import difflib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")
SYNC_SCRIPT = os.path.join(SKILL_DIR, "scripts", "sync.py")

DEFAULT_CHAT_ID = "oc_59301fc3e11c6e131f31ffb8acd4125a"
BOT_SENDER_TYPES = ("app", "bot")          # 机器人消息（wu2198 发言由自定义机器人发出）
SIM_THRESHOLD = 0.97                        # 文本相似度去重阈值
TEST_KEYWORDS = ["测试", "test", "TEST", "这是一条测试", "同步测试", "设备A同步测试"]


def find_lark_cli():
    """定位 lark-cli 可执行文件（兼容 PATH 与常见全局安装目录）"""
    p = shutil.which("lark-cli")
    if p:
        return p
    candidates = [
        os.path.expandvars(r"%APPDATA%\npm\lark-cli"),
        os.path.expandvars(r"%APPDATA%\npm\lark-cli.cmd"),
        os.path.expanduser("~/.npm-global-user/lark-cli"),
        "/usr/local/bin/lark-cli",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return "lark-cli"


def trading_time_guard():
    """交易日 + 盘中时间守卫（北京时间）。返回 (是否可运行, 原因)"""
    now = datetime.now(timezone(timedelta(hours=8)))
    if now.weekday() >= 5:
        return False, "非交易日（周末）"
    hm = now.hour * 100 + now.minute
    # 盘中 9:30-11:30 / 13:00-15:00，以及盘后 16:00 各执行一次
    if (930 <= hm <= 1130) or (1300 <= hm <= 1500) or (1555 <= hm <= 1605):
        return True, ""
    return False, "非盘中/盘后时间（当前 %02d:%02d）" % (now.hour, now.minute)


def fetch_latest_messages(lark_cli, chat_id, page_size=50):
    """通过 lark-cli 拉取群内最新消息（默认最新 50 条，倒序）"""
    cmd = [
        lark_cli, "im", "+chat-messages-list",
        "--chat-id", chat_id,
        "--as", "user",
        "--order", "desc",
        "--page-size", str(page_size),
        "--no-reactions",
        "--json",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("[ERROR] lark-cli 拉取超时")
        return None
    if r.returncode != 0:
        print("[ERROR] lark-cli 拉取失败: %s" % (r.stderr[:300] or r.stdout[:300]))
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        print("[ERROR] 解析 lark-cli 输出失败")
        return None
    if not data.get("ok"):
        print("[ERROR] lark-cli 返回异常: %s" % json.dumps(data.get("error", {}), ensure_ascii=False)[:200])
        return None
    return data.get("data", {}).get("messages", []) or []


def extract_text(msg):
    """从消息 item 提取纯文本（仅 text 类型）"""
    if msg.get("msg_type") != "text":
        return None
    c = msg.get("content", "")
    if isinstance(c, str) and c.strip().startswith("{"):
        try:
            o = json.loads(c)
            return (o.get("text", "") or "").strip()
        except Exception:
            return c.strip()
    return c.strip() if isinstance(c, str) else ""


def is_test_message(text):
    if not text:
        return True
    for kw in TEST_KEYWORDS:
        if kw in text:
            return True
    return False


def normalize(text):
    return "".join(text.split())


def ensure_schema(conn):
    """确保 kol_records 表存在，不存在则调用 db_init.py 初始化"""
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='kol_records'")
    if cur.fetchone() is None:
        print("[INIT] 数据库表不存在，初始化中 ...")
        subprocess.run([sys.executable, os.path.join(SKILL_DIR, "scripts", "db_init.py")],
                       capture_output=True, timeout=60)


def main():
    ap = argparse.ArgumentParser(description="wu2198五号群消息自动同步（合并版）")
    ap.add_argument("--chat-id", default=DEFAULT_CHAT_ID)
    ap.add_argument("--threshold", type=float, default=SIM_THRESHOLD)
    ap.add_argument("--page-size", type=int, default=50)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--force", action="store_true", help="忽略盘中/交易日守卫")
    ap.add_argument("--no-push", action="store_true", help="跳过 GitHub 推送")
    ap.add_argument("--dry-run", action="store_true", help="只预览不写库")
    args = ap.parse_args()

    if not args.force:
        ok, reason = trading_time_guard()
        if not ok:
            print("[SKIP] %s" % reason)
            return

    lark_cli = find_lark_cli()
    print("=" * 56)
    print("  wu2198五号群 消息自动同步")
    print("  时间: %s" % datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"))
    print("  群ID: %s" % args.chat_id)
    print("=" * 56)

    # 1. 拉取最新消息
    messages = fetch_latest_messages(lark_cli, args.chat_id, args.page_size)
    if messages is None:
        print("[FAIL] 拉取失败，本轮结束")
        sys.exit(1)
    print("[1/4] 拉到 %d 条消息" % len(messages))

    # 2. 过滤机器人消息 + 提取文本
    bot_texts = []
    for m in messages:
        s = m.get("sender") or {}
        stype = s.get("sender_type", "")
        sname = s.get("name", "")
        if stype not in BOT_SENDER_TYPES and sname != "自定义机器人":
            continue
        t = extract_text(m)
        if t is None:
            continue
        bot_texts.append((m.get("create_time", ""), t))
    print("[2/4] 机器人文本消息 %d 条" % len(bot_texts))

    # 3. 去重 + 入库
    conn = sqlite3.connect(args.db)
    ensure_schema(conn)
    cur = conn.cursor()
    cur.execute("SELECT content FROM kol_records WHERE kol_name='wu2198'")
    existing = [r[0] for r in cur.fetchall() if r[0]]
    existing_norm = [normalize(e) for e in existing]

    inserted = 0
    dup_skipped = 0
    test_skipped = 0
    empty_skipped = 0

    for ct, text in reversed(bot_texts):  # 旧在前
        if not text:
            empty_skipped += 1
            continue
        if is_test_message(text):
            test_skipped += 1
            continue
        nn = normalize(text)
        if any(nn and difflib.SequenceMatcher(None, ee, nn).ratio() >= args.threshold for ee in existing_norm):
            dup_skipped += 1
            continue
        if not args.dry_run:
            cur.execute("""INSERT INTO kol_records
                (kol_name, platform, content, extracted_viewpoints, related_assets,
                 record_date, position_size, position_action, position_note)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                ("wu2198", "飞书群", text, "", "", ct, None, "", "飞书群自动同步"))
        existing_norm.append(nn)
        inserted += 1

    if not args.dry_run:
        conn.commit()
    total = cur.execute("SELECT COUNT(*) FROM kol_records WHERE kol_name='wu2198'").fetchone()[0]
    conn.close()
    print("[3/4] 入库完成")

    # 4. GitHub 推送
    push_ok = None
    if not args.dry_run and inserted > 0 and not args.no_push:
        print("[4/4] 有新增，导出并推送到 GitHub ...")
        r = subprocess.run([sys.executable, SYNC_SCRIPT, "push"], capture_output=True, text=True, timeout=180)
        push_ok = r.returncode == 0
        tail = (r.stdout + r.stderr).strip().splitlines()
        for line in tail[-6:]:
            print("      " + line)
    else:
        print("[4/4] 跳过推送（dry-run=%s, inserted=%d, no-push=%s）"
              % (args.dry_run, inserted, args.no_push))

    # 5. 报告
    print("\n" + "=" * 56)
    print("  同步结果报告")
    print("=" * 56)
    print("  新增导入条数: %d" % inserted)
    print("  跳过条数: %d（重复相似度>%.0f%%: %d / 测试消息: %d / 空消息: %d）"
          % (dup_skipped + test_skipped + empty_skipped, args.threshold * 100,
             dup_skipped, test_skipped, empty_skipped))
    print("  数据库 wu2198 总条数: %d" % total)
    if push_ok is not None:
        print("  GitHub 推送: %s" % ("成功" if push_ok else "失败（见上方日志）"))
    print("=" * 56)


if __name__ == "__main__":
    main()

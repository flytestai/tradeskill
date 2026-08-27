#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wu2198 五号群 消息自动同步（合并版：飞书群拉取 + 大V发言分析）

功能：
  1. 通过 lark-cli（OAuth 用户授权）拉取 wu2198五号群 的机器人消息（即 wu2198 的发言）
  2. 增量拉取：只记住「最后一次拉取的群消息时间」，仅拉取该时间之后的新消息
  3. 按 97% 文本相似度去重后，增量导入 kol-opinion-analyzer 的 SQLite 数据库
  4. 测试消息自动跳过
  5. 报告本次同步结果（新增导入条数、跳过条数）
  6. 有新增时自动导出 JSON 并推送到 GitHub

盘中时间定义：交易日 9:00-11:30 / 13:00-15:00（9:00-9:30 也算盘中），
另在盘后 16:00 兜底同步一次；其余时间自动跳过。

用法:
  python sync_feishu_auto.py                # 正常同步（盘中 + 交易日守卫）
  python sync_feishu_auto.py --force        # 忽略盘中/交易日守卫，强制同步
  python sync_feishu_auto.py --no-push      # 不同步到 GitHub
  python sync_feishu_auto.py --dry-run      # 只预览，不写库不推送
  python sync_feishu_auto.py --reset-watermark  # 重置增量水位（下次重新全量拉取）
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
STATE_PATH = os.path.join(SKILL_DIR, "sync", "feishu_sync_state.json")

DEFAULT_CHAT_ID = "oc_59301fc3e11c6e131f31ffb8acd4125a"
BOT_SENDER_TYPES = ("app", "bot")          # 机器人消息（wu2198 发言由自定义机器人发出）
SIM_THRESHOLD = 0.97                        # 文本相似度去重阈值
TEST_KEYWORDS = ["转发测试", "同步测试", "设备A同步测试", "test", "TEST"]


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
    """交易日 + 盘中/盘后时间守卫（北京时间）。返回 (是否可运行, 原因)"""
    now = datetime.now(timezone(timedelta(hours=8)))
    if now.weekday() >= 5:
        return False, "非交易日（周末）"
    hm = now.hour * 100 + now.minute
    # 盘中 9:00-11:30（9:00-9:30 也算盘中）/ 13:00-15:00，盘后 16:00 兜底一次
    if (900 <= hm <= 1130) or (1300 <= hm <= 1500) or (1555 <= hm <= 1605):
        return True, ""
    return False, "非盘中/盘后时间（当前 %02d:%02d）" % (now.hour, now.minute)


def to_iso(ts):
    """'YYYY-MM-DD HH:MM' -> ISO8601（北京时间）"""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M")
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"
    except Exception:
        return None


def pull_latest():
    """从 GitHub 拉取最新同步数据（含水位），失败不阻塞后续同步"""
    try:
        r = subprocess.run(["git", "-C", SKILL_DIR, "pull", "--rebase"],
                           capture_output=True, text=True, timeout=90)
        if r.returncode == 0:
            print("[PULL] 已拉取最新同步数据（含水位）")
        else:
            msg = (r.stderr or r.stdout).strip().splitlines()
            print("[PULL] 拉取失败（继续使用本地水位）: %s" % (msg[-1][:120] if msg else "unknown"))
    except Exception as e:
        print("[PULL] 拉取异常（继续使用本地水位）: %s" % e)


def load_watermark(db_path):
    """加载增量水位：优先 sync/ 状态文件，回退到 DB 中飞书群最新记录时间，再回退 None"""
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            t = d.get("last_message_time")
            if t:
                return t
        except Exception:
            pass
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT MAX(record_date) FROM kol_records WHERE kol_name='wu2198' AND platform='飞书群'")
            t = cur.fetchone()[0]
            conn.close()
            if t:
                return t
        except Exception:
            pass
    return None


def save_watermark(t):
    """保存增量水位（最后拉取的群消息时间）"""
    if not t:
        return
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"last_message_time": t}, f, ensure_ascii=False)
    except Exception as e:
        print("[WARN] 保存水位失败: %s" % e)


def fetch_messages_since(lark_cli, chat_id, start_iso=None):
    """通过 lark-cli 拉取 start_iso 之后的消息（升序，自动分页）"""
    cmd = [
        lark_cli, "im", "+chat-messages-list",
        "--chat-id", chat_id,
        "--as", "user",
        "--order", "asc",
        "--page-all",
        "--page-limit", "1000",
        "--no-reactions",
        "--json",
    ]
    if start_iso:
        cmd += ["--start", start_iso]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
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
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--force", action="store_true", help="忽略盘中/交易日守卫")
    ap.add_argument("--no-push", action="store_true", help="跳过 GitHub 推送")
    ap.add_argument("--dry-run", action="store_true", help="只预览不写库")
    ap.add_argument("--reset-watermark", action="store_true", help="重置增量水位，下次全量拉取")
    ap.add_argument("--no-pull", action="store_true", help="同步前不拉取最新水位")
    args = ap.parse_args()

    if args.reset_watermark:
        if os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)
            print("[OK] 已重置增量水位（下次将全量拉取）")
        else:
            print("[INFO] 无水位文件，无需重置")
        return

    if not args.force:
        ok, reason = trading_time_guard()
        if not ok:
            print("[SKIP] %s" % reason)
            return

    lark_cli = find_lark_cli()
    if not args.no_pull:
        pull_latest()
    watermark = load_watermark(args.db)
    start_iso = to_iso(watermark) if watermark else None

    print("=" * 56)
    print("  wu2198五号群 消息自动同步")
    print("  时间: %s" % datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"))
    print("  群ID: %s" % args.chat_id)
    print("  增量水位: %s" % (watermark or "无（全量拉取）"))
    print("=" * 56)

    # 1. 只拉取水位之后的消息
    messages = fetch_messages_since(lark_cli, args.chat_id, start_iso)
    if messages is None:
        print("[FAIL] 拉取失败，本轮结束")
        sys.exit(1)
    print("[1/5] 拉到 %d 条新消息" % len(messages))

    # 2. 计算新水位 = 所有拉取消息的最大 create_time
    new_watermark = watermark
    if messages:
        times = [m.get("create_time", "") for m in messages if m.get("create_time")]
        if times:
            latest = max(times)
            if not new_watermark or latest > new_watermark:
                new_watermark = latest

    # 3. 过滤机器人消息 + 提取文本
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
    print("[3/5] 机器人文本消息 %d 条" % len(bot_texts))

    # 4. 97% 去重 + 入库
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
            vip = 1 if "仅TA的真爱粉可见" in text else 0
            cur.execute("""INSERT INTO kol_records
                (kol_name, platform, content, extracted_viewpoints, related_assets,
                 record_date, position_size, position_action, position_note, is_vip)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ("wu2198", "飞书群", text, "", "", ct, None, "", "飞书群自动同步", vip))
        existing_norm.append(nn)
        inserted += 1

    if not args.dry_run:
        conn.commit()
        # 5. 保存新水位（记录本次拉取到的最新群消息时间）
        if new_watermark:
            save_watermark(new_watermark)
        if new_watermark != watermark:
            print("[5/5] 水位已更新: %s -> %s" % (watermark or "无", new_watermark))
        else:
            print("[5/5] 无新消息，水位保持: %s" % (new_watermark or "无"))
    total = cur.execute("SELECT COUNT(*) FROM kol_records WHERE kol_name='wu2198'").fetchone()[0]
    conn.close()
    print("[4/5] 入库完成")

    # 6. GitHub 推送（有新记录，或水位前移时也推送，保证多设备水位一致）
    watermark_advanced = bool(new_watermark and new_watermark != watermark)
    push_ok = None
    if not args.dry_run and not args.no_push and (inserted > 0 or watermark_advanced):
        print("[6/6] 有新增，导出并推送到 GitHub ...")
        r = subprocess.run([sys.executable, SYNC_SCRIPT, "push"], capture_output=True, text=True, timeout=180)
        push_ok = r.returncode == 0
        tail = (r.stdout + r.stderr).strip().splitlines()
        for line in tail[-6:]:
            print("      " + line)
    else:
        print("[6/6] 跳过推送（dry-run=%s, inserted=%d, no-push=%s）"
              % (args.dry_run, inserted, args.no_push))

    # 7. 报告
    print("\n" + "=" * 56)
    print("  同步结果报告")
    print("=" * 56)
    print("  增量水位: %s" % (watermark or "无"))
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

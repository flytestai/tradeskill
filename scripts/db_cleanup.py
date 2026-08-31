#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库清理：精确去重 + 删除测试消息 + 删除占位符（可重复运行，幂等）

用法:
  python db_cleanup.py [--kol-name wu2198] [--dry-run]
"""
import argparse
import os
import sqlite3

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")

# 占位符（图片/卡片/文件等非文本内容）
PLACEHOLDERS = ("[图片消息]", "[互动卡片]", "[图片]", "[文件]", "[语音]", "[视频]")


def cleanup(db_path, kol_name, dry_run=False):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    before = cur.execute("SELECT COUNT(*) FROM kol_records WHERE kol_name=?", (kol_name,)).fetchone()[0]

    # 1. 删测试消息
    test_del = cur.execute(
        """DELETE FROM kol_records WHERE kol_name=? AND (
            content LIKE '%转发测试%' OR content LIKE '%同步测试%' OR content LIKE '%设备A同步测试%'
        )""", (kol_name,)).rowcount

    # 2. 删占位符
    ph_del = cur.execute(
        "DELETE FROM kol_records WHERE kol_name=? AND content IN (%s)"
        % ",".join("?" * len(PLACEHOLDERS)), (kol_name, *PLACEHOLDERS)).rowcount

    # 3. 精确去重（每段 content 保留最早 id）
    dup_del = cur.execute(
        """DELETE FROM kol_records WHERE kol_name=? AND id NOT IN (
            SELECT MIN(id) FROM kol_records WHERE kol_name=? GROUP BY content
        )""", (kol_name, kol_name)).rowcount

    if not dry_run:
        conn.commit()
    after = cur.execute("SELECT COUNT(*) FROM kol_records WHERE kol_name=?", (kol_name,)).fetchone()[0]
    conn.close()

    tag = "[DRY RUN]" if dry_run else ""
    print("%s 清理前 %d 条" % (tag, before))
    print("%s 删除：测试消息 %d / 占位符 %d / 完全重复 %d" % (tag, test_del, ph_del, dup_del))
    print("%s 清理后 %d 条" % (tag, after))
    return after


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="清理 KOL 数据库（去重+删测试+删占位）")
    ap.add_argument("--kol-name", default="wu2198")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--dry-run", action="store_true", help="只预览不执行")
    args = ap.parse_args()
    cleanup(args.db, args.kol_name, args.dry_run)

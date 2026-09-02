#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 wu2198 当天发言 dump 成纯文本，供蜜蜂任务做 AI 一句话总结。"""
import argparse
import os
import re
from datetime import datetime, timezone, timedelta

from common import connect_db

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")


def main():
    ap = argparse.ArgumentParser(description="dump wu2198 当天发言")
    ap.add_argument("--lunch", action="store_true", help="只取 11:35 前（午间）")
    ap.add_argument("--limit", type=int, default=8, help="最多输出条数")
    args = ap.parse_args()

    now = datetime.now(timezone(timedelta(hours=8)))
    day = now.strftime("%Y-%m-%d")

    conn = connect_db(DB_PATH)
    cur = conn.cursor()
    sql = "select content from kol_records where kol_name='wu2198' and record_date like ?"
    params = [day + "%"]
    if args.lunch:
        sql += " and record_date <= ?"
        params.append(day + " 11:35")
    sql += " order by record_date asc"
    rows = [r[0] for r in cur.execute(sql, params)]
    conn.close()

    texts = []
    for t in rows:
        t = re.sub(r"【仅TA的真爱粉可见】", "", t or "")
        t = re.sub(r"^\s*@?wu2198\s*", "", t, flags=re.I)
        t = re.sub(r"(明白666|收到请回复|收到回复|明白)\s*$", "", t, flags=re.I)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            texts.append(t)

    for t in texts[-args.limit:]:
        print(t)


if __name__ == "__main__":
    main()

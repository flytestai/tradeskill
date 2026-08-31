#!/usr/bin/env python3
"""
批量导入大V言论（增量写入，解决SQLite锁死问题）

用法:
  python db_batch.py --json data.json           # 从JSON文件批量导入
  python db_batch.py --text records.txt         # 从文本文件导入（每行一条）
  python db_batch.py --dry-run --json data.json # 预览不执行

JSON格式（数组）:
  [
    {"kol_name":"wu2198","platform":"微博","content":"...","related_assets":"...",
     "record_date":"2026-08-14 10:00","position_size":2,"position_action":"持有","position_note":"..."},
    ...
  ]

文本格式（每行一条，Tab分隔）:
  kol_name\tplatform\tcontent\trelated_assets\trecord_date\tposition_size\tposition_action\tposition_note
"""
import sqlite3, json, os, sys, argparse
from datetime import datetime

from records_hash import content_hash

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")

def connect_db():
    """带WAL模式和busy_timeout的连接，避免锁死"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")       # WAL模式：读不阻塞写
    conn.execute("PRAGMA busy_timeout=5000")      # 忙时等待5秒
    conn.execute("PRAGMA synchronous=NORMAL")     # 提升写入性能
    return conn

def insert_records(records, dry_run=False):
    """增量INSERT，按(kol_name, record_date, content前200字)去重"""
    conn = connect_db()
    cur = conn.cursor()

    # 只查指纹字段，不查全表（按 content_hash 去重）
    cur.execute("SELECT kol_name, content_hash FROM kol_records")
    existing = {(r[0], r[1]) for r in cur.fetchall() if r[1]}

    inserted, skipped = 0, 0
    for r in records:
        h = content_hash(r.get("content",""), r.get("image_path",""))
        if (r.get("kol_name",""), h) in existing:
            skipped += 1
            continue
        if dry_run:
            print(f"  [预览] {r.get('record_date')} | {r.get('content','')[:50]}")
            inserted += 1
            continue

        cur.execute("""INSERT INTO kol_records
            (kol_name, platform, content, extracted_viewpoints, related_assets,
             record_date, position_size, position_action, position_note, content_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (
            r.get("kol_name",""), r.get("platform",""),
            r.get("content",""), r.get("extracted_viewpoints",""),
            r.get("related_assets",""), r.get("record_date",""),
            r.get("position_size"), r.get("position_action",""),
            r.get("position_note",""), h))
        existing.add((r.get("kol_name",""), h))
        inserted += 1

    conn.commit()
    conn.close()
    return inserted, skipped

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "kol_records" in data:
        return data["kol_records"]
    if isinstance(data, list):
        return data
    raise ValueError("无法识别的JSON格式")

def load_text(path):
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 5:
                print(f"  跳过（字段不足）: {line[:50]}")
                continue
            r = {
                "kol_name": parts[0], "platform": parts[1] if len(parts)>1 else "",
                "content": parts[2] if len(parts)>2 else "",
                "related_assets": parts[3] if len(parts)>3 else "",
                "record_date": parts[4] if len(parts)>4 else "",
            }
            if len(parts) > 5: r["position_size"] = int(parts[5]) if parts[5] else None
            if len(parts) > 6: r["position_action"] = parts[6]
            if len(parts) > 7: r["position_note"] = parts[7]
            records.append(r)
    return records

def main():
    parser = argparse.ArgumentParser(description="批量导入大V言论（增量写入）")
    parser.add_argument("--json", help="JSON文件路径")
    parser.add_argument("--text", help="文本文件路径（Tab分隔）")
    parser.add_argument("--dry-run", action="store_true", help="预览不执行")
    args = parser.parse_args()

    if args.json:
        records = load_json(args.json)
    elif args.text:
        records = load_text(args.text)
    else:
        print("请指定 --json 或 --text")
        sys.exit(1)

    print(f"[INFO] 共 {len(records)} 条待导入")
    inserted, skipped = insert_records(records, args.dry_run)

    if args.dry_run:
        print(f"[DRY-RUN] 将导入 {inserted} 条，跳过 {skipped} 条（重复）")
    else:
        print(f"[OK] 导入 {inserted} 条，跳过 {skipped} 条（重复）")

if __name__ == "__main__":
    main()

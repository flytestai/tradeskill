#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 sync/ 下的数据文件导入 → SQLite 数据库（幂等，按 content_hash 去重）

用法:
  python db_import.py                  # 导入 sync/records.jsonl（缺失则回退 kol_records.json）
  python db_import.py --file x.jsonl   # 指定文件
  python db_import.py --dry-run        # 预览不执行
"""
import sqlite3, json, os, sys, argparse

from records_hash import content_hash

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")
SYNC_DIR = os.path.join(SKILL_DIR, "sync")
JSONL_PATH = os.path.join(SYNC_DIR, "records.jsonl")
OLD_JSON_PATH = os.path.join(SYNC_DIR, "kol_records.json")


def _load_records(path):
    """从文件加载记录：JSONL 逐行解析，或 JSON（dict/list）。"""
    records = []
    if not os.path.exists(path):
        return records
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if path.endswith(".jsonl"):
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[ERROR] 无法解析 {path}")
        return records
    if isinstance(data, dict) and "kol_records" in data:
        return data["kol_records"]
    if isinstance(data, list):
        return data
    return records


def import_from_file(path, dry_run=False):
    records = _load_records(path)
    if not records:
        print(f"[INFO] 无记录可导入: {path}")
        return False

    if dry_run:
        print(f"[DRY-RUN] 将导入 {len(records)} 条:")
        for r in records[:10]:
            print(f"  {r.get('record_date','')} | {r.get('kol_name','')} | {(r.get('content','') or '')[:50]}")
        if len(records) > 10:
            print(f"  ... 以及 {len(records)-10} 条")
        return True

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT kol_name, content_hash FROM kol_records")
    existing = {(r[0], r[1]) for r in cur.fetchall() if r[1]}

    inserted, skipped = 0, 0
    for r in records:
        h = content_hash(r.get("content", ""), r.get("image_path", ""))
        if (r.get("kol_name", ""), h) in existing:
            skipped += 1
            continue
        cur.execute("""
            INSERT INTO kol_records (kol_name, platform, content, extracted_viewpoints,
                                     related_assets, record_date, position_size,
                                     position_action, position_note, image_path, is_vip, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            r.get("kol_name", ""),
            r.get("platform", ""),
            r.get("content", ""),
            r.get("extracted_viewpoints", ""),
            r.get("related_assets", ""),
            r.get("record_date", ""),
            r.get("position_size"),
            r.get("position_action", ""),
            r.get("position_note", ""),
            r.get("image_path", ""),
            r.get("is_vip", 0),
            h,
        ))
        existing.add((r.get("kol_name", ""), h))
        inserted += 1

    conn.commit()
    conn.close()
    print(f"[OK] Imported: {inserted} new, {skipped} skipped")
    return True


def main():
    parser = argparse.ArgumentParser(description="Import KOL data from sync file (git-synced)")
    parser.add_argument("--file", default=None, help="Specific JSONL/JSON file to import")
    parser.add_argument("--dry-run", action="store_true", help="Preview without importing")
    args = parser.parse_args()

    if args.file:
        path = args.file
    elif os.path.exists(JSONL_PATH):
        path = JSONL_PATH
    else:
        path = OLD_JSON_PATH

    import_from_file(path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

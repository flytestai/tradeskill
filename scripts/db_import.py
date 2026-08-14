#!/usr/bin/env python3
"""
从 sync/ 目录下的 JSON 文件导入 → SQLite 数据库（幂等操作）

用法:
  python db_import.py                  # 导入 sync/kol_records.json
  python db_import.py --dry-run        # 预览不执行
"""
import sqlite3, json, os, sys, argparse
from datetime import datetime

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")
SYNC_DIR = os.path.join(SKILL_DIR, "sync")

def import_from_json(json_path, dry_run=False):
    if not os.path.exists(json_path):
        print(f"[ERROR] File not found: {json_path}")
        print(f"  Run 'python db_export.py' on source device first, then git push/pull.")
        return False

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Support both formats: full export {"kol_records": [...], ...} or single KOL [...]
    if isinstance(data, dict) and "kol_records" in data:
        records = data["kol_records"]
        exported_at = data.get("exported_at", "unknown")
    elif isinstance(data, list):
        records = data
        exported_at = "unknown"
    else:
        print(f"[ERROR] Unrecognized JSON format in {json_path}")
        return False

    print(f"[INFO] JSON exported at: {exported_at}")
    print(f"[INFO] Records in file: {len(records)}")

    if dry_run:
        print("[DRY-RUN] Would import the following:")
        for r in records[:10]:
            print(f"  {r['record_date']} | {r['kol_name']} | {r['content'][:60]}...")
        if len(records) > 10:
            print(f"  ... and {len(records)-10} more")
        return True

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Get existing records for dedup
    cur.execute("SELECT kol_name, record_date, content FROM kol_records")
    existing = set()
    for row in cur.fetchall():
        # Use first 200 chars of content as fingerprint
        existing.add((row[0], row[1], row[2][:200] if row[2] else ""))

    inserted, skipped = 0, 0
    for r in records:
        fingerprint = (r.get("kol_name",""), r.get("record_date",""), r.get("content","")[:200])
        if fingerprint in existing:
            skipped += 1
            continue

        cur.execute("""
            INSERT INTO kol_records (kol_name, platform, content, extracted_viewpoints,
                                     related_assets, record_date, position_size,
                                     position_action, position_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        ))
        inserted += 1

    conn.commit()
    conn.close()

    print(f"[OK] Imported: {inserted} new, {skipped} skipped (already exist)")
    return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import KOL data from JSON (git-synced)")
    parser.add_argument("--file", default=None, help="Specific JSON file to import")
    parser.add_argument("--dry-run", action="store_true", help="Preview without importing")
    args = parser.parse_args()

    if args.file:
        json_path = args.file
    else:
        json_path = os.path.join(SYNC_DIR, "kol_records.json")

    import_from_json(json_path, dry_run=args.dry_run)

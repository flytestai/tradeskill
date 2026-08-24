#!/usr/bin/env python3
"""
导出 SQLite 数据库 → sync/ 目录下的 JSON 文件（文本格式，适合 git 追踪）

用法:
  python db_export.py                  # 导出全部记录
  python db_export.py --kol wu2198     # 只导出指定大V
"""
import sqlite3, json, os, sys, argparse
from datetime import datetime

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")
SYNC_DIR = os.path.join(SKILL_DIR, "sync")

def export_all(output_dir):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Export kol_records
    cur.execute("SELECT * FROM kol_records ORDER BY record_date DESC")
    records = [dict(r) for r in cur.fetchall()]

    # Export analysis_reports（表可能不存在，容错）
    reports = []
    try:
        cur.execute("SELECT * FROM analysis_reports ORDER BY created_at DESC")
        reports = [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        pass  # 表不存在，跳过

    conn.close()

    os.makedirs(output_dir, exist_ok=True)

    # Main data file
    data = {
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_records": len(records),
        "total_reports": len(reports),
        "kol_records": records,
        "analysis_reports": reports,
    }

    path = os.path.join(output_dir, "kol_records.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Metadata
    meta = {
        "last_export": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "record_count": len(records),
        "kols": list(set(r["kol_name"] for r in records)),
    }
    with open(os.path.join(output_dir, "sync_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"[OK] Exported {len(records)} records, {len(reports)} reports → {path}")
    print(f"     KOLs: {meta['kols']}")
    return path

def export_kol(kol_name, output_dir):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM kol_records WHERE kol_name=? ORDER BY record_date DESC", (kol_name,))
    records = [dict(r) for r in cur.fetchall()]
    conn.close()

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"kol_{kol_name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print(f"[OK] Exported {len(records)} records for '{kol_name}' → {path}")
    return path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export KOL database to JSON for git sync")
    parser.add_argument("--kol", help="Export single KOL only")
    args = parser.parse_args()

    if args.kol:
        export_kol(args.kol, SYNC_DIR)
    else:
        export_all(SYNC_DIR)

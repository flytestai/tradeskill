#!/usr/bin/env python3
"""
JSONL 追加式同步 — 解决多设备同时写入的 git 冲突

原理：
  每条记录是 JSONL 文件中的一行，追加式写入。
  历史行永不修改，多人同时追加不同行 → git 自动合并，无冲突。

用法:
  python sync_jsonl.py export     # 从DB追加新记录到 sync/records.jsonl
  python sync_jsonl.py import     # 从 sync/records.jsonl 导入新记录到DB
  python sync_jsonl.py migrate    # 从旧的 kol_records.json 迁移到 JSONL
"""
import sqlite3, json, os, sys
from datetime import datetime

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")
JSONL_PATH = os.path.join(SKILL_DIR, "sync", "records.jsonl")
OLD_JSON_PATH = os.path.join(SKILL_DIR, "sync", "kol_records.json")

FIELDS = ["kol_name","platform","content","extracted_viewpoints",
          "related_assets","record_date","position_size",
          "position_action","position_note"]

def fingerprint(r):
    """记录指纹：kol_name + record_date + content前200字"""
    return (r.get("kol_name",""), r.get("record_date",""),
            (r.get("content","") or "")[:200])

def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn

def read_jsonl(path=JSONL_PATH):
    """读取 JSONL 所有行（容错：跳过坏行）"""
    records = []
    if not os.path.exists(path):
        return records
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 跳过坏行
    return records

def export():
    """从DB追加新记录到 JSONL（只追加，不重写历史行）"""
    conn = connect_db()
    db_rows = conn.execute(f"SELECT {','.join(FIELDS)} FROM kol_records ORDER BY record_date").fetchall()
    conn.close()
    db_records = [dict(r) for r in db_rows]

    # 读取现有 JSONL 的指纹
    existing_records = read_jsonl()
    existing_fps = set(fingerprint(r) for r in existing_records)

    # 找出 DB 中有但 JSONL 中没有的记录
    new_records = [r for r in db_records if fingerprint(r) not in existing_fps]

    if new_records:
        os.makedirs(os.path.dirname(JSONL_PATH), exist_ok=True)
        with open(JSONL_PATH, "a", encoding="utf-8") as f:
            for r in new_records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"[EXPORT] 追加 {len(new_records)} 条新记录 → sync/records.jsonl")
    else:
        print(f"[EXPORT] 无新记录（JSONL 已有 {len(existing_records)} 条）")
    return len(new_records)

def import_():
    """从 JSONL 导入新记录到 DB（指纹去重，幂等）"""
    jsonl_records = read_jsonl()
    if not jsonl_records:
        print("[IMPORT] JSONL 为空或不存在")
        return 0

    conn = connect_db()
    cur = conn.cursor()
    cur.execute(f"SELECT {','.join(FIELDS)} FROM kol_records")
    existing_fps = set(fingerprint(dict(r)) for r in cur.fetchall())

    inserted, skipped = 0, 0
    for r in jsonl_records:
        fp = fingerprint(r)
        if fp in existing_fps:
            skipped += 1
            continue
        cur.execute(f"""INSERT INTO kol_records
            ({','.join(FIELDS)}) VALUES ({','.join('?'*len(FIELDS))})""",
            tuple(r.get(f, "") if f != "position_size" else r.get("position_size")
                  for f in FIELDS))
        existing_fps.add(fp)
        inserted += 1

    conn.commit()
    conn.close()
    print(f"[IMPORT] 导入 {inserted} 条，跳过 {skipped} 条（已存在）")
    return inserted

def migrate():
    """从旧的 kol_records.json 迁移到 JSONL"""
    if not os.path.exists(OLD_JSON_PATH):
        print("[MIGRATE] 旧 JSON 不存在，跳过")
        return

    with open(OLD_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    records = data.get("kol_records", data if isinstance(data, list) else [])

    # 追加到 JSONL（如果 JSONL 还没有这些记录）
    existing = read_jsonl()
    existing_fps = set(fingerprint(r) for r in existing)
    new_records = [r for r in records if fingerprint(r) not in existing_fps]

    if new_records:
        os.makedirs(os.path.dirname(JSONL_PATH), exist_ok=True)
        with open(JSONL_PATH, "a", encoding="utf-8") as f:
            for r in new_records:
                f.write(json.dumps({k: r.get(k, "") for k in FIELDS}, ensure_ascii=False) + "\n")
        print(f"[MIGRATE] 迁移 {len(new_records)} 条到 JSONL")
    else:
        print(f"[MIGRATE] 无需迁移（JSONL 已有全部记录）")

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "export"
    {
        "export": export,
        "import": import_,
        "migrate": migrate,
    }[action]()

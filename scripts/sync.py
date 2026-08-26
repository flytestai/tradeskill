#!/usr/bin/env python3
"""
云端同步控制器 — 统一管理 push（上传）和 pull（下载）

用法:
  python sync.py push       # 导出 DB → JSON → git push
  python sync.py pull       # git pull → JSON → DB 导入
  python sync.py status     # 查看同步状态
"""
import sqlite3, json, os, sys, subprocess, argparse
from datetime import datetime

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNC_DIR = os.path.join(SKILL_DIR, "sync")
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")

def run(cmd, cwd=None):
    """Run shell command, return (ok, output)"""
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd or SKILL_DIR,
                          capture_output=True, text=True, timeout=30)
        return r.returncode == 0, r.stdout + r.stderr
    except Exception as e:
        return False, str(e)

def export_db():
    """Export SQLite → sync/kol_records.json"""
    if not os.path.exists(DB_PATH):
        print("[ERROR] Database not found. Run db_init.py first.")
        return False

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM kol_records ORDER BY record_date DESC").fetchall()
    records = [dict(r) for r in rows]
    conn.close()

    os.makedirs(SYNC_DIR, exist_ok=True)
    data = {
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_records": len(records),
        "kol_count": len(set(r["kol_name"] for r in records)),
        "kol_records": records,
    }
    path = os.path.join(SYNC_DIR, "kol_records.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[EXPORT] {len(records)} records → sync/kol_records.json")
    return True

def import_db():
    """Import sync/kol_records.json → SQLite (idempotent)"""
    path = os.path.join(SYNC_DIR, "kol_records.json")
    if not os.path.exists(path):
        print("[ERROR] sync/kol_records.json not found. Run 'python sync.py pull' first.")
        return False

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    records = data.get("kol_records", data if isinstance(data, list) else [])
    exported_at = data.get("exported_at", "unknown")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT kol_name, record_date, content FROM kol_records")
    existing = {(r[0], r[1], (r[2] or "")[:200]) for r in cur.fetchall()}

    inserted, skipped = 0, 0
    for r in records:
        fp = (r.get("kol_name",""), r.get("record_date",""), (r.get("content","") or "")[:200])
        if fp in existing:
            skipped += 1
            continue
        cur.execute("""INSERT INTO kol_records (kol_name,platform,content,extracted_viewpoints,
                       related_assets,record_date,position_size,position_action,position_note,
                       image_path,is_vip)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
            r.get("kol_name",""), r.get("platform",""), r.get("content",""),
            r.get("extracted_viewpoints",""), r.get("related_assets",""),
            r.get("record_date",""), r.get("position_size"),
            r.get("position_action",""), r.get("position_note",""),
            r.get("image_path",""), r.get("is_vip", 0)))
        inserted += 1

    conn.commit()
    conn.close()
    print(f"[IMPORT] {inserted} new, {skipped} skipped | Source: {exported_at}")
    return True

def git_push():
    """Git add sync/ + commit + push"""
    ok, out = run("git add sync/kol_records.json")
    if not ok:
        print(f"[GIT] add failed: {out}"); return False

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    ok, out = run(f'git commit -m "sync: {ts}"')
    # "nothing to commit" is not an error
    if not ok and "nothing to commit" not in out:
        print(f"[GIT] commit skipped: {out.strip()}")

    ok, out = run("git push")
    if ok:
        print(f"[GIT] pushed → remote")
    else:
        print(f"[GIT] push failed: {out.strip()}")
        print("  (可能需要配置 GitHub 凭证)")
    return ok

def git_pull():
    """Git pull latest sync data"""
    ok, out = run("git pull --rebase")
    if ok:
        print(f"[GIT] pulled from remote")
    else:
        print(f"[GIT] pull failed: {out.strip()}")
    return ok

def cmd_push():
    """Full push: export → git push"""
    if not export_db():
        return
    git_push()

def cmd_pull():
    """Full pull: git pull → import"""
    if not git_pull():
        print("[WARN] git pull failed, trying local import anyway...")
    import_db()

def cmd_status():
    """Show sync status"""
    # Local DB count
    if os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM kol_records")
        local = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT kol_name) FROM kol_records")
        kols = cur.fetchone()[0]
        cur.execute("SELECT MAX(record_date) FROM kol_records")
        latest = cur.fetchone()[0]
        conn.close()
    else:
        local, kols, latest = 0, 0, "N/A"

    # Sync file
    sync_path = os.path.join(SYNC_DIR, "kol_records.json")
    if os.path.exists(sync_path):
        with open(sync_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        sync_count = data.get("total_records", len(data.get("kol_records", [])))
        sync_time = data.get("exported_at", "unknown")
    else:
        sync_count, sync_time = 0, "not exported"

    # Git status
    ok, out = run("git status --short")
    git_status = "clean" if not out.strip() else f"{len(out.strip().split(chr(10)))} files changed"

    print(f"""
  📊 同步状态
  ┌─────────────┬──────────────────┐
  │ 本地数据库   │ {local} 条记录, {kols} 位大V │
  │ 最新发言     │ {latest}           │
  │ 同步文件     │ {sync_count} 条          │
  │ 上次导出     │ {sync_time}       │
  │ Git 状态     │ {git_status}              │
  └─────────────┴──────────────────┘
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KOL DB cloud sync controller")
    parser.add_argument("action", choices=["push", "pull", "status"],
                       help="push=export+upload, pull=download+import, status=show info")
    args = parser.parse_args()

    {
        "push": cmd_push,
        "pull": cmd_pull,
        "status": cmd_status,
    }[args.action]()

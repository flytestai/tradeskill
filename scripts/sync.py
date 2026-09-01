#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端同步控制器 — 统一管理 push（上传）和 pull（下载）

数据文件：sync/records.jsonl（追加式 JSONL，每条记录一行）。
历史行永不修改，每次同步只追加新行 → git 只产生增量 diff，仓库不膨胀，多设备不易冲突。

用法:
  python sync.py push       # git pull → 导入 → 增量导出 → git push
  python sync.py pull       # git pull → 导入
  python sync.py export     # 增量导出 DB → records.jsonl（不推送）
  python sync.py import     # 从 records.jsonl 导入到 DB（不拉取）
  python sync.py status     # 查看同步状态
"""
import sqlite3, json, os, sys, subprocess, argparse, time
from datetime import datetime

from records_hash import content_hash

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNC_DIR = os.path.join(SKILL_DIR, "sync")
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")
JSONL_PATH = os.path.join(SYNC_DIR, "records.jsonl")
OLD_JSON_PATH = os.path.join(SYNC_DIR, "kol_records.json")
META_PATH = os.path.join(SYNC_DIR, "sync_meta.json")

FIELDS = ["kol_name", "platform", "content", "extracted_viewpoints",
          "related_assets", "record_date", "position_size",
          "position_action", "position_note", "image_path", "is_vip", "content_hash"]


def run(cmd, cwd=None):
    """Run shell command, return (ok, output)"""
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd or SKILL_DIR,
                           capture_output=True, text=True, timeout=180)
        return r.returncode == 0, r.stdout + r.stderr
    except Exception as e:
        return False, str(e)


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_hash_column(conn):
    """确保 content_hash 列存在（老库兼容；索引与回填由 db_init.py 负责）"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(kol_records)").fetchall()]
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE kol_records ADD COLUMN content_hash TEXT")
        conn.commit()


def _hash_of(r):
    """取记录哈希：优先用已有 content_hash，否则按正文/image_path 现算。"""
    h = r.get("content_hash")
    if h:
        return h
    return content_hash(r.get("content", ""), r.get("image_path", ""))


def _read_jsonl_records():
    """读取 records.jsonl 所有行（容错跳过坏行）；不存在返回 []。"""
    records = []
    if not os.path.exists(JSONL_PATH):
        return records
    with open(JSONL_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _read_old_json_records():
    """兼容旧格式：读 sync/kol_records.json（迁移过渡期用）。"""
    if not os.path.exists(OLD_JSON_PATH):
        return []
    try:
        with open(OLD_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("kol_records", data if isinstance(data, list) else [])
    except Exception:
        return []


def _ensure_trailing_newline():
    """避免追加时与上一行粘连。"""
    if not os.path.exists(JSONL_PATH) or os.path.getsize(JSONL_PATH) == 0:
        return
    with open(JSONL_PATH, "rb") as f:
        f.seek(-1, 2)
        last = f.read(1)
    if last != b"\n":
        with open(JSONL_PATH, "a", encoding="utf-8") as f:
            f.write("\n")


def _write_meta():
    count = 0
    if os.path.exists(JSONL_PATH):
        with open(JSONL_PATH, "r", encoding="utf-8") as f:
            count = sum(1 for line in f if line.strip())
    meta = {
        "last_export": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "record_count": count,
    }
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def export_incremental():
    """增量导出：只把 DB 中 content_hash 不在 records.jsonl 的记录追加写入。"""
    if not os.path.exists(DB_PATH):
        print("[ERROR] Database not found. Run db_init.py first.")
        return False

    conn = _connect()
    _ensure_hash_column(conn)
    rows = [dict(r) for r in conn.execute("SELECT * FROM kol_records ORDER BY id ASC").fetchall()]
    conn.close()

    existing = {_hash_of(r) for r in _read_jsonl_records()}
    os.makedirs(SYNC_DIR, exist_ok=True)
    _ensure_trailing_newline()

    appended = 0
    with open(JSONL_PATH, "a", encoding="utf-8") as f:
        for rec in rows:
            h = _hash_of(rec)
            if h in existing:
                continue
            line = {k: rec.get(k, "") for k in FIELDS}
            line["content_hash"] = h
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
            existing.add(h)
            appended += 1

    _write_meta()
    print(f"[EXPORT] 追加 {appended} 条 → sync/records.jsonl")
    return True


def import_incremental():
    """从 records.jsonl 导入 DB（content_hash 去重，幂等）；jsonl 缺失时回退旧 JSON。"""
    records = _read_jsonl_records()
    if not records:
        records = _read_old_json_records()
    if not records:
        print("[IMPORT] 无可导入数据（records.jsonl / kol_records.json 均不存在）")
        return 0

    conn = _connect()
    _ensure_hash_column(conn)
    cur = conn.cursor()
    existing = {(r[0], r[1]) for r in cur.execute(
        "SELECT kol_name, content_hash FROM kol_records").fetchall() if r[1]}

    inserted, skipped = 0, 0
    for r in records:
        h = _hash_of(r)
        if (r.get("kol_name", ""), h) in existing:
            skipped += 1
            continue
        cur.execute("""INSERT INTO kol_records
            (kol_name, platform, content, extracted_viewpoints, related_assets,
             record_date, position_size, position_action, position_note, image_path, is_vip, content_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
            r.get("kol_name", ""), r.get("platform", ""), r.get("content", ""),
            r.get("extracted_viewpoints", ""), r.get("related_assets", ""),
            r.get("record_date", ""), r.get("position_size"),
            r.get("position_action", ""), r.get("position_note", ""),
            r.get("image_path", ""), r.get("is_vip", 0), h))
        existing.add((r.get("kol_name", ""), h))
        inserted += 1
    conn.commit()
    conn.close()
    print(f"[IMPORT] 导入 {inserted} 条，跳过 {skipped} 条")
    return inserted


def git_push():
    """Git add sync/ + commit + push"""
    ok, out = run("git add sync/")
    if not ok:
        print(f"[GIT] add failed: {out}"); return False

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    ok, out = run(f'git commit -m "sync: {ts}" -- sync/')
    if not ok and "nothing to commit" not in out:
        print(f"[GIT] commit skipped: {out.strip()}")

    ok, out = run("git push")
    if ok:
        print(f"[GIT] pushed → remote")
    else:
        for i in range(1, 6):
            print(f"[GIT] push 失败，第 {i} 次重试（等 6 秒）...")
            time.sleep(6)
            ok, out = run("git push")
            if ok:
                print(f"[GIT] pushed → remote（第 {i} 次重试成功）")
                break
        if not ok:
            print(f"[GIT] push failed: {out.strip()}")
            print("  (可能需要配置 GitHub 凭证)")
    return ok


def git_pull():
    """Git pull latest sync data（--autostash 处理本地未提交改动）"""
    ok, out = run("git pull --rebase --autostash")
    if ok:
        print(f"[GIT] pulled from remote")
    else:
        print(f"[GIT] pull failed: {out.strip()}")
    return ok


def cmd_push():
    """Full push: 先拉取合并 → 导入 → 增量导出 → 推送"""
    git_pull()
    import_incremental()
    export_incremental()
    git_push()


def cmd_pull():
    """Full pull: git pull → 导入"""
    if not git_pull():
        print("[WARN] git pull failed, trying local import anyway...")
    import_incremental()


def cmd_export():
    export_incremental()


def cmd_import():
    import_incremental()


def cmd_rebuild():
    """从 DB 全量重建 records.jsonl（用于补全/迁移后一次性重新导出）。"""
    if not os.path.exists(DB_PATH):
        print("[ERROR] Database not found. Run db_init.py first.")
        return
    conn = _connect()
    _ensure_hash_column(conn)
    rows = [dict(r) for r in conn.execute("SELECT * FROM kol_records ORDER BY id ASC").fetchall()]
    conn.close()
    os.makedirs(SYNC_DIR, exist_ok=True)
    with open(JSONL_PATH, "w", encoding="utf-8") as f:
        for rec in rows:
            line = {k: rec.get(k, "") for k in FIELDS}
            line["content_hash"] = _hash_of(rec)
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    _write_meta()
    print(f"[REBUILD] 全量重建 records.jsonl：{len(rows)} 条")


def cmd_compact():
    """去重整理 records.jsonl：按 content_hash 去重并重写（多设备合并产生重复行时用）。"""
    if not os.path.exists(JSONL_PATH):
        print("[COMPACT] records.jsonl 不存在")
        return
    records = _read_jsonl_records()
    seen = set()
    unique = []
    removed = 0
    for r in records:
        h = _hash_of(r)
        if h in seen:
            removed += 1
            continue
        seen.add(h)
        unique.append(r)
    with open(JSONL_PATH, "w", encoding="utf-8") as f:
        for r in unique:
            line = {k: r.get(k, "") for k in FIELDS}
            line["content_hash"] = _hash_of(r)
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    _write_meta()
    print(f"[COMPACT] 保留 {len(unique)} 条，去除重复 {removed} 条")


def cmd_status():
    if os.path.exists(DB_PATH):
        conn = _connect()
        cur = conn.cursor()
        local = cur.execute("SELECT COUNT(*) FROM kol_records").fetchone()[0]
        kols = cur.execute("SELECT COUNT(DISTINCT kol_name) FROM kol_records").fetchone()[0]
        latest = cur.execute("SELECT MAX(record_date) FROM kol_records").fetchone()[0]
        conn.close()
    else:
        local, kols, latest = 0, 0, "N/A"

    sync_count = 0
    if os.path.exists(JSONL_PATH):
        with open(JSONL_PATH, "r", encoding="utf-8") as f:
            sync_count = sum(1 for line in f if line.strip())

    ok, out = run("git status --short")
    git_status = "clean" if not out.strip() else f"{len(out.strip().split(chr(10)))} files changed"

    print(f"""
  📊 同步状态
  ┌─────────────┬──────────────────┐
  │ 本地数据库   │ {local} 条记录, {kols} 位大V │
  │ 最新发言     │ {latest}           │
  │ 同步文件     │ {sync_count} 条（records.jsonl）│
  │ Git 状态     │ {git_status}              │
  └─────────────┴──────────────────┘
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KOL DB cloud sync controller")
    parser.add_argument("action", choices=["push", "pull", "export", "import", "compact", "rebuild", "status"],
                       help="push=merge+upload, pull=download+import, export/import/compact/rebuild=local only, status=show info")
    args = parser.parse_args()

    {
        "push": cmd_push,
        "pull": cmd_pull,
        "export": cmd_export,
        "import": cmd_import,
        "compact": cmd_compact,
        "rebuild": cmd_rebuild,
        "status": cmd_status,
    }[args.action]()

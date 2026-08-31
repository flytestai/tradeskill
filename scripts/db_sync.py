#!/usr/bin/env python3
"""
Auto-sync KOL data from JSON files to the database.
Detects new records by comparing record_date + kol_name, only inserts what's missing.

Usage:
  python db_sync.py                          # sync all JSON data files
  python db_sync.py --kol-name "wu2198"      # sync specific KOL only
  python db_sync.py --dry-run                # preview without inserting
"""

import sqlite3, os, json, sys

from records_hash import content_hash

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
DB_PATH = os.path.join(SKILL_DIR, 'data', 'kol_opinions.db')
DATA_DIR = SCRIPT_DIR  # JSON files sit next to scripts


def sync_json(conn, json_path: str, kol_name: str, dry_run: bool = False) -> dict:
    """同步/补全一个 JSON 文件：按 content_hash 去重；已存在则补全资产与仓位。"""
    with open(json_path, 'r', encoding='utf-8') as f:
        records = json.load(f)

    cur = conn.cursor()
    existing = {r[0]: r[1] for r in cur.execute(
        "SELECT content_hash, id FROM kol_records WHERE kol_name=?", (kol_name,)).fetchall() if r[0]}

    result = {'inserted': 0, 'enriched': 0, 'skipped': 0, 'errors': 0}
    for r in records:
        content = r['c']
        h = content_hash(content)
        assets = r.get('a', '')
        ps = r.get('ps')
        pa = r.get('pa', '')
        pn = r.get('pn', '')

        if h in existing:
            rid = existing[h]
            if dry_run:
                row = cur.execute(
                    "SELECT related_assets, position_size FROM kol_records WHERE id=?", (rid,)).fetchone()
                if (not row[0] and assets) or (row[1] is None and ps is not None):
                    result['enriched'] += 1
                else:
                    result['skipped'] += 1
                continue
            row = cur.execute(
                "SELECT related_assets, position_size FROM kol_records WHERE id=?", (rid,)).fetchone()
            sets, params = [], []
            if not row[0] and assets:
                sets.append("related_assets=?")
                params.append(assets)
            if row[1] is None and ps is not None:
                sets.extend(["position_size=?", "position_action=?", "position_note=?"])
                params.extend([ps, pa, pn])
            if sets:
                params.append(rid)
                cur.execute(f"UPDATE kol_records SET {', '.join(sets)} WHERE id=?", params)
                result['enriched'] += 1
            else:
                result['skipped'] += 1
            continue

        if dry_run:
            result['inserted'] += 1
            continue

        vps = [s.strip() for d in '。！？；' for s in content.split(d) if len(s.strip()) > 4]
        vp_text = '\n'.join(vps[:10])
        is_vip = 1 if "仅TA的真爱粉可见" in content else 0
        try:
            cur.execute(
                """INSERT INTO kol_records
                   (kol_name, platform, content, extracted_viewpoints, related_assets,
                    record_date, position_size, position_action, position_note, content_hash, is_vip)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (kol_name, "微博/公众号", content, vp_text, assets, r['t'], ps, pa, pn, h, is_vip)
            )
            result['inserted'] += 1
        except Exception as e:
            print(f"  [ERROR] {r['t']}: {e}", file=sys.stderr)
            result['errors'] += 1

    if not dry_run:
        conn.commit()
    return result


def find_json_files(kol_name: str = '') -> list:
    """Find all *_data.json files, optionally filtered by KOL name."""
    files = []
    prefix = f"{kol_name}_" if kol_name else ""
    for f in os.listdir(DATA_DIR):
        if f.endswith('_data.json') and f.startswith(prefix):
            files.append((f, f.replace('_data.json', '')))
    return files


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Sync JSON data files to SQLite database')
    parser.add_argument('--kol-name', help='Sync specific KOL only')
    parser.add_argument('--dry-run', action='store_true', help='Preview without inserting')
    args = parser.parse_args()

    if not os.path.exists(DB_PATH):
        print(f'[ERROR] Database not found: {DB_PATH}', file=sys.stderr)
        print('[HINT] Run db_init.py first.', file=sys.stderr)
        sys.exit(1)

    # Ensure required columns exist (per-column, ignore duplicates)
    conn = sqlite3.connect(DB_PATH)
    for stmt in [
        "ALTER TABLE kol_records ADD COLUMN position_size INTEGER DEFAULT NULL",
        "ALTER TABLE kol_records ADD COLUMN position_action TEXT DEFAULT ''",
        "ALTER TABLE kol_records ADD COLUMN position_note TEXT DEFAULT ''",
        "ALTER TABLE kol_records ADD COLUMN image_path TEXT DEFAULT ''",
        "ALTER TABLE kol_records ADD COLUMN content_hash TEXT",
        "ALTER TABLE kol_records ADD COLUMN is_vip INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.commit()

    json_files = find_json_files(args.kol_name or '')

    if not json_files:
        print('[INFO] No JSON data files found')
        conn.close()
        return

    tag = '[DRY RUN] ' if args.dry_run else ''
    total = {'inserted': 0, 'enriched': 0, 'skipped': 0, 'errors': 0}

    for filename, kol_name in json_files:
        path = os.path.join(DATA_DIR, filename)
        result = sync_json(conn, path, kol_name, args.dry_run)
        print(f'{tag}[{kol_name}] {filename}: {result["inserted"]} new, {result["enriched"]} enriched, {result["skipped"]} skipped, {result["errors"]} errors')
        for k in total:
            total[k] += result[k]

    conn.close()

    if args.dry_run:
        print(f'\n{tag}Would insert {total["inserted"]} new, enrich {total["enriched"]}, skip {total["skipped"]}')
    else:
        print(f'\n[OK] Done: {total["inserted"]} inserted, {total["enriched"]} enriched, {total["skipped"]} up-to-date, {total["errors"]} errors')


if __name__ == '__main__':
    main()

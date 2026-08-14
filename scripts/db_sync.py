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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
DB_PATH = os.path.join(SKILL_DIR, 'data', 'kol_opinions.db')
DATA_DIR = SCRIPT_DIR  # JSON files sit next to scripts


def get_existing_keys(conn, kol_name: str) -> set:
    """Get (kol_name, record_date) pairs already in DB."""
    rows = conn.execute(
        "SELECT kol_name, record_date FROM kol_records WHERE kol_name = ?",
        (kol_name,)
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


def sync_json(conn, json_path: str, kol_name: str, dry_run: bool = False) -> dict:
    """Sync one JSON file. Returns {inserted, skipped, errors}."""
    with open(json_path, 'r', encoding='utf-8') as f:
        records = json.load(f)

    existing = get_existing_keys(conn, kol_name)

    result = {'inserted': 0, 'skipped': 0, 'errors': 0}
    for r in records:
        key = (kol_name, r['t'])
        if key in existing:
            result['skipped'] += 1
            continue

        if dry_run:
            result['inserted'] += 1
            continue

        content = r['c']
        assets = r.get('a', '')
        ps = r.get('ps')
        pa = r.get('pa', '')
        pn = r.get('pn', '')

        vps = [s.strip() for d in '。！？；' for s in content.split(d) if len(s.strip()) > 4]
        vp_text = '\n'.join(vps[:10])

        try:
            conn.execute(
                """INSERT INTO kol_records
                   (kol_name, platform, content, extracted_viewpoints, related_assets,
                    record_date, position_size, position_action, position_note)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (kol_name, "微博/公众号", content, vp_text, assets, r['t'], ps, pa, pn)
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

    # Ensure position columns exist
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("ALTER TABLE kol_records ADD COLUMN position_size INTEGER DEFAULT NULL")
        conn.execute("ALTER TABLE kol_records ADD COLUMN position_action TEXT DEFAULT ''")
        conn.execute("ALTER TABLE kol_records ADD COLUMN position_note TEXT DEFAULT ''")
        conn.commit()
        print('[INFO] Added position tracking columns')
    except sqlite3.OperationalError:
        pass

    json_files = find_json_files(args.kol_name or '')

    if not json_files:
        print('[INFO] No JSON data files found')
        conn.close()
        return

    tag = '[DRY RUN] ' if args.dry_run else ''
    total = {'inserted': 0, 'skipped': 0, 'errors': 0}

    for filename, kol_name in json_files:
        path = os.path.join(DATA_DIR, filename)
        result = sync_json(conn, path, kol_name, args.dry_run)
        print(f'{tag}[{kol_name}] {filename}: {result["inserted"]} new, {result["skipped"]} skipped, {result["errors"]} errors')
        for k in total:
            total[k] += result[k]

    conn.close()

    if args.dry_run:
        print(f'\n{tag}Would insert {total["inserted"]} new records ({total["skipped"]} already in DB)')
    else:
        print(f'\n[OK] Done: {total["inserted"]} inserted, {total["skipped"]} up-to-date, {total["errors"]} errors')


if __name__ == '__main__':
    main()

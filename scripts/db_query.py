#!/usr/bin/env python3
"""
Query KOL opinion records from the database.
Default: last 30 days, latest first.

Usage:
  python db_query.py --kol-name "李大霄"                    # last 30 days
  python db_query.py --kol-name "李大霄" --days 7           # last 7 days
  python db_query.py --kol-name "李大霄" --all              # all time
  python db_query.py --kol-name "李大霄" --latest 5         # latest 5 in last 30 days
  python db_query.py --kol-name "李大霄" --with-position    # position history
  python db_query.py --list-kols                            # all KOLs
  python db_query.py --recent 20                            # recent 20 across all KOLs
  python db_query.py --kol-name "李大霄" --from "2026-07-01" --to "2026-08-05"
  python db_query.py --id 5
  python db_query.py --search "牛市"
"""

import sqlite3
import os
import sys
import argparse
import json
from datetime import datetime, timedelta


def get_default_db_path():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    skill_dir = os.path.dirname(script_dir)
    return os.path.join(skill_dir, 'data', 'kol_opinions.db')


def get_connection(db_path: str):
    if not os.path.exists(db_path):
        print(f'[ERROR] Database not found: {db_path}', file=sys.stderr)
        print('[HINT] Run db_init.py first to create the database.', file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def list_kols(conn) -> list:
    """List all distinct KOL names with record counts."""
    rows = conn.execute(
        """SELECT kol_name, COUNT(*) as cnt,
           MIN(record_date) as first_date, MAX(record_date) as last_date
           FROM kol_records GROUP BY kol_name ORDER BY cnt DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def query_by_kol(conn, kol_name: str, date_from: str = '', date_to: str = '',
                 limit: int = 0) -> list:
    """Query records for a specific KOL.
    Returns results ordered by record_date DESC (latest first)."""
    sql = "SELECT * FROM kol_records WHERE kol_name = ?"
    params = [kol_name]

    if date_from:
        sql += " AND record_date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND record_date <= ?"
        params.append(date_to)

    sql += " ORDER BY record_date DESC, created_at DESC"

    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def query_recent(conn, limit: int = 10) -> list:
    """Get the most recent records across all KOLs."""
    rows = conn.execute(
        "SELECT * FROM kol_records ORDER BY created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def query_by_id(conn, record_id: int) -> dict:
    """Get a single record by ID."""
    row = conn.execute(
        "SELECT * FROM kol_records WHERE id = ?", (record_id,)
    ).fetchone()
    if row:
        result = dict(row)
        report = conn.execute(
            "SELECT * FROM analysis_reports WHERE record_id = ? ORDER BY created_at DESC LIMIT 1",
            (record_id,)
        ).fetchone()
        if report:
            result['report'] = dict(report)
        return result
    return {}


def query_positions(conn, kol_name: str = '', days: int = 0) -> list:
    """Get only records that have position tracking data, ordered by time."""
    if kol_name:
        if days > 0:
            cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            rows = conn.execute(
                "SELECT * FROM kol_records WHERE kol_name = ? AND position_size IS NOT NULL AND record_date >= ? ORDER BY record_date ASC",
                (kol_name, cutoff)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM kol_records WHERE kol_name = ? AND position_size IS NOT NULL ORDER BY record_date ASC",
                (kol_name,)
            ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM kol_records WHERE position_size IS NOT NULL ORDER BY record_date ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def search_content(conn, keyword: str) -> list:
    """Full-text search in content field."""
    rows = conn.execute(
        "SELECT * FROM kol_records WHERE content LIKE ? ORDER BY created_at DESC LIMIT 50",
        (f'%{keyword}%',)
    ).fetchall()
    return [dict(r) for r in rows]


def auto_sync_from_github():
    """自动从 GitHub 拉取最新数据（静默失败，不影响查询）"""
    try:
        import subprocess
        skill_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # 1. git pull（静默）
        subprocess.run(['git', '-C', skill_dir, 'pull', '--rebase'],
                      capture_output=True, timeout=15,
                      encoding='utf-8', errors='ignore')
        # 2. 调用 sync_jsonl.py import（幂等导入 JSONL）
        sync_jsonl = os.path.join(skill_dir, 'scripts', 'sync_jsonl.py')
        if os.path.exists(sync_jsonl):
            r = subprocess.run([sys.executable, sync_jsonl, 'import'],
                              capture_output=True, timeout=20,
                              encoding='utf-8', errors='ignore')
            out = (r.stdout or '').strip()
            if out and '导入 0 条' not in out:
                print(out)
    except Exception:
        pass  # 静默失败，离线也能查询


def main():
    parser = argparse.ArgumentParser(
        description='Query KOL opinion records (default: last 30 days)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Examples:\n'
               '  python db_query.py --kol-name "wu2198"\n'
               '  python db_query.py --kol-name "wu2198" --days 7\n'
               '  python db_query.py --kol-name "wu2198" --all\n'
               '  python db_query.py --kol-name "wu2198" --with-position'
    )
    parser.add_argument('--db-path', default=None, help='Path to SQLite database')
    parser.add_argument('--kol-name', help='Filter by KOL name')
    parser.add_argument('--days', type=int, default=30,
                        help='Show records from last N days (default: 30)')
    parser.add_argument('--all', action='store_true',
                        help='Show all records (override --days)')
    parser.add_argument('--latest', type=int,
                        help='Limit to N latest records within the time window')
    parser.add_argument('--with-position', action='store_true',
                        help='Only show records with position tracking data')
    parser.add_argument('--list-kols', action='store_true', help='List all KOLs')
    parser.add_argument('--recent', type=int, help='Get N most recent records across all KOLs')
    parser.add_argument('--from', dest='date_from', help='Start date (YYYY-MM-DD)')
    parser.add_argument('--to', dest='date_to', help='End date (YYYY-MM-DD)')
    parser.add_argument('--id', type=int, help='Get record by ID')
    parser.add_argument('--search', help='Search keyword in content')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--no-sync', action='store_true', help='跳过自动从GitHub同步（默认开启同步）')
    args = parser.parse_args()

    # 自动从 GitHub 拉取最新数据（默认开启，可用 --no-sync 关闭）
    if not args.no_sync:
        auto_sync_from_github()

    db_path = args.db_path or get_default_db_path()
    conn = get_connection(db_path)

    # Calculate default date_from based on --days (unless --all or explicit --from)
    default_date_from = ''
    if not args.all and not args.date_from and not args.id and not args.search:
        default_date_from = (datetime.now() - timedelta(days=args.days)).strftime('%Y-%m-%d')

    try:
        if args.list_kols:
            results = list_kols(conn)
            if not args.json:
                if not results:
                    print('[INFO] No KOL records found.')
                else:
                    print(f'{"KOL Name":<20} {"Records":<10} {"First":<16} {"Last":<16}')
                    print('-' * 64)
                    for r in results:
                        print(f'{r["kol_name"]:<20} {r["cnt"]:<10} {r["first_date"]:<16} {r["last_date"]:<16}')

        elif args.id:
            result = query_by_id(conn, args.id)
            results = [result] if result else []

        elif args.search:
            results = search_content(conn, args.search)

        elif args.recent:
            results = query_recent(conn, args.recent)

        elif args.with_position:
            days = 0 if args.all else args.days
            results = query_positions(conn, args.kol_name or '', days)

        elif args.kol_name:
            date_from = args.date_from or default_date_from
            limit = args.latest if args.latest else 0
            results = query_by_kol(conn, args.kol_name, date_from, args.date_to or '', limit)

        else:
            results = query_recent(conn, 10)

        # Output
        used_days = f" (last {args.days} days)" if default_date_from and not args.all else ""
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        elif results and not args.list_kols:
            print(f'[INFO] Found {len(results)} records{used_days}')
            for r in results:
                print(f'\n{"="*60}')
                print(f'ID: {r["id"]} | KOL: {r["kol_name"]} | Date: {r["record_date"]}')
                print(f'Platform: {r.get("platform","")} | Assets: {r.get("related_assets","")}')
                ps = r.get('position_size')
                if ps is not None:
                    print(f'Position: {ps}米 ({r.get("position_action","")}) {r.get("position_note","")}')
                print(f'---')
                content = r['content']
                if len(content) > 200:
                    content = content[:200] + '...'
                print(f'Content: {content}')
                if r.get('report'):
                    print(f'Report: {r["report"].get("report_path", "N/A")}')
        elif not args.list_kols:
            print(f'[INFO] No records found{used_days}')
            print('[HINT] Try --all to see all records, or --days N to widen the window.')

    finally:
        conn.close()


if __name__ == '__main__':
    main()

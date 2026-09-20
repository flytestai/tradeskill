#!/usr/bin/env python3
"""
Initialize the SQLite database for KOL opinion records.

Creates the database file and tables if they don't exist.
Usage: python db_init.py [--db-path <path>]
"""

import sqlite3
import os
import sys
import argparse

from records_hash import content_hash


SCHEMA = """
CREATE TABLE IF NOT EXISTS kol_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kol_name TEXT NOT NULL,
    platform TEXT DEFAULT '',
    content TEXT NOT NULL,
    extracted_viewpoints TEXT DEFAULT '',
    related_assets TEXT DEFAULT '',
    record_date TEXT DEFAULT '',
    position_size INTEGER DEFAULT NULL,
    position_action TEXT DEFAULT '',
    position_note TEXT DEFAULT '',
    image_path TEXT DEFAULT '',
    content_hash TEXT,
    is_vip INTEGER DEFAULT 0,
    vip_pushed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS analysis_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL,
    report_path TEXT DEFAULT '',
    credibility_score INTEGER DEFAULT 0,
    recommendation_summary TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (record_id) REFERENCES kol_records(id)
);

CREATE INDEX IF NOT EXISTS idx_kol_records_name ON kol_records(kol_name);
CREATE INDEX IF NOT EXISTS idx_kol_records_date ON kol_records(record_date);
CREATE INDEX IF NOT EXISTS idx_kol_records_position ON kol_records(kol_name, position_size);
CREATE INDEX IF NOT EXISTS idx_analysis_reports_record ON analysis_reports(record_id);
"""

MIGRATION_COLUMNS = [
    "ALTER TABLE kol_records ADD COLUMN position_size INTEGER DEFAULT NULL",
    "ALTER TABLE kol_records ADD COLUMN position_action TEXT DEFAULT ''",
    "ALTER TABLE kol_records ADD COLUMN position_note TEXT DEFAULT ''",
    "ALTER TABLE kol_records ADD COLUMN image_path TEXT DEFAULT ''",
    "ALTER TABLE kol_records ADD COLUMN content_hash TEXT",
    "ALTER TABLE kol_records ADD COLUMN is_vip INTEGER DEFAULT 0",
    "ALTER TABLE kol_records ADD COLUMN vip_pushed INTEGER DEFAULT 0",
]


def get_default_db_path():
    """Get default database path relative to this script's skill directory."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    skill_dir = os.path.dirname(script_dir)
    data_dir = os.path.join(skill_dir, 'data')
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, 'kol_opinions.db')


def init_db(db_path: str) -> str:
    """Initialize the database. Returns the db path on success."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        # WAL模式 + busy_timeout：避免多进程读写锁死
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        # Run migration for existing databases (per-column, ignore duplicates)
        for stmt in MIGRATION_COLUMNS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # Column already exists
        # 回填 content_hash（历史数据）：图片按 image_key、文本按归一化正文
        try:
            rows = conn.execute(
                "SELECT id, content, image_path FROM kol_records "
                "WHERE content_hash IS NULL OR content_hash=''"
            ).fetchall()
            for rid, content, image_path in rows:
                conn.execute("UPDATE kol_records SET content_hash=? WHERE id=?",
                             (content_hash(content, image_path), rid))
            # 唯一索引：防止多路径重复入库（NULL 不参与去重，兼容未写哈希的旧路径）
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_kol_records_hash "
                "ON kol_records(kol_name, content_hash)")
            # 复合索引：加速 VIP 补推查询（WHERE kol_name=? AND is_vip=1 AND vip_pushed=0）
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kol_records_vip "
                "ON kol_records(kol_name, is_vip, vip_pushed)")
        except Exception as e:
            print(f"[WARN] content_hash 回填/索引失败: {e}")
        conn.commit()
    finally:
        conn.close()
    return db_path


def main():
    parser = argparse.ArgumentParser(description='Initialize KOL opinion database')
    parser.add_argument('--db-path', default=None, help='Path to SQLite database file')
    args = parser.parse_args()

    db_path = args.db_path or get_default_db_path()

    if os.path.exists(db_path):
        print(f'[INFO] Database already exists: {db_path}')
        # Still run init to ensure schema is up to date
        init_db(db_path)
        print('[INFO] Schema verified and up to date.')
    else:
        init_db(db_path)
        print(f'[OK] Database created: {db_path}')

    print('[OK] Tables created: kol_records, analysis_reports')
    print('[OK] Indexes created: kol_name, record_date, record_id')


if __name__ == '__main__':
    main()

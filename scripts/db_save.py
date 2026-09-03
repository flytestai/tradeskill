#!/usr/bin/env python3
"""
Save a KOL opinion record to the database.
Supports precise timestamps (YYYY-MM-DD HH:MM) for chronological tracking.

Usage:
  python db_save.py --kol-name "李大霄" --platform "微博" \
    --content "A股即将迎来史上最长牛市..." \
    --related-assets "沪深300ETF,上证50ETF" \
    --record-date "2026-08-05 14:30" \
    --position-size 6 --position-action "加仓"
"""

import sqlite3
import os
import sys
import argparse
import json
from datetime import datetime

from records_hash import content_hash
import common  # noqa: F401  触发静默运行补丁


def get_default_db_path():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    skill_dir = os.path.dirname(script_dir)
    return os.path.join(skill_dir, 'data', 'kol_opinions.db')


def save_record(db_path: str, kol_name: str, platform: str,
                content: str, related_assets: str = '',
                record_date: str = '',
                position_size: int = None,
                position_action: str = '',
                position_note: str = '') -> dict:
    """Save a KOL record. Returns dict with id and status."""
    if not os.path.exists(db_path):
        return {'error': f'Database not found: {db_path}. Run db_init.py first.'}

    if not record_date:
        record_date = datetime.now().strftime('%Y-%m-%d %H:%M')

    # Basic viewpoint extraction - look for keywords
    viewpoints = extract_viewpoints(content)

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO kol_records
               (kol_name, platform, content, extracted_viewpoints,
                related_assets, record_date,
                position_size, position_action, position_note, content_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (kol_name, platform, content, viewpoints,
             related_assets, record_date,
             position_size, position_action, position_note,
             content_hash(content))
        )
        record_id = cursor.lastrowid
        conn.commit()
        result = {
            'status': 'ok',
            'id': record_id,
            'kol_name': kol_name,
            'record_date': record_date,
            'viewpoints_count': len(viewpoints.split('\n')) if viewpoints else 0
        }
        if position_size is not None:
            result['position_size'] = position_size
            result['position_action'] = position_action
        return result
    except Exception as e:
        return {'error': str(e)}
    finally:
        conn.close()


def extract_viewpoints(content: str) -> str:
    """Extract viewpoints from content using keyword-based segmentation."""
    # Split by common delimiters
    raw_points = []
    for delimiter in ['。', '；', '！', '？', '\n']:
        if delimiter in content:
            parts = content.split(delimiter)
            for part in parts:
                stripped = part.strip()
                if len(stripped) > 5:
                    raw_points.append(stripped)

    # Keep unique points
    seen = set()
    unique_points = []
    for p in raw_points:
        if p not in seen:
            seen.add(p)
            unique_points.append(p)

    return '\n'.join(unique_points[:20])  # Cap at 20 viewpoints


def main():
    parser = argparse.ArgumentParser(description='Save KOL opinion record')
    parser.add_argument('--db-path', default=None, help='Path to SQLite database')
    parser.add_argument('--kol-name', required=True, help='Name of the KOL')
    parser.add_argument('--platform', default='未标注', help='Platform (微博/雪球/公众号/抖音/其他)')
    parser.add_argument('--content', required=True, help='Full content of the opinion')
    parser.add_argument('--related-assets', default='', help='Comma-separated related assets')
    parser.add_argument('--record-date', default='', help='Date/time of record (YYYY-MM-DD or YYYY-MM-DD HH:MM)')
    parser.add_argument('--position-size', type=int, default=None, help='Position size (e.g. 6 for 6米)')
    parser.add_argument('--position-action', default='', help='Position action: 加仓/减仓/兑现/持有/建仓/清仓')
    parser.add_argument('--position-note', default='', help='Position note (e.g. target assets)')
    parser.add_argument('--auto-sync', action='store_true', default=True,
                       help='Auto export+push to GitHub after saving (默认开启，--no-auto-sync 关闭)')
    parser.add_argument('--no-auto-sync', action='store_true', help='禁用自动推送')
    args = parser.parse_args()

    db_path = args.db_path or get_default_db_path()
    result = save_record(
        db_path, args.kol_name, args.platform,
        args.content, args.related_assets, args.record_date,
        args.position_size, args.position_action, args.position_note
    )

    if 'error' in result:
        print(f'[ERROR] {result["error"]}', file=sys.stderr)
        sys.exit(1)

    print(f'[OK] Record saved (ID: {result["id"]})')
    print(f'     KOL: {result["kol_name"]}')
    print(f'     Date: {result["record_date"]}')
    print(f'     Viewpoints extracted: {result["viewpoints_count"]}')
    if result.get('position_size') is not None:
        print(f'     Position: {result["position_size"]}米 ({result["position_action"]})')

    # Output JSON for programmatic use
    print(f'\nJSON: {json.dumps(result, ensure_ascii=False)}')

    # Auto-sync to GitHub (默认开启，可用 --no-auto-sync 关闭)
    if args.auto_sync and not args.no_auto_sync:
        print('\n[SYNC] 自动推送到 GitHub (push.sh 带重试)...')
        push_script = os.path.join(os.path.dirname(__file__), 'push.sh')
        import subprocess
        # 用 bash 运行 push.sh（带自动重试5次），忽略编码错误
        try:
            r = subprocess.run(['bash', push_script],
                              capture_output=True, text=True, timeout=120,
                              encoding='utf-8', errors='ignore')
            out = (r.stdout or '')[-500:]
            if out:
                print(out)
            if r.returncode != 0:
                print('[SYNC] 推送失败（已重试），稍后手动: bash scripts/push.sh')
        except Exception as e:
            print(f'[SYNC] 推送异常: {e}')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
多KOL对比分析系统

用法:
  python kol_compare.py                    # 对比所有大V
  python kol_compare.py --kol wu2198 李大霄  # 对比指定大V
  python kol_compare.py --detail           # 详细对比（含最新言论）
"""
import sqlite3, os, argparse
from datetime import datetime

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")

def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def get_kol_summary(conn, kol_name):
    """获取单个大V的摘要信息"""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM kol_records WHERE kol_name=?", (kol_name,))
    total = cur.fetchone()[0]

    cur.execute("SELECT MIN(record_date), MAX(record_date) FROM kol_records WHERE kol_name=?", (kol_name,))
    first, last = cur.fetchone()

    cur.execute("""SELECT position_size, position_action, position_note, record_date
        FROM kol_records WHERE kol_name=? AND position_size IS NOT NULL
        ORDER BY record_date DESC LIMIT 1""", (kol_name,))
    pos = cur.fetchone()

    cur.execute("""SELECT record_date, content FROM kol_records
        WHERE kol_name=? ORDER BY record_date DESC LIMIT 3""", (kol_name,))
    latest = cur.fetchall()

    # 情绪统计
    cur.execute("""SELECT COUNT(*) FROM kol_records WHERE kol_name=?
        AND content LIKE '%仅TA的真爱粉可见%'""", (kol_name,))
    vip_count = cur.fetchone()[0]

    return {
        "name": kol_name, "total": total, "first": first, "last": last,
        "position": pos, "latest": latest, "vip_count": vip_count
    }

def get_stance(summary):
    """从仓位推断立场"""
    if not summary["position"]:
        return "未知"
    size = summary["position"][0]
    if size is None:
        return "未知"
    if size >= 5:
        return "🔴 看多（重仓）"
    elif size >= 3:
        return "🟡 中性偏多"
    elif size >= 2:
        return "🟠 防御"
    elif size >= 1:
        return "🟢 看空（轻仓）"
    else:
        return "⚪ 空仓"

def compare(args):
    conn = connect()
    cur = conn.cursor()

    # 获取所有大V
    if args.kol:
        kols = args.kol
    else:
        cur.execute("SELECT DISTINCT kol_name FROM kol_records")
        kols = [r[0] for r in cur.fetchall()]

    print(f"\n  🆚 多KOL对比分析 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print(f"  {'='*70}")

    summaries = []
    for kol in kols:
        s = get_kol_summary(conn, kol)
        summaries.append(s)
        stance = get_stance(s)
        pos_str = ""
        if s["position"] and s["position"][0] is not None:
            pos_str = f"{s['position'][0]}米 {s['position'][1]}"
        print(f"""
  ┌─ {s['name']} ──────────────────────────────
  │ 记录数: {s['total']} 条  ({s['first']} ~ {s['last']})
  │ VIP消息: {s['vip_count']} 条
  │ 最新仓位: {pos_str or '无'}
  │ 当前立场: {stance}
  └─ 最新观点:""")
        for r in s["latest"]:
            print(f"     {r[0]} | {r[1][:55]}")

    # 立场对比
    if len(summaries) >= 2:
        print(f"\n  ⚖️ 立场对比:")
        for s in summaries:
            print(f"     {s['name']}: {get_stance(s)}")

        # 分歧检测
        stances = [get_stance(s) for s in summaries]
        if "🔴 看多（重仓）" in stances and ("🟢 看空（轻仓）" in stances or "⚪ 空仓" in stances):
            print(f"\n  ⚠️ 发现分歧：存在看多和看空的大V，市场分歧较大！")
        elif all("看多" in st or "看空" in st for st in stances):
            print(f"\n  ✅ 共识度：大V方向基本一致")
        else:
            print(f"\n  ℹ️ 大V观点分化不明显")

    conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="多KOL对比")
    parser.add_argument("--kol", nargs="+", help="指定大V名称（空格分隔）")
    args = parser.parse_args()
    compare(args)

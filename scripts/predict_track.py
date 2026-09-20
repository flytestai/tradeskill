#!/usr/bin/env python3
"""
大V预测准确率追踪系统

用法:
  python predict_track.py --add --kol "wu2198" --pred "B反目标3756" --type "点位" --target "3756" --dir "看涨到" --date "2026-08-13"
  python predict_track.py --verify --id 1 --actual "3681.80" --date "2026-08-14"
  python predict_track.py --report --kol "wu2198"          # 查看准确率报告
  python predict_track.py --list                             # 列出所有预测
"""
import sqlite3, os, sys, argparse
from datetime import datetime

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kol_name TEXT NOT NULL,
    prediction TEXT NOT NULL,
    target_type TEXT DEFAULT '',      -- 点位/方向/板块/仓位
    target_value TEXT DEFAULT '',     -- 目标值（如"3756"）
    direction TEXT DEFAULT '',        -- 看多/看空/看涨到/看跌到
    record_date TEXT DEFAULT '',      -- 预测时间
    verify_date TEXT DEFAULT '',      -- 验证时间
    actual_value TEXT DEFAULT '',     -- 实际值
    verdict TEXT DEFAULT '待验证',     -- 命中/偏差/错误/待验证
    error_pct REAL DEFAULT NULL,      -- 误差百分比
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_pred_kol ON predictions(kol_name);
CREATE INDEX IF NOT EXISTS idx_pred_verdict ON predictions(verdict);
"""

def connect(readonly: bool = False):
    """连接数据库。

    ⚠️ 为什么不能"写失败就静默降级为只读"（2026-09-19 修订）
    --------------------------------------------------------
    我先前的版本是：读写连接失败时自动回退到只读 URI。
    这个"贴心"的降级**破坏了写入语义**，并产生误导性报错：

        --add 流程：init() 建表（只读下失败，被降级吞掉）
                 → INSERT 落到只读连接
                 → 报 "attempt to write a readonly database"
        而真实原因是「容器把 data 挂成只读」—— 报错完全指不到真因。

    MCP 容器的 data 是**只读**挂载（设计如此：MCP 只做查询，写入走 REST，
    见 rest_app 的 POST /api/v1/kol/predictions，REST 容器为读写挂载）。
    所以：

      · **只读用例**（--list / --report）→ 显式传 readonly=True，
        直接用只读 URI，在只读挂载下也能正常工作。
      · **写入用例**（--add / --verify）→ 用普通读写连接；
        若环境不允许写，就让 SQLite 抛出**真实的**错误，
        由调用方据此提示"应改用 REST 接口"，而不是被降级掩盖。

    :param readonly: True 时以 mode=ro&immutable=1 打开（纯查询用）
    """
    if readonly:
        p = str(DB_PATH).replace("\\", "/")
        if not p.startswith("/"):
            p = "/" + p
        return sqlite3.connect("file:%s?mode=ro&immutable=1" % p, uri=True)

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def init():
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

def add_prediction(args):
    init()
    conn = connect()
    cur = conn.cursor()
    cur.execute("""INSERT INTO predictions
        (kol_name, prediction, target_type, target_value, direction, record_date)
        VALUES (?,?,?,?,?,?)""",
        (args.kol, args.pred, args.type or "", args.target or "",
         args.dir or "", args.date or ""))
    conn.commit()
    print(f"[OK] 预测已记录 ID={cur.lastrowid}")
    conn.close()

def verify_prediction(args):
    init()
    conn = connect()
    cur = conn.cursor()
    # 自动判断命中/偏差/错误
    verdict = "命中"
    error_pct = None
    if args.verdict:
        verdict = args.verdict
    else:
        # 根据误差自动判定
        cur.execute("SELECT target_value, direction FROM predictions WHERE id=?", (args.id,))
        row = cur.fetchone()
        if row and row[0] and args.actual:
            try:
                target = float(row[0].replace(",", ""))
                actual = float(args.actual.replace(",", ""))
                error_pct = abs(actual - target) / target * 100
                if error_pct <= 0.5:
                    verdict = "命中"
                elif error_pct <= 2.0:
                    verdict = "偏差"
                else:
                    verdict = "错误"
            except ValueError:
                pass
    cur.execute("""UPDATE predictions SET
        verify_date=?, actual_value=?, verdict=?, error_pct=? WHERE id=?""",
        (args.date or "", args.actual or "", verdict, error_pct, args.id))
    conn.commit()
    print(f"[OK] 预测 #{args.id} 验证结果: {verdict}" + (f" (误差{error_pct:.2f}%)" if error_pct else ""))
    conn.close()

def _ensure_readable(conn) -> bool:
    """只读连接下检查 predictions 表是否存在（不存在则给出可操作提示）。

    只读路径**不能**调用 init() —— 它执行 CREATE TABLE/INDEX，需要写权限，
    在 MCP 容器的只读挂载下会抛 "unable to open database file"，
    把「环境不允许写」误报成「表不存在」。
    这里改为直接查 sqlite_master（只读即可）。
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='predictions'"
        ).fetchone()
    except Exception:
        return False
    if not row:
        print("[ERROR] predictions 表不存在（该库尚未初始化）。")
        print("[HINT]  预测数据需通过 REST 接口写入：POST /api/v1/kol/predictions")
        print("       （MCP 侧为只读挂载，设计上不承担写入）")
        return False
    return True


def report(args):
    conn = connect(readonly=True)
    if not _ensure_readable(conn):
        conn.close()
        return
    cur = conn.cursor()
    where = "WHERE kol_name=?" if args.kol else "WHERE 1=1"
    params = (args.kol,) if args.kol else ()

    # 已出结果的统计
    cur.execute(f"""SELECT verdict, COUNT(*) FROM predictions
        {where} AND verdict != '待验证' GROUP BY verdict""", params)
    stats = dict(cur.fetchall())

    cur.execute(f"SELECT COUNT(*) FROM predictions {where}", params)
    total = cur.fetchone()[0]

    cur.execute(f"SELECT COUNT(*) FROM predictions {where} AND verdict='待验证'", params)
    pending = cur.fetchone()[0]

    done = total - pending
    hit = stats.get("命中", 0)
    dev = stats.get("偏差", 0)
    err = stats.get("错误", 0)

    print(f"""
  📊 预测准确率报告 {('— ' + args.kol) if args.kol else ''}
  ┌─────────────┬──────┐
  │ 总预测数     │ {total:4d} │
  │ 已出结果     │ {done:4d} │
  │ 待验证       │ {pending:4d} │
  ├─────────────┼──────┤
  │ ✅ 命中      │ {hit:4d} │
  │ ⚠️ 偏差      │ {dev:4d} │
  │ ❌ 错误      │ {err:4d} │
  └─────────────┴──────┘
""")
    if done > 0:
        print(f"  命中率: {hit/done*100:.1f}%   (含偏差: {(hit+dev)/done*100:.1f}%)")

    # 列出待验证和已出结果
    print("\n  ── 预测明细 ──")
    cur.execute(f"""SELECT id, record_date, prediction, target_value, actual_value,
        verdict, error_pct FROM predictions {where} ORDER BY id DESC""", params)
    for r in cur.fetchall():
        err_str = f"{r[6]:.2f}%" if r[6] is not None else "-"
        print(f"  #{r[0]} | {r[1]} | {r[2][:40]} | 目标{r[3]} 实际{r[4]} | {r[5]} (误差{err_str})")
    conn.close()

def list_all(args):
    conn = connect(readonly=True)
    if not _ensure_readable(conn):
        conn.close()
        return
    cur = conn.cursor()
    cur.execute("SELECT id, kol_name, record_date, prediction, verdict FROM predictions ORDER BY id DESC")
    for r in cur.fetchall():
        print(f"#{r[0]} | {r[1]} | {r[2]} | {r[3][:40]} | {r[4]}")
    conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="预测准确率追踪")
    parser.add_argument("--add", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--kol", help="大V名称")
    parser.add_argument("--pred", help="预测内容")
    parser.add_argument("--type", help="预测类型：点位/方向/板块/仓位")
    parser.add_argument("--target", help="目标值")
    parser.add_argument("--dir", help="方向：看多/看空/看涨到/看跌到")
    parser.add_argument("--date", help="日期")
    parser.add_argument("--id", type=int, help="预测ID")
    parser.add_argument("--actual", help="实际值")
    parser.add_argument("--verdict", help="手动判定：命中/偏差/错误")
    args = parser.parse_args()

    if args.add:
        add_prediction(args)
    elif args.verify:
        verify_prediction(args)
    elif args.report:
        report(args)
    elif args.list:
        list_all(args)
    else:
        parser.print_help()

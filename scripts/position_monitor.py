#!/usr/bin/env python3
"""
wu2198 仓位变化监控 — 第一时间捕捉加仓/减仓信号

用法:
  python position_monitor.py                 # 查看最近仓位变化 + 告警
  python position_monitor.py --kol wu2198    # 指定大V
  python position_monitor.py --watch         # 持续监控（每5分钟检查一次）

告警规则（基于历史规律总结）:
  🔴 减仓≥2米       → 强减仓信号（如5→3→2→1）
  🟠 减到2米        → 防御状态
  🚨 减到1米        → 接近清仓（她躲暴跌的信号）
  🔵 加仓           → 进攻信号（如买3米→5米）
"""
import sqlite3, os, sys, time, argparse, subprocess

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")
STATE_FILE = os.path.join(SKILL_DIR, "data", "position_state.txt")

BASH = r"C:\Program Files\Git\usr\bin\bash.exe"
if not os.path.exists(BASH):
    BASH = r"C:\Program Files\Git\bin\bash.exe"
if not os.path.exists(BASH):
    BASH = "bash"

def connect():
    conn = sqlite3.connect('file:' + DB_PATH + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn

def get_position_history(kol_name):
    """获取仓位变化历史（按时间正序）"""
    conn = connect()
    rows = conn.execute("""
        SELECT record_date, position_size, position_action, position_note, content
        FROM kol_records
        WHERE kol_name=? AND position_size IS NOT NULL
        ORDER BY record_date
    """, (kol_name,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def analyze_alerts(history):
    """分析仓位变化，生成告警"""
    alerts = []
    prev_size = None
    for r in history:
        size = r["position_size"]
        action = r["position_action"] or ""
        if prev_size is not None and size != prev_size:
            delta = size - prev_size
            if delta < 0:  # 减仓
                level = "🔴 减仓"
                if size <= 1:
                    level = "🚨 减到1米（接近清仓）"
                elif size <= 2:
                    level = "🟠 减到2米（防御）"
                elif abs(delta) >= 2:
                    level = "🔴 大幅减仓"
                alerts.append({
                    "time": r["record_date"],
                    "level": level,
                    "from": prev_size,
                    "to": size,
                    "delta": delta,
                    "action": action,
                    "note": (r["position_note"] or "")[:40],
                })
            elif delta > 0:  # 加仓
                alerts.append({
                    "time": r["record_date"],
                    "level": "🔵 加仓",
                    "from": prev_size,
                    "to": size,
                    "delta": delta,
                    "action": action,
                    "note": (r["position_note"] or "")[:40],
                })
        prev_size = size
    return alerts

def check(args):
    history = get_position_history(args.kol)
    if not history:
        print(f"[INFO] 无 {args.kol} 的仓位记录")
        return

    latest = history[-1]
    alerts = analyze_alerts(history)

    print(f"\n  📡 {args.kol} 仓位监控")
    print(f"  {'='*60}")
    print(f"  最新仓位: {latest['position_size']}米 ({latest['position_action'] or '持有'})")
    print(f"  最新时间: {latest['record_date']}")
    print(f"  最新备注: {latest['position_note'] or '无'}")

    # 当前告警状态
    size = latest["position_size"]
    if size <= 1:
        print(f"\n  🚨🚨 当前状态：接近清仓（1米）—— 她躲暴跌的信号！")
    elif size <= 2:
        print(f"\n  🟠 当前状态：防御（2米）")
    elif size >= 5:
        print(f"\n  🔴 当前状态：进攻（{size}米）")
    else:
        print(f"\n  🟡 当前状态：中性（{size}米）")

    # 最近的仓位变化告警
    print(f"\n  ── 仓位变化轨迹 ──")
    for a in alerts[-10:]:
        print(f"  {a['level']:<12} {a['time']}  {a['from']}米→{a['to']}米  {a['note']}")

def watch(args):
    """持续监控模式"""
    print(f"[WATCH] 开始监控 {args.kol} 仓位变化（每{args.interval}秒检查）...")
    last_size = None
    last_time = None
    try:
        while True:
            history = get_position_history(args.kol)
            if history:
                latest = history[-1]
                size = latest["position_size"]
                if last_size is None:
                    last_size = size
                    last_time = latest["record_date"]
                    print(f"[INIT] 当前 {size}米 ({latest['record_date']})")
                elif size != last_size:
                    delta = size - last_size
                    emoji = "🔴 减仓" if delta < 0 else "🔵 加仓"
                    print(f"\n{'='*50}")
                    print(f"🚨 {emoji}告警！{last_time} → {latest['record_date']}")
                    print(f"   仓位 {last_size}米 → {size}米 (变化{delta:+d}米)")
                    print(f"   备注: {latest['position_note']}")
                    if size <= 1:
                        print(f"   ⚠️⚠️ 已减到1米，接近清仓！注意风险！")
                    print(f"{'='*50}\n")
                    last_size = size
                    last_time = latest["record_date"]
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[STOP] 监控结束")

def _save_state(size, rdate):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write("%s|%s" % (size, rdate))
    except Exception:
        pass


def _load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            parts = f.read().strip().split("|")
        return {"size": int(parts[0]), "date": parts[1] if len(parts) > 1 else ""}
    except Exception:
        return None


def notify_once(args):
    """一次性检查：仓位变化时推送到群（荔枝种植交流群），并记录状态。"""
    history = get_position_history(args.kol)
    if not history:
        print("[INFO] 无仓位记录")
        return
    latest = history[-1]
    size = latest["position_size"]
    rdate = latest["record_date"]
    action = latest["position_action"] or "持有"
    note = (latest["position_note"] or "")[:40]
    prev = _load_state()

    if prev is None:
        _save_state(size, rdate)
        print(f"[INIT] 记录初始仓位 {size}米 ({rdate})")
        return

    if size != prev["size"]:
        delta = size - prev["size"]
        emoji = "🔴 减仓" if delta < 0 else "🔵 加仓"
        msg = (f"🚨 **【仓位变化】**\n"
               f"🕐 {rdate}\n"
               f"{emoji}：{prev['size']}米 → {size}米（{delta:+d}米，{action}）\n"
               f"备注：{note or '无'}")
        if size <= 1:
            msg += "\n⚠️⚠️ 已减到1米，接近清仓！"
        _save_state(size, rdate)
        try:
            subprocess.run([BASH, os.path.join(SKILL_DIR, "scripts", "notify_group.sh"), msg],
                           capture_output=True, timeout=30, cwd=SKILL_DIR)
        except Exception as e:
            print("[WARN] 仓位告警发送失败: %s" % e)
        print("[ALERT] " + msg.replace("\n", " | "))
    else:
        _save_state(size, rdate)
        print(f"[OK] 仓位未变（{size}米）")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="仓位变化监控")
    parser.add_argument("--kol", default="wu2198", help="大V名称")
    parser.add_argument("--watch", action="store_true", help="持续监控模式")
    parser.add_argument("--notify", action="store_true", help="一次性检查，仓位变化时推送到群")
    parser.add_argument("--interval", type=int, default=300, help="监控间隔（秒），默认300")
    args = parser.parse_args()

    if args.watch:
        watch(args)
    elif args.notify:
        notify_once(args)
    else:
        check(args)

#!/usr/bin/env python3
"""
关键点位监控提醒系统

用法:
  python level_monitor.py --index 创业板指 --price 3590    # 输入当前价，看距离各关键位
  python level_monitor.py --index 上证指数 --price 3918
  python level_monitor.py --list                            # 列出所有监控的点位
  python level_monitor.py --set 创业板指 --level 3540 --type 支撑 --note "B反风控线"
  python level_monitor.py --del 创业板指 --level 3540

监控逻辑：
  当前价距关键位 < 1% → 接近预警
  当前价上穿关键位     → 突破信号
  当前价下穿关键位     → 跌破信号
"""
import json, os, sys, argparse

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(SKILL_DIR, "data", "level_targets.json")

DEFAULT_LEVELS = {
    "创业板指": [
        {"level": 3540, "type": "风控线", "note": "破= B反失败"},
        {"level": 3590, "type": "支撑", "note": "短线抵抗"},
        {"level": 3626, "type": "关口", "note": "B反台阶"},
        {"level": 3686, "type": "压力", "note": "B反第一压力"},
        {"level": 3756, "type": "目标", "note": "B反终点"},
        {"level": 3805, "type": "上限", "note": "B反极限"},
    ],
    "上证指数": [
        {"level": 3741, "type": "风控线", "note": "B反支撑线起点"},
        {"level": 3767, "type": "风控线", "note": "二次反击点"},
        {"level": 3886, "type": "支撑", "note": "短线支撑"},
        {"level": 3906, "type": "支撑", "note": "短线支撑上沿"},
        {"level": 3956, "type": "关口", "note": "已收复"},
        {"level": 3982, "type": "压力", "note": "短线阻力"},
        {"level": 3996, "type": "压力", "note": "短线阻力上沿"},
    ],
}

def load_levels():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return dict(DEFAULT_LEVELS)

def save_levels(data):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def monitor(args):
    levels = load_levels()
    if args.index not in levels:
        print(f"[ERROR] 未找到 {args.index} 的监控点位。可用指数: {list(levels.keys())}")
        return
    if args.price is None:
        print("[ERROR] 请提供 --price 当前价格")
        return

    price = args.price
    print(f"\n  📍 {args.index} 当前价 {price} 关键位监控")
    print(f"  {'─'*60}")
    print(f"  {'点位':>8} {'类型':>8} {'距离':>10} {'状态':>10}  说明")

    for item in sorted(levels[args.index], key=lambda x: x["level"]):
        level = item["level"]
        diff = price - level
        diff_pct = diff / level * 100

        # 判断状态
        if abs(diff_pct) < 1.0:
            if diff >= 0:
                status = "🟡 上方接近"
            else:
                status = "🟡 下方接近"
        elif diff >= 0:
            status = "🟢 在上方"
        else:
            status = "🔴 在下方"

        arrow = "↑" if diff >= 0 else "↓"
        print(f"  {level:>8} {item['type']:>8} {abs(diff):>8.1f}({abs(diff_pct):.1f}%) {status:>10}  {item['note']}")

    print(f"\n  ⚠️ 提示: 距离 < 1% 的关键位需要重点关注")

def list_levels(args):
    levels = load_levels()
    for idx, items in levels.items():
        print(f"\n  {idx}:")
        for item in sorted(items, key=lambda x: x["level"]):
            print(f"    {item['level']:>6} | {item['type']} | {item['note']}")

def set_level(args):
    levels = load_levels()
    if args.index not in levels:
        levels[args.index] = []
    # 移除同点位的旧记录
    levels[args.index] = [x for x in levels[args.index] if x["level"] != args.level]
    levels[args.index].append({"level": args.level, "type": args.type or "关键位", "note": args.note or ""})
    save_levels(levels)
    print(f"[OK] 已设置 {args.index} 关键位 {args.level} ({args.type or '关键位'})")

def del_level(args):
    levels = load_levels()
    if args.index in levels:
        levels[args.index] = [x for x in levels[args.index] if x["level"] != args.level]
        save_levels(levels)
        print(f"[OK] 已删除 {args.index} 关键位 {args.level}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="关键点位监控")
    parser.add_argument("--index", help="指数名称：创业板指/上证指数")
    parser.add_argument("--price", type=float, help="当前价格")
    parser.add_argument("--list", action="store_true", help="列出所有监控点位")
    parser.add_argument("--set", action="store_true", help="设置点位")
    parser.add_argument("--delete", action="store_true", help="删除点位")
    parser.add_argument("--level", type=float, help="点位数值")
    parser.add_argument("--type", help="点位类型：支撑/压力/风控线/目标")
    parser.add_argument("--note", help="说明")
    args = parser.parse_args()

    if args.list:
        list_levels(args)
    elif args.set:
        set_level(args)
    elif args.delete:
        del_level(args)
    elif args.index and args.price is not None:
        monitor(args)
    else:
        parser.print_help()

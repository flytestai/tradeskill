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
import json, os, argparse

try:
    from safe_json import read_json, write_json
except Exception:
    def read_json(path, default=None, **kw):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default if default is not None else {}

    def write_json(path, data, indent=2):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=indent)
            return True
        except Exception:
            return False

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
    # 文件损坏时留证并回退内置默认值（不能直接抛：会让依赖它的周报/问答崩掉）
    d = read_json(CONFIG_PATH, default=None, on_error=lambda e, _p, b: print(
        "[ERROR] 关键位文件损坏: %s（已备份 %s）: %s" % (_p, b or "无", e)))
    if isinstance(d, dict) and d:
        return d
    return dict(DEFAULT_LEVELS)


#: 关键位数据日期文件（内容里显式记录该批点位是哪天分析的）
ASOF_FILE = os.path.join(SKILL_DIR, "data", "level_asof.txt")


def levels_asof():
    """返回该批关键位的「数据日期」datetime；无法判断返回 None。

    ⚠️ 为什么不能只看文件 mtime（CRITICAL）
    -------------------------------------
    实测踩坑：宿主机上 level_targets.json 的 mtime 是 8/31（18 天前），
    但容器里同一份文件显示「0 天前」—— 因为部署时的同步/拷贝会刷新 mtime。
    若用 mtime 判断，**换台机器看就变成「最新」**，陈旧度告警形同虚设。

    故改为优先读**内容里显式记录的日期**（data/level_asof.txt），
    其次取各点位自带的 asof 字段，最后才退回 mtime。
    """
    import datetime
    # 1) 显式日期文件
    try:
        with open(ASOF_FILE, encoding="utf-8") as f:
            raw = f.read().strip()
        if raw:
            return datetime.datetime.strptime(raw[:10], "%Y-%m-%d")
    except Exception:
        pass
    # 2) 数据里最晚的 asof 字段
    try:
        levels = load_levels()
        dates = [it.get("asof") for items in levels.values() for it in items
                 if isinstance(it, dict) and it.get("asof")]
        if dates:
            return datetime.datetime.strptime(max(dates)[:10], "%Y-%m-%d")
    except Exception:
        pass
    # 3) 退回文件 mtime（不可靠，仅兜底）
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(CONFIG_PATH))
    except Exception:
        return None


def levels_age_days():
    """关键位数据距今天数；无法判断返回 -1。"""
    import datetime
    d = levels_asof()
    if d is None:
        return -1.0
    # 用北京时间，与 levels_asof 的日期口径一致
    try:
        from common import beijing_now as _bj
        _now = _bj().replace(tzinfo=None)
    except Exception:
        _now = datetime.datetime.now()
    return (_now - d).total_seconds() / 86400.0


def levels_staleness_note():
    """生成陈旧度提示文本（无法判断时返回空）。"""
    d = levels_asof()
    if d is None:
        return ""
    age = levels_age_days()
    ds = d.strftime("%Y-%m-%d")
    if age > 14:
        return ("⚠️ 关键位数据日期 %s（已过期 %.0f 天）—— 以下点位为历史快照，"
                "请先与当前价核对后再使用" % (ds, age))
    if age > 7:
        return "⏳ 关键位数据日期 %s（%.0f 天前）—— 点位可能已偏离当前价，请留意" % (ds, age)
    return "关键位数据日期 %s（%.0f 天前）" % (ds, age)

def save_levels(data):
    # 原子写：关键位表被写坏会让所有点位丢失
    write_json(CONFIG_PATH, data)

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

        print(f"  {level:>8} {item['type']:>8} {abs(diff):>8.1f}"
              f"({abs(diff_pct):.1f}%) {status:>10}  {item['note']}")

    print(f"\n  ⚠️ 提示: 距离 < 1% 的关键位需要重点关注")

def list_levels(args):
    levels = load_levels()

    # ⚠️ 陈旧度必须显式标注：这些点位是人工快照，会随上下文进入大模型；
    #    旧输出不带日期，模型会把过期点位当成本轮有效支撑压力
    #    （实测 data/level_targets.json 已过期 20 天，点位与现价严重偏离）。
    note = levels_staleness_note()
    if note:
        print(f"  {note}")

    for idx, items in levels.items():
        print("\n  %s:" % idx)
        for item in sorted(items, key=lambda x: x["level"]):
            asof = item.get("asof") or ""
            suffix = f"  [更新 {asof}]" if asof else ""
            print(f"    {item['level']:>6} | {item['type']} | {item['note']}{suffix}")

def set_level(args):
    levels = load_levels()
    if args.index not in levels:
        levels[args.index] = []
    # 移除同点位的旧记录
    levels[args.index] = [x for x in levels[args.index] if x["level"] != args.level]
    # 每条点位带日期，便于在上下文里直接看出该位是哪次分析给出的
    import datetime
    updated = datetime.date.today().isoformat()
    levels[args.index].append({"level": args.level, "type": args.type or "关键位",
                               "note": args.note or "", "asof": updated})
    save_levels(levels)
    # 同步记录「该批点位的数据日期」，供陈旧度判断（不依赖文件 mtime）
    try:
        with open(ASOF_FILE, "w", encoding="utf-8") as f:
            f.write(updated)
    except Exception:
        pass
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

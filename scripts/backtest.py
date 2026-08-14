#!/usr/bin/env python3
"""
跟单回测系统 — 按大V仓位信号回测ETF收益

用法:
  python backtest.py                          # 用内置样例数据回测wu2198
  python backtest.py --strategy full          # full=满仓跟, half=半仓跟, vip=只跟VIP
  python backtest.py --csv prices.csv         # 用自定义价格序列回测

CSV格式: date,price  (价格可以是ETF价格或指数点位)
"""
import csv, os, sys, argparse

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# wu2198 的仓位时间线（几米，1米=16.7%仓位）
WU2198_POSITIONS = [
    ("2026-08-03", 0),   # A杀末端，空仓等待
    ("2026-08-04", 6),   # 满仓建仓
    ("2026-08-05", 3),   # 兑现至3米
    ("2026-08-06", 3),   # 持有
    ("2026-08-07", 3),   # 兑现后3米
    ("2026-08-10", 3),   # 持有
    ("2026-08-11", 3),   # 持有
    ("2026-08-12", 5),   # 加仓至5米
    ("2026-08-13", 4),   # 减仓至4米
    ("2026-08-14", 2),   # 借高开兑现至2米
]

# 创业板指 价格序列（收盘/关键点位）
SAMPLE_PRICES = [
    ("2026-08-03", 3158.00),
    ("2026-08-04", 3488.97),
    ("2026-08-05", 3584.00),
    ("2026-08-06", 3563.00),
    ("2026-08-07", 3563.00),
    ("2026-08-10", 3610.00),
    ("2026-08-11", 3549.16),
    ("2026-08-12", 3610.65),
    ("2026-08-13", 3586.00),
    ("2026-08-14", 3590.00),
]

def load_positions(csv_path=None):
    if csv_path:
        positions = []
        with open(csv_path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                positions.append((row["date"], float(row["position"])))
        return positions
    return WU2198_POSITIONS

def load_prices(csv_path=None):
    if csv_path:
        prices = []
        with open(csv_path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                prices.append((row["date"], float(row["price"])))
        return prices
    return SAMPLE_PRICES

def run_backtest(positions, prices, strategy="full"):
    """回测主逻辑，strategy: full/half/vip"""
    # 米 → 仓位百分比映射
    # full: 1米=16.7% (6米=100%)
    # half: 1米=8.3% (最多50%)
    mi_to_pct = 1/6 if strategy == "full" else 0.5/6

    price_map = dict(prices)
    pos_map = dict(positions)

    # 对齐日期
    dates = sorted(set(price_map.keys()) & set(pos_map.keys()))

    cash = 1.0       # 初始资金100%
    shares = 0.0     # 持仓份额
    nav_history = []
    prev_nav = 1.0
    peak = 1.0
    max_dd = 0.0

    for date in dates:
        price = price_map[date]
        target_pct = pos_map[date] * mi_to_pct

        # 调整仓位到目标比例
        current_nav = cash + shares * price
        target_value = current_nav * target_pct
        current_value = shares * price

        # 买卖调整
        diff_value = target_value - current_value
        if diff_value != 0:
            trade_shares = diff_value / price
            shares += trade_shares
            cash -= diff_value

        # 记录净值
        nav = cash + shares * price
        nav_history.append((date, nav, target_pct))
        prev_nav = nav

        # 计算回撤
        if nav > peak:
            peak = nav
        dd = (peak - nav) / peak
        if dd > max_dd:
            max_dd = dd

    # 计算买入持有基准
    first_price = price_map[dates[0]]
    last_price = price_map[dates[-1]]
    buy_hold_return = (last_price / first_price - 1) * 100

    final_nav = cash + shares * price_map[dates[-1]]
    total_return = (final_nav - 1) * 100

    return {
        "nav_history": nav_history,
        "total_return": total_return,
        "buy_hold_return": buy_hold_return,
        "max_drawdown": max_dd * 100,
        "final_nav": final_nav,
        "days": len(dates),
    }

def main():
    parser = argparse.ArgumentParser(description="跟单回测")
    parser.add_argument("--strategy", choices=["full", "half"], default="full",
                       help="full=满仓跟(6米=100%), half=半仓跟(6米=50%)")
    parser.add_argument("--positions", help="仓位CSV文件")
    parser.add_argument("--prices", help="价格CSV文件")
    args = parser.parse_args()

    positions = load_positions(args.positions)
    prices = load_prices(args.prices)

    print(f"\n  📈 跟单回测 — wu2198仓位信号")
    print(f"  {'='*60}")
    print(f"  策略: {'满仓跟(6米=100%)' if args.strategy=='full' else '半仓跟(6米=50%)'}")
    print(f"  标的: 创业板指（示例数据）")
    print(f"  周期: {prices[0][0]} ~ {prices[-1][0]} ({len(prices)}天)\n")

    result = run_backtest(positions, prices, args.strategy)

    # 打印每日净值
    print(f"  {'日期':<12} {'净值':>8} {'仓位':>8}")
    print(f"  {'-'*35}")
    for date, nav, pct in result["nav_history"]:
        print(f"  {date:<12} {nav:>8.4f} {pct*100:>7.1f}%")

    print(f"\n  ┌─────────────────────────────┐")
    print(f"  │ 最终净值:      {result['final_nav']:.4f}        │")
    print(f"  │ 策略收益:      {result['total_return']:+.2f}%        │")
    print(f"  │ 买入持有收益:  {result['buy_hold_return']:+.2f}%        │")
    print(f"  │ 超额收益:      {result['total_return']-result['buy_hold_return']:+.2f}%        │")
    print(f"  │ 最大回撤:      {result['max_drawdown']:.2f}%        │")
    print(f"  └─────────────────────────────┘")

    # 结论
    if result["total_return"] > result["buy_hold_return"]:
        print(f"\n  ✅ 跟单策略跑赢买入持有 {result['total_return']-result['buy_hold_return']:.2f}%")
    else:
        print(f"\n  ⚠️ 跟单策略跑输买入持有 {result['buy_hold_return']-result['total_return']:.2f}%")
    print(f"  💡 跟单的价值在于降低回撤，而非单纯追求最高收益")

if __name__ == "__main__":
    main()

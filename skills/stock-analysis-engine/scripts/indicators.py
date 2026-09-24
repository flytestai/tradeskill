#!/usr/bin/env python3
"""
五色青龙量化分析系统 - 技术指标计算脚本
支持：五色青龙量价信号、斐波那契位、波浪计数辅助、89日均线、筹码集中度判定
"""

import math
from dataclasses import dataclass, field
from typing import Optional

# ============================================================
# 数据结构
# ============================================================

@dataclass
class DailyBar:
    """单日K线数据"""
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    chip_concentration_90: Optional[float] = None  # 筹码集中度90


@dataclass
class AnalysisResult:
    """综合分析结果"""
    # 五色青龙
    wucang_color: str = "白色"
    wucang_color_desc: str = ""
    wucang_consecutive_days: int = 0
    wucang_score: int = 0  # 0-100

    # 89日均线
    ma89: float = 0.0
    ma89_position: str = ""      # "上方" / "下方"
    ma89_direction: str = ""     # "向上" / "向下" / "走平"
    ma89_signal: str = ""        # "持仓" / "止盈" / "观望"

    # 黄金分割
    fib_retracement_levels: dict = field(default_factory=dict)
    fib_extension_levels: dict = field(default_factory=dict)
    fib_current_zone: str = ""   # 当前价格在哪个斐波那契区域

    # 波浪理论
    wave_phase: str = ""         # 当前浪位描述
    wave_trend: str = ""         # "推动" / "调整" / "不明"

    # 筹码集中度
    chip_signal: str = ""        # "买入" / "卖出" / "中性"
    chip_concentration: float = 0.0

    # WR威廉指标（超买超卖温度表）
    wr_short: float = 0.0        # 短期WR（6日，更灵敏）
    wr_long: float = 0.0         # 长期WR（10日，更平滑）
    wr_zone: str = ""            # 超买 / 超卖 / 中性
    wr_signal: str = ""          # 信号描述
    wr_cross: str = ""           # 金叉 / 死叉 / 空

    # 综合评分
    total_score: int = 0         # 0-100
    verdict: str = ""            # 最终判断


# ============================================================
# 一、五色青龙量价信号
# ============================================================

def calc_wucang_color(bars: list[DailyBar], idx: int) -> dict:
    """
    计算指定位置的五色青龙信号。
    返回: {color, desc, consecutive_days, score}
    """
    if idx < 5:
        return {"color": "白色", "desc": "数据不足", "consecutive_days": 0, "score": 50}

    current = bars[idx]
    prev = bars[idx - 1]

    # 计算近5日均量
    recent_vols = [b.volume for b in bars[idx - 4:idx + 1]]
    avg_vol_5 = sum(recent_vols) / len(recent_vols)
    vol_ratio = current.volume / avg_vol_5 if avg_vol_5 > 0 else 1.0

    # 涨跌幅
    change_pct = (current.close - prev.close) / prev.close * 100

    # 横盘检测
    if abs(change_pct) < 1.0:
        recent_changes = [
            abs((bars[i].close - bars[i - 1].close) / bars[i - 1].close * 100)
            for i in range(idx - 3, idx + 1) if i > 0
        ]
        if all(c < 1.0 for c in recent_changes):
            # 计算连续白色天数
            cons = 1
            for i in range(idx - 1, max(0, idx - 10), -1):
                pc = abs((bars[i].close - bars[i - 1].close) / bars[i - 1].close * 100)
                if pc < 1.0:
                    cons += 1
                else:
                    break
            return {"color": "白色", "desc": "横盘整理，方向不明", "consecutive_days": cons, "score": 50}

    is_fangliang = vol_ratio > 1.2
    is_suoliang = vol_ratio < 0.8
    is_up = change_pct > 0

    color = "白色"
    desc = ""
    score = 50

    if is_up and is_fangliang:
        color = "赤红"
        desc = f"放量上涨（量比{vol_ratio:.2f}），主力强力介入"
        score = 90
    elif is_up and is_suoliang:
        color = "橙色"
        desc = f"缩量上涨（量比{vol_ratio:.2f}），惜售拉升"
        score = 75
    elif not is_up and is_fangliang:
        color = "黄色"
        desc = f"放量下跌（量比{vol_ratio:.2f}），恐慌抛售"
        score = 25
    elif not is_up and is_suoliang:
        color = "绿色"
        desc = f"缩量下跌（量比{vol_ratio:.2f}），自然回调"
        score = 35
    else:
        color = "白色"
        desc = "量价平衡，观望"
        score = 50

    # 计算连续天数
    cons = 1
    for i in range(idx - 1, max(0, idx - 20), -1):
        prev_color = _get_color_at(bars, i)
        if prev_color == color:
            cons += 1
        else:
            break

    return {"color": color, "desc": desc, "consecutive_days": cons, "score": score}


def _get_color_at(bars: list[DailyBar], idx: int) -> str:
    """内部辅助：快速获取指定位置的颜色（非递归，仅基于量价关系）"""
    if idx < 5:
        return "白色"
    current = bars[idx]
    prev = bars[idx - 1]

    recent_vols = [b.volume for b in bars[idx - 4:idx + 1]]
    avg_vol_5 = sum(recent_vols) / len(recent_vols)
    vol_ratio = current.volume / avg_vol_5 if avg_vol_5 > 0 else 1.0

    change_pct = (current.close - prev.close) / prev.close * 100

    is_fangliang = vol_ratio > 1.2
    is_suoliang = vol_ratio < 0.8
    is_up = change_pct > 0

    if is_up and is_fangliang:
        return "赤红"
    elif is_up and is_suoliang:
        return "橙色"
    elif not is_up and is_fangliang:
        return "黄色"
    elif not is_up and is_suoliang:
        return "绿色"
    else:
        return "白色"


# ============================================================
# 二、移动平均线
# ============================================================

def calc_ma(close_prices: list[float], period: int) -> list[float]:
    """计算简单移动平均线"""
    result = [0.0] * len(close_prices)
    for i in range(len(close_prices)):
        if i >= period - 1:
            result[i] = sum(close_prices[i - period + 1:i + 1]) / period
    return result


def calc_ema(close_prices: list[float], period: int) -> list[float]:
    """计算指数移动平均线"""
    result = [0.0] * len(close_prices)
    multiplier = 2.0 / (period + 1)
    # 首个有效值用SMA
    for i in range(len(close_prices)):
        if i == period - 1:
            result[i] = sum(close_prices[:period]) / period
        elif i >= period:
            result[i] = (close_prices[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


def eval_ma89(bars: list[DailyBar], idx: int) -> dict:
    """
    89日均线信号评估。
    返回: {ma89, position, direction, signal, score}
    """
    if idx < 89:
        return {"ma89": 0, "position": "数据不足", "direction": "数据不足",
                "signal": "数据不足", "score": 50}

    closes = [b.close for b in bars]
    ma89_list = calc_ma(closes, 89)
    ma89 = ma89_list[idx]
    current_close = bars[idx].close

    # 位置
    position = "上方" if current_close > ma89 else "下方"

    # 方向：比较近5日的MA89
    ma89_5d_ago = ma89_list[idx - 4] if idx >= 93 else ma89_list[idx]
    if ma89 > ma89_5d_ago * 1.005:
        direction = "向上"
    elif ma89 < ma89_5d_ago * 0.995:
        direction = "向下"
    else:
        direction = "走平"

    # 信号
    signal = ""
    score = 50
    if position == "上方":
        if direction == "向上":
            signal = "强势持仓"
            score = 85
        elif direction == "走平":
            signal = "中性持仓"
            score = 65
        else:
            signal = "减仓预警"
            score = 55
    else:
        if direction == "向上":
            signal = "观望"  # 等待重新站上
            score = 40
        elif direction == "向下":
            signal = "止盈"  # 跌破89MA且方向向下
            score = 15
        else:
            signal = "观望"
            score = 35

    return {"ma89": round(ma89, 2), "position": position, "direction": direction,
            "signal": signal, "score": score}


# ============================================================
# 三、黄金分割（斐波那契）
# ============================================================

def calc_fibonacci_levels(high: float, low: float, is_uptrend: bool = True) -> dict:
    """
    计算斐波那契回调位和扩展位。
    is_uptrend=True: 上涨趋势中计算回调支撑位
    is_uptrend=False: 下跌趋势中计算反弹阻力位
    """
    diff = high - low
    retracement_levels = {
        "23.6%": round(high - diff * 0.236, 2),
        "38.2%": round(high - diff * 0.382, 2),
        "50.0%": round(high - diff * 0.500, 2),
        "61.8%": round(high - diff * 0.618, 2),
        "78.6%": round(high - diff * 0.786, 2),
    }
    extension_levels = {
        "127.2%": round(high + diff * 0.272, 2),
        "161.8%": round(high + diff * 0.618, 2),
        "200.0%": round(high + diff * 1.000, 2),
        "261.8%": round(high + diff * 1.618, 2),
    }
    return {"retracement": retracement_levels, "extension": extension_levels}


def find_fib_zone(current_price: float, fib_levels: dict) -> str:
    """
    判断当前价格处于哪个斐波那契区间。
    """
    ret = fib_levels.get("retracement", {})
    ext = fib_levels.get("extension", {})

    if current_price >= ext.get("261.8%", float("inf")):
        return f"超过261.8%扩展位({ext['261.8%']})，极度超买"
    if current_price >= ext.get("200.0%", 0):
        return f"在200.0%-261.8%扩展区间({ext['200.0%']}-{ext['261.8%']})"
    if current_price >= ext.get("161.8%", 0):
        return f"在161.8%-200.0%扩展区间({ext['161.8%']}-{ext['200.0%']})"
    if current_price >= ext.get("127.2%", 0):
        return f"在127.2%-161.8%扩展区间({ext['127.2%']}-{ext['161.8%']})"
    if current_price >= ret.get("23.6%", 0):
        return f"在23.6%-0%回调区间({ret['23.6%']}以上)，强势区"
    if current_price >= ret.get("38.2%", 0):
        return f"在38.2%-23.6%回调区间({ret['38.2%']}-{ret['23.6%']})"
    if current_price >= ret.get("50.0%", 0):
        return f"在50.0%-38.2%回调区间({ret['50.0%']}-{ret['38.2%']})"
    if current_price >= ret.get("61.8%", 0):
        return f"在61.8%-50.0%回调区间({ret['61.8%']}-{ret['50.0%']})"
    if current_price >= ret.get("78.6%", 0):
        return f"在78.6%-61.8%回调区间({ret['78.6%']}-{ret['61.8%']})"
    return f"跌破78.6%回调位({ret['78.6%']})，趋势可能逆转"


# ============================================================
# 四、波浪计数辅助
# ============================================================

def identify_swing_points(highs: list[float], lows: list[float],
                           min_distance: int = 5) -> dict:
    """
    识别波段高点和低点（简易版）。
    min_distance: 两个相邻波峰/波谷之间的最小距离。
    """
    n = len(highs)
    swing_highs = []
    swing_lows = []

    for i in range(min_distance, n - min_distance):
        # 波峰
        if highs[i] == max(highs[i - min_distance:i + min_distance + 1]):
            swing_highs.append({"idx": i, "price": highs[i]})
        # 波谷
        if lows[i] == min(lows[i - min_distance:i + min_distance + 1]):
            swing_lows.append({"idx": i, "price": lows[i]})

    return {"swing_highs": swing_highs, "swing_lows": swing_lows}


def classify_wave_phase(bars: list[DailyBar], idx: int,
                        swing_points: dict) -> dict:
    """
    基于波段点判断当前可能的波浪阶段。
    返回: {phase, trend, confidence, score}
    """
    sh = swing_points.get("swing_highs", [])
    sl = swing_points.get("swing_lows", [])

    if len(sh) < 2 or len(sl) < 2:
        return {"phase": "数据不足，需要更多波段点", "trend": "不明", "confidence": "低", "score": 50}

    # 取最近的两个高点和低点
    last_high = sh[-1]
    second_last_high = sh[-2] if len(sh) >= 2 else sh[-1]
    last_low = sl[-1]
    second_last_low = sl[-2] if len(sl) >= 2 else sl[-1]

    # 判断趋势
    trend_up = last_high["price"] > second_last_high["price"] and last_low["price"] > second_last_low["price"]
    trend_down = last_high["price"] < second_last_high["price"] and last_low["price"] < second_last_low["price"]

    if not trend_up and not trend_down:
        return {"phase": "震荡整理，无明确波浪结构", "trend": "不明", "confidence": "低", "score": 50}

    # 简化的波浪位判断
    current_price = bars[idx].close

    if trend_up:
        # 上涨趋势：看是第几浪
        phase_desc, score = _estimate_wave_up(current_price, last_low, last_high,
                                               second_last_low, second_last_high)
        return {"phase": phase_desc, "trend": "推动（上升）", "confidence": "中", "score": score}
    else:
        # 下跌趋势：看调整浪
        phase_desc, score = _estimate_wave_down(current_price, last_high, last_low,
                                                 second_last_high, second_last_low)
        return {"phase": phase_desc, "trend": "调整（下跌）/ 推动（下跌）", "confidence": "中", "score": score}


def _estimate_wave_up(price, last_low, last_high, sl2, sh2):
    """上涨趋势中的波浪估算"""
    wave1_range = last_high["price"] - last_low["price"]
    fib_382 = last_high["price"] - wave1_range * 0.382
    fib_618 = last_high["price"] - wave1_range * 0.618

    if price > last_high["price"]:
        # 突破前高 → 可能在浪3或浪5
        target_1618 = last_low["price"] + wave1_range * 1.618
        if price > target_1618:
            return f"疑似浪5延长段（已超过浪1×1.618={target_1618:.2f}），警惕衰竭", 55
        return f"疑似浪3主升段（目标位{target_1618:.2f}）", 85
    elif price > fib_382:
        return f"疑似浪4回调（38.2%支撑位{fib_382:.2f}），回调未完成", 60
    elif price > fib_618:
        return f"疑似浪2回调/浪4深调（61.8%支撑位{fib_618:.2f}）", 50
    else:
        return f"深度回调，跌破61.8%（{fib_618:.2f}），可能趋势转弱", 30


def _estimate_wave_down(price, last_high, last_low, sh2, sl2):
    """下跌趋势中的波浪估算"""
    wave_a_range = last_high["price"] - last_low["price"]
    fib_382 = last_low["price"] + wave_a_range * 0.382
    fib_618 = last_low["price"] + wave_a_range * 0.618

    if price < last_low["price"]:
        return f"跌破前低，C浪延长中", 25
    elif price < fib_382:
        return f"疑似B浪反弹弱（38.2%={fib_382:.2f}下方）", 35
    elif price < fib_618:
        return f"疑似B浪反弹中（38.2%-61.8%区间）", 45
    else:
        return f"B浪反弹偏强（超过61.8%={fib_618:.2f}），警惕反转", 55


# ============================================================
# 五、筹码集中度判定
# ============================================================

def eval_chip_concentration(concentration: float) -> dict:
    """
    筹码集中度90评估。
    concentration: 百分比数值（如传入13.5表示13.5%）
    返回: {signal, level, score}
    """
    if concentration < 10:
        return {"signal": "买入", "level": "极度集中",
                "desc": "主力高度控盘，爆发力强", "score": 90}
    elif concentration < 15:
        return {"signal": "买入", "level": "集中",
                "desc": "筹码集中，适合买入", "score": 80}
    elif concentration <= 25:
        return {"signal": "中性", "level": "中性",
                "desc": "筹码分布正常，观望", "score": 50}
    elif concentration <= 35:
        return {"signal": "卖出", "level": "分散",
                "desc": "筹码趋于分散，应卖出", "score": 20}
    else:
        return {"signal": "卖出", "level": "极度分散",
                "desc": "无主力关照，不宜持有", "score": 10}


# ============================================================
# 五·补、WR威廉指标（Williams %R，超买超卖温度表）
# ============================================================

def calc_wr(highs: list[float], lows: list[float], closes: list[float],
            period_short: int = 6, period_long: int = 10) -> dict:
    """
    计算威廉指标 WR（Williams %R）。

    公式：WR = (N日最高价 - 当日收盘价) / (N日最高价 - N日最低价) × 100
    取值 0-100：
        - 趋近 0   → 收盘价贴近N日最高点 → 超买（热，可能回落）
        - 趋近 100 → 收盘价贴近N日最低点 → 超卖（冷，可能反弹）

    默认双线：短期WR（6日，灵敏）+ 长期WR（10日，平滑）。
    金叉：短期WR 上穿 长期WR（短线转强，偏多）
    死叉：短期WR 下穿 长期WR（短线转弱，偏空）

    返回: {wr_short, wr_long, zone, signal, score, cross}
    """
    def _wr(n: int, idx: int) -> Optional[float]:
        if idx < n - 1:
            return None
        hh = max(highs[idx - n + 1: idx + 1])
        ll = min(lows[idx - n + 1: idx + 1])
        if hh == ll:
            return 50.0
        return (hh - closes[idx]) / (hh - ll) * 100.0

    idx = len(closes) - 1
    wr_s = _wr(period_short, idx)
    wr_l = _wr(period_long, idx)
    if wr_s is None or wr_l is None:
        return {"wr_short": 50.0, "wr_long": 50.0, "zone": "数据不足",
                "signal": "数据不足", "score": 50, "cross": ""}

    wr_s = round(wr_s, 2)
    wr_l = round(wr_l, 2)

    # 超买超卖区判定（以长期WR为主，减少噪音）
    if wr_l >= 80:
        zone = "超卖"
    elif wr_l <= 20:
        zone = "超买"
    else:
        zone = "中性"

    # 金叉/死叉判定（比较上一交易日）
    wr_s_prev = _wr(period_short, idx - 1) if idx >= 1 else None
    wr_l_prev = _wr(period_long, idx - 1) if idx >= 1 else None
    cross = ""
    if wr_s_prev is not None and wr_l_prev is not None:
        if wr_s_prev <= wr_l_prev and wr_s > wr_l:
            cross = "金叉"
        elif wr_s_prev >= wr_l_prev and wr_s < wr_l:
            cross = "死叉"

    # 评分（0-100，越高越偏多）
    if zone == "超卖":
        score = 85
        signal = "WR进入超卖区，价格贴近近期低点，反弹概率增大"
    elif zone == "超买":
        score = 15
        signal = "WR进入超买区，价格贴近近期高点，回落风险增大"
    else:
        # 中性区内按位置微调：WR>50偏冷（偏多），WR<50偏热（偏空）
        if wr_l >= 50:
            score = 60
            signal = "WR偏冷（中性区偏超卖），可关注低吸"
        else:
            score = 40
            signal = "WR偏热（中性区偏超买），注意追高风险"

    if cross == "金叉":
        signal += "；WR金叉（短线上穿长线），短线转强"
        score = min(95, score + 5)
    elif cross == "死叉":
        signal += "；WR死叉（短线下穿长线），短线转弱"
        score = max(5, score - 5)

    return {"wr_short": wr_s, "wr_long": wr_l, "zone": zone,
            "signal": signal, "score": score, "cross": cross}


# ============================================================
# 六、综合分析
# ============================================================

def run_full_analysis(bars: list[DailyBar], fib_high: float = None,
                      fib_low: float = None) -> AnalysisResult:
    """
    执行完整的五色青龙综合分析。
    """
    result = AnalysisResult()
    idx = len(bars) - 1  # 分析最新数据
    if idx < 90:
        result.verdict = "数据不足，需要至少90个交易日"
        return result

    # 1. 五色青龙
    wc = calc_wucang_color(bars, idx)
    result.wucang_color = wc["color"]
    result.wucang_color_desc = wc["desc"]
    result.wucang_consecutive_days = wc["consecutive_days"]
    result.wucang_score = wc["score"]

    # 2. 89日均线
    ma89 = eval_ma89(bars, idx)
    result.ma89 = ma89["ma89"]
    result.ma89_position = ma89["position"]
    result.ma89_direction = ma89["direction"]
    result.ma89_signal = ma89["signal"]

    # 3. 黄金分割
    if fib_high and fib_low:
        fib = calc_fibonacci_levels(fib_high, fib_low, is_uptrend=True)
        result.fib_retracement_levels = fib["retracement"]
        result.fib_extension_levels = fib["extension"]
        result.fib_current_zone = find_fib_zone(bars[idx].close, fib)
    else:
        # 自动选最近波段的高低点
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        swing = identify_swing_points(highs, lows, min_distance=10)
        sh = swing["swing_highs"]
        sl = swing["swing_lows"]
        if sh and sl:
            auto_high = max(p["price"] for p in sh[-3:]) if len(sh) >= 3 else sh[-1]["price"]
            auto_low = min(p["price"] for p in sl[-3:]) if len(sl) >= 3 else sl[-1]["price"]
            fib = calc_fibonacci_levels(auto_high, auto_low, is_uptrend=True)
            result.fib_retracement_levels = fib["retracement"]
            result.fib_extension_levels = fib["extension"]
            result.fib_current_zone = find_fib_zone(bars[idx].close, fib)

    # 4. 波浪理论
    high_list = [b.high for b in bars]
    low_list = [b.low for b in bars]
    sp = identify_swing_points(high_list, low_list, min_distance=8)
    wave = classify_wave_phase(bars, idx, sp)
    result.wave_phase = wave["phase"]
    result.wave_trend = wave["trend"]

    # 5. 筹码集中度
    if bars[idx].chip_concentration_90 is not None:
        chip = eval_chip_concentration(bars[idx].chip_concentration_90)
        result.chip_signal = chip["signal"]
        result.chip_concentration = bars[idx].chip_concentration_90
    else:
        result.chip_signal = "无数据"
        result.chip_concentration = 0

    # 6. WR威廉指标
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    wr = calc_wr(highs, lows, closes)
    result.wr_short = wr["wr_short"]
    result.wr_long = wr["wr_long"]
    result.wr_zone = wr["zone"]
    result.wr_signal = wr["signal"]
    result.wr_cross = wr["cross"]

    # 7. 综合评分
    wucang_weight = 0.23
    ma89_weight = 0.23
    wave_weight = 0.18
    fib_weight = 0.13
    chip_weight = 0.13
    wr_weight = 0.10

    # 斐波那契分数
    fib_score = 50
    if "强势区" in result.fib_current_zone:
        fib_score = 80
    elif "38.2%" in result.fib_current_zone and "23.6%" in result.fib_current_zone:
        fib_score = 65
    elif "50.0%" in result.fib_current_zone:
        fib_score = 55
    elif "61.8%" in result.fib_current_zone:
        fib_score = 40
    elif "78.6%" in result.fib_current_zone or "跌破" in result.fib_current_zone:
        fib_score = 20

    chip_score = 50
    if result.chip_signal == "买入":
        chip_score = 85
    elif result.chip_signal == "卖出":
        chip_score = 15

    wr_score = wr["score"]

    total = (result.wucang_score * wucang_weight +
             ma89["score"] * ma89_weight +
             wave["score"] * wave_weight +
             fib_score * fib_weight +
             chip_score * chip_weight +
             wr_score * wr_weight)

    result.total_score = round(total)

    if result.total_score >= 80:
        result.verdict = "强买入信号 —— 多指标共振看多"
    elif result.total_score >= 60:
        result.verdict = "弱买入信号 —— 偏多但需确认"
    elif result.total_score >= 40:
        result.verdict = "中性信号 —— 方向不明，建议观望"
    elif result.total_score >= 20:
        result.verdict = "弱卖出信号 —— 偏空，注意风险"
    else:
        result.verdict = "强卖出信号 —— 多指标共振看空"

    return result


# ============================================================
# 七、格式化输出
# ============================================================

def format_report(result: AnalysisResult, symbol: str = "") -> str:
    """生成Markdown格式的分析报告"""
    color_map = {
        "赤红": "🔴", "橙色": "🟠", "黄色": "🟡",
        "绿色": "🟢", "白色": "⚪"
    }
    emoji = color_map.get(result.wucang_color, "⚪")

    report = f"""## {emoji} 五色青龙量化分析报告 — {symbol}

### 📊 综合评分：{result.total_score}/100 → **{result.verdict}**

---

### 一、五色青龙量价信号
| 项目 | 数值 |
|------|------|
| 当前颜色 | {emoji} {result.wucang_color} |
| 信号描述 | {result.wucang_color_desc} |
| 连续天数 | {result.wucang_consecutive_days} 天 |
| 维度评分 | {result.wucang_score}/100 |

### 二、89日均线（持仓/止盈）
| 项目 | 数值 |
|------|------|
| 89日均线值 | ¥{result.ma89} |
| 价格位置 | {result.ma89_position} |
| 均线方向 | {result.ma89_direction} |
| 操作信号 | **{result.ma89_signal}** |

### 三、黄金分割（斐波那契）
| 回调位 | 价格 |
|--------|------|
"""
    for k, v in result.fib_retracement_levels.items():
        report += f"| {k} | ¥{v} |\n"

    report += "\n| 扩展位 | 价格 |\n|--------|------|\n"
    for k, v in result.fib_extension_levels.items():
        report += f"| {k} | ¥{v} |\n"

    report += f"\n> 当前价格区间：{result.fib_current_zone}\n\n"

    report += f"""### 四、波浪理论
| 项目 | 数值 |
|------|------|
| 当前浪位 | {result.wave_phase} |
| 趋势类型 | {result.wave_trend} |

### 五、筹码集中度90
"""

    if result.chip_concentration > 0:
        report += f"""| 项目 | 数值 |
|------|------|
| 集中度90 | {result.chip_concentration:.1f}% |
| 信号 | **{result.chip_signal}** |
"""
    else:
        report += "> 暂无筹码集中度数据\n"

    report += f"""
### 六、WR威廉指标（超买超卖温度表）
| 项目 | 数值 |
|------|------|
| 短期WR（6日） | {result.wr_short} |
| 长期WR（10日） | {result.wr_long} |
| 当前区域 | {result.wr_zone} |
| 金叉/死叉 | {result.wr_cross or '—'} |
| 信号 | {result.wr_signal} |

---

### 🎯 操作建议

"""
    if result.total_score >= 80:
        report += "> **建议：积极做多。** 多个指标形成共振，信号强度高。可适当加仓，止损设在近期波段低点或89日均线下方。\n"
    elif result.total_score >= 60:
        report += "> **建议：谨慎做多。** 信号偏多但存在分歧指标，控制仓位，等待更多确认信号。\n"
    elif result.total_score >= 40:
        report += "> **建议：持币观望。** 信号中性偏弱，不宜追涨杀跌，等待明确方向后再操作。\n"
    elif result.total_score >= 20:
        report += "> **建议：减仓防守。** 多数指标偏空，建议减仓或设紧止损。严格执行89日均线止盈纪律。\n"
    else:
        report += "> **建议：果断离场。** 指标全面看空，建议清仓或止盈。不站在倒塌的墙下。\n"

    # 89日均线特别提示
    if result.ma89_signal == "止盈":
        report += "\n⚠️ **89日均线止盈信号已触发！** 价格已跌破89日均线，按纪律应执行止盈。\n"
    elif result.ma89_signal == "强势持仓":
        report += "\n✅ **89日均线持仓确认。** 价格站稳89日均线上方且均线向上，可继续持有。\n"

    # 筹码集中度特别提示
    if result.chip_signal == "卖出":
        report += "\n⚠️ **筹码集中度90 > 25%，触发卖出条件！** 筹码趋于分散，主力出货迹象。\n"
    elif result.chip_signal == "买入":
        report += "\n✅ **筹码集中度90 < 15%，触发买入条件！** 筹码高度集中，主力控盘。\n"

    # WR威廉指标特别提示
    if result.wr_zone == "超卖":
        report += "\n✅ **WR进入超卖区！** 价格贴近近期低点，反弹概率增大；若同时落在89日均线/斐波那契支撑带，为共振买点。\n"
    elif result.wr_zone == "超买":
        report += "\n⚠️ **WR进入超买区！** 价格贴近近期高点，回落风险增大，注意追高、可分批止盈。\n"

    return report


# ============================================================
# 使用示例
# ============================================================

if __name__ == "__main__":
    # 模拟数据示例
    import random
    random.seed(42)

    bars = []
    base_price = 100.0
    for i in range(120):
        change = random.gauss(0.0005, 0.02)
        close = base_price * (1 + change)
        bar = DailyBar(
            date=f"2026-{(i // 30) + 1:02d}-{(i % 30) + 1:02d}",
            open=close * random.uniform(0.995, 1.005),
            high=close * random.uniform(1.0, 1.03),
            low=close * random.uniform(0.97, 1.0),
            close=close,
            volume=random.uniform(1e8, 5e8),
            chip_concentration_90=random.uniform(10, 30) if i > 90 else random.uniform(8, 20),
        )
        bars.append(bar)
        base_price = close

    result = run_full_analysis(bars)
    print(format_report(result, symbol="示例指数"))

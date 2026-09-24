#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""elliott-index-wave —— 指数艾略特波浪分析报告生成器（纯标准库，多周期版）。

数据源链（按序回退）：腾讯日K(fqkline) → 新浪日K(getKLineData) → 腾讯实时(qt.gtimg.cn)

多周期：日线 / 周线 / 月线 / 年线 全部直连腾讯 newfqkline（period 分别为
day/week/month/year，字段一致 [date, open, close, high, low, volume, ...]）。
年线定方向 → 月线定浪级 → 周线定结构 → 日线定位置，逐级收敛。

用法：
    python generate_report.py --index 创业板指 --out /path/out.md

说明：
    本脚本是「程序化预筛 + 规则确认」的机器判定，输出为结构化 markdown 报告，
    供上层 AI 作为上下文进一步作答。最终浪型仍需人工复核。
"""

import argparse
import json
import sys
import time
import urllib.request

# ---------------------------------------------------------------------------
# 指数 → 行情代码
# ---------------------------------------------------------------------------
INDEX_CODES = {
    "上证指数": "sh000001",
    "深证成指": "sz399001",
    "创业板指": "sz399006",
    "科创50": "sh000688",
    "科创综指": "sh000680",
    "沪深300": "sh000300",
    "中证500": "sh000905",
    "北证50": "bj899050",
    "恒生指数": "hkHSI",
    "纳斯达克": "usNDX",
}

INDEX_ALIASES = {
    "大盘": "上证指数", "沪指": "上证指数", "上证": "上证指数",
    "创业板": "创业板指", "深成指": "深证成指", "深证": "深证成指",
    "科创": "科创50", "科创综指": "科创综指", "北证": "北证50",
    "沪深300": "沪深300", "中证500": "中证500", "恒生": "恒生指数",
    "纳指": "纳斯达克", "纳斯达克": "纳斯达克",
}

MARKET_NAMES = {
    "上证指数": "A股", "深证成指": "A股", "创业板指": "A股",
    "科创50": "A股", "科创综指": "A股", "沪深300": "A股",
    "中证500": "A股", "北证50": "A股", "恒生指数": "港股", "纳斯达克": "美股",
}

TF_LABEL = {"day": "日线", "week": "周线", "month": "月线", "year": "年线"}

# 各周期分析参数：min_bars=最少K线数；lookback=顶部搜索窗口；leg=末段/前段涨幅窗口
TF_PARAMS = {
    "day":   {"min_bars": 40, "lookback": 130, "leg": 80},
    "week":  {"min_bars": 20, "lookback": 65,  "leg": 40},
    "month": {"min_bars": 12, "lookback": 60,  "leg": 24},
}


def _http_get(url, timeout=15, headers=None):
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://gu.qq.com/",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 数据源：腾讯 newfqkline（多周期，字段 [date, open, close, high, low, volume]）
# ---------------------------------------------------------------------------
def _fetch_tencent(symbol, period="day", n=260):
    # 注：web.ifzq.gtimg.cn 对 urllib 返回 501（需 HTTP/2），改用 proxy.finance.qq.com
    # （同为腾讯官方源，支持 HTTP/1.1，字段一致；period 支持 day/week/month/year）。
    url = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
           "?param=%s,%s,,,%d,qfq" % (symbol, period, n))
    txt = _http_get(url, timeout=15)
    d = json.loads(txt)
    data = d.get("data", {})
    if not isinstance(data, dict):
        return []
    node = data.get(symbol, {})
    rows = node.get(period) or node.get("qfq" + period) or []
    out = []
    for r in rows:
        try:
            out.append({
                "date": str(r[0]),
                "open": float(r[1]),
                "close": float(r[2]),
                "high": float(r[3]),
                "low": float(r[4]),
                "volume": float(r[5]) if len(r) > 5 and r[5] not in ("", None) else 0.0,
            })
        except (ValueError, TypeError, IndexError):
            continue
    return out


def _fetch_sina_daily(symbol, n=260):
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/"
           "CN_MarketDataService.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d"
           % (symbol, n))
    txt = _http_get(url, timeout=15)
    start = txt.find("([")
    if start < 0:
        start = txt.find("[")
    else:
        start += 1
    end = txt.rfind("])")
    if end < 0:
        end = txt.rfind("]")
    if start < 0 or end < 0 or end <= start:
        return []
    arr = json.loads(txt[start:end + 1])
    out = []
    for r in arr:
        try:
            out.append({
                "date": str(r.get("day")),
                "open": float(r["open"]),
                "close": float(r["close"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "volume": float(r.get("volume") or 0),
            })
        except (ValueError, TypeError, KeyError):
            continue
    return out


def _fetch_tencent_quote(symbol):
    url = "https://qt.gtimg.cn/q=%s" % symbol
    raw = _http_get(url, timeout=10)
    if not raw:
        return None
    try:
        data = raw.split('="')[1].rsplit('"', 1)[0]
        p = data.split("~")
        return {"date": p[30][:8] if len(p) > 30 else "", "price": float(p[3]),
                "prev": float(p[4]), "name": p[1]}
    except Exception:
        return None


def _fetch_tf(symbol, period="day", n=260):
    """按周期取数，日线优先腾讯→新浪，周/月/年仅腾讯（无则空）。返回 (bars, source)。"""
    if period == "day":
        bars = _fetch_tencent(symbol, "day", n)
        if len(bars) >= 30:
            return bars, "腾讯行情接口（真实 OHLCV + 成交量）"
        bars = _fetch_sina_daily(symbol, n)
        if len(bars) >= 30:
            return bars, "新浪行情接口（真实 OHLCV）"
        return [], ""
    bars = _fetch_tencent(symbol, period, n)
    if bars:
        return bars, "腾讯行情接口（%s）" % TF_LABEL[period]
    return [], ""


def _fetch_daily(symbol, n=260):
    """向后兼容：日线取数，返回 (bars, source)。"""
    return _fetch_tf(symbol, "day", n)


# ---------------------------------------------------------------------------
# Zigzag：识别显著拐点（交替高低点 + 最小摆动幅度过滤）
# ---------------------------------------------------------------------------
def _zigzag(bars, pct=0.05):
    n = len(bars)
    cand = []
    for i in range(1, n - 1):
        ch, cl = bars[i]["high"], bars[i]["low"]
        if ch >= bars[i - 1]["high"] and ch >= bars[i + 1]["high"]:
            cand.append([i, ch, "H"])
        elif cl <= bars[i - 1]["low"] and cl <= bars[i + 1]["low"]:
            cand.append([i, cl, "L"])
    piv = []
    for idx, price, kind in cand:
        if not piv:
            piv.append([idx, price, kind])
            continue
        li, lp, lk = piv[-1]
        if kind == lk:
            if (kind == "H" and price > lp) or (kind == "L" and price < lp):
                piv[-1] = [idx, price, kind]
            continue
        if abs(price - lp) / max(lp, 1e-9) >= pct:
            piv.append([idx, price, kind])
    return piv


def _fmt(price):
    return "%.2f" % price if price is not None else "-"


def _c_subwaves(bars, b_off, n):
    """识别 C 浪内部 C1-C2-C3-C4-C5 子浪（在 B 高之后的一段日线里做细粒度 zigzag）。

    C1=首段下杀低点，C2=反弹高点，C3=再下杀低点（常为主跌段），
    C4=反弹高点（若 C3 后出现），C5=末端下杀低点（若 C4 后出现）。
    """
    if n - b_off < 8:
        return None
    sub = bars[b_off:]
    piv = _zigzag(sub, pct=0.02)
    l_piv = [p for p in piv if p[2] == "L"]
    h_piv = [p for p in piv if p[2] == "H"]
    if not l_piv:
        return None
    c1 = l_piv[0]
    out = {"c1": {"price": c1[1], "date": sub[c1[0]]["date"]}}
    h_after = [p for p in h_piv if p[0] > c1[0]]
    if not h_after:
        return out
    c2 = h_after[0]
    out["c2"] = {"price": c2[1], "date": sub[c2[0]]["date"]}
    c3_off = min(range(c2[0], len(sub)), key=lambda i: sub[i]["low"])
    out["c3"] = {"price": sub[c3_off]["low"], "date": sub[c3_off]["date"]}
    out["c3_ongoing"] = c3_off >= len(sub) - 2

    # C4：C3 之后的第一个反弹高点
    h_after3 = [p for p in h_piv if p[0] > c3_off]
    if h_after3 and c3_off < len(sub) - 2:
        c4 = h_after3[0]
        out["c4"] = {"price": c4[1], "date": sub[c4[0]]["date"]}
        out["c4_ongoing"] = c4[0] >= len(sub) - 2
        # C5：C4 之后的最低点
        if c4[0] < len(sub) - 1:
            c5_off = min(range(c4[0], len(sub)), key=lambda i: sub[i]["low"])
            if c5_off > c4[0]:
                out["c5"] = {"price": sub[c5_off]["low"], "date": sub[c5_off]["date"]}
                out["c5_ongoing"] = c5_off >= len(sub) - 2
    return out


# ---------------------------------------------------------------------------
# 通用 A-B-C 结构识别（按周期参数化）
# ---------------------------------------------------------------------------
def _analyze_tf(bars, tf="day"):
    n = len(bars)
    p = TF_PARAMS.get(tf, TF_PARAMS["day"])
    if n < p["min_bars"]:
        return None
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    last_close = closes[-1]
    last_date = bars[-1]["date"]

    # ---- 顶：近 lookback 根内最高点（排除末尾 3 根，避免把"正在创新高"当顶）----
    lookback = min(p["lookback"], n)
    seg_hi = highs[-lookback:]
    top_off = max(range(len(seg_hi)), key=lambda i: seg_hi[i])
    top_i = n - lookback + top_off
    if top_i >= n - 3:
        return {"state": "冲顶", "tf": tf, "top": max(seg_hi),
                "top_date": bars[top_i]["date"], "last_close": last_close,
                "last_date": last_date}
    top = highs[top_i]
    top_date = bars[top_i]["date"]

    a_off = min(range(top_i + 1, n), key=lambda i: lows[i])
    A = lows[a_off]
    A_date = bars[a_off]["date"]

    if a_off >= n - 1:
        return {"state": "A进行中", "tf": tf, "top": top, "top_date": top_date,
                "A": A, "A_date": A_date, "last_close": last_close,
                "last_date": last_date}
    b_off = max(range(a_off + 1, n), key=lambda i: highs[i])
    B = highs[b_off]
    B_date = bars[b_off]["date"]

    c_off = min(range(b_off, n), key=lambda i: lows[i])
    C = lows[c_off]
    C_date = bars[c_off]["date"]

    # ---- 前段/末段涨幅（3浪 or 5浪 门槛）----
    leg = p["leg"]
    leg_start_off = min(range(max(0, top_i - leg), top_i + 1), key=lambda i: lows[i])
    leg_low = lows[leg_start_off]
    pre_hi_off = max(range(max(0, leg_start_off - leg), leg_start_off + 1),
                     key=lambda i: highs[i]) if leg_start_off > 3 else leg_start_off
    pre_low_off = min(range(max(0, pre_hi_off - leg), pre_hi_off + 1),
                      key=lambda i: lows[i]) if pre_hi_off > 0 else 0
    pre_low = lows[pre_low_off]
    pre_hi = highs[pre_hi_off]

    last_leg = top - leg_low
    prev_leg = pre_hi - pre_low
    wave3_like = (last_leg >= prev_leg) if prev_leg > 0 else True

    amp_A = top - A
    retr_B = (B - A) / amp_A if amp_A > 0 else 0.0
    if retr_B < 0.382:
        shape = "zigzag（B回撤A %.1f%%）" % (retr_B * 100)
    elif retr_B < 0.786:
        shape = "flat（B回撤A %.1f%%）" % (retr_B * 100)
    else:
        shape = "扩张/不规则（B回撤A %.1f%%）" % (retr_B * 100)

    fib = {
        "0.618": B - 0.618 * amp_A,
        "1.000": B - amp_A,
        "1.618": B - 1.618 * amp_A,
    }
    retr_deep = amp_A / last_leg if last_leg > 0 else 0.0

    vol_A = 0.0
    vol_C = 0.0
    if a_off - top_i >= 3:
        vol_A = sum(b["volume"] for b in bars[top_i:a_off]) / max(1, a_off - top_i)
    if n - b_off >= 3:
        vol_C = sum(b["volume"] for b in bars[b_off:n]) / max(1, n - b_off)

    # 子浪细分仅在日线做（周期太粗时 zigzag 无意义）
    c_sub = _c_subwaves(bars, b_off, n) if tf == "day" else None

    return {
        "state": "调整", "tf": tf,
        "top": top, "top_date": top_date,
        "A": A, "A_date": A_date, "amp_A": amp_A,
        "B": B, "B_date": B_date, "retr_B": retr_B, "shape": shape,
        "C": C, "C_date": C_date,
        "last_close": last_close, "last_date": last_date,
        "leg_low": leg_low, "pre_low": pre_low, "pre_hi": pre_hi,
        "last_leg": last_leg, "prev_leg": prev_leg, "wave3_like": wave3_like,
        "fib": fib, "retr_deep": retr_deep,
        "vol_A": vol_A, "vol_C": vol_C,
        "c_sub": c_sub,
    }


def _analyze(bars):
    """向后兼容：日线单周期分析。"""
    return _analyze_tf(bars, "day")


def _analyze_year(bars):
    """年线：仅定超长周期方向（K线太少，不做 A-B-C）。"""
    n = len(bars)
    if n < 2:
        return None
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    last_close = closes[-1]
    last_year = bars[-1]["date"][:4]

    hist_hi_off = max(range(n - 1), key=lambda i: highs[i])
    hist_hi = highs[hist_hi_off]
    hist_hi_year = bars[hist_hi_off]["date"][:4]
    low_off = min(range(n), key=lambda i: lows[i])
    hist_low = lows[low_off]
    hist_low_year = bars[low_off]["date"][:4]

    if n >= 3:
        trend3 = "向上" if last_close >= closes[-3] else "向下"
    else:
        trend3 = "向上" if last_close >= closes[0] else "向下"

    making_new_high = last_close >= hist_hi * 0.98
    if making_new_high:
        state = "长牛延续（接近/刷新历史高位）"
    else:
        drawdown = (hist_hi - last_close) / hist_hi * 100 if hist_hi > 0 else 0.0
        state = "高位整理（距历史高点 %s 约 %.1f%%）" % (hist_hi_year, drawdown)

    return {
        "tf": "year", "state": state,
        "hist_high": hist_hi, "hist_high_year": hist_hi_year,
        "hist_low": hist_low, "hist_low_year": hist_low_year,
        "last_close": last_close, "last_year": last_year,
        "trend3": trend3, "n": n,
    }


# ---------------------------------------------------------------------------
# 多周期编排
# ---------------------------------------------------------------------------
def _multi_analyze(symbol, daily_n=2000):
    result = {"symbol": symbol, "sources": {}, "n_bars": {}}
    for tf, n in (("day", daily_n), ("week", 500), ("month", 500), ("year", 500)):
        bars, source = _fetch_tf(symbol, tf, n)
        result["sources"][tf] = source
        result["n_bars"][tf] = len(bars)
        if tf == "year":
            result[tf] = _analyze_year(bars) if bars else None
        else:
            result[tf] = _analyze_tf(bars, tf) if bars else None
    return result


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _render_tf_block(r, label):
    """渲染单个周期的 A-B-C 摘要（用于周线/月线）。"""
    L = []
    if not r:
        L.append("- %s：数据不足，无法判定" % label)
        return L
    if r.get("state") == "冲顶":
        L.append("- %s：**仍在上行，疑似第5浪冲顶段**（高点 %s @ %s，收盘 %s）" % (
            label, _fmt(r["top"]), r["top_date"], _fmt(r["last_close"])))
    elif r.get("state") == "A进行中":
        L.append("- %s：**见顶回落，A浪进行中**（顶部 %s → A低 %s）" % (
            label, _fmt(r["top"]), _fmt(r["A"])))
    else:
        L.append("- %s：**%s顶后进入调整**（A %s→%s，B回撤%.1f%%至%s，C低 %s）" % (
            label, "3浪" if r["wave3_like"] else "5浪",
            _fmt(r["top"]), _fmt(r["A"]), r["retr_B"] * 100,
            _fmt(r["B"]), _fmt(r["C"])))
    return L


def _render(multi, index, symbol):
    L = []
    day = multi.get("day")
    week = multi.get("week")
    month = multi.get("month")
    year = multi.get("year")
    n_bars = multi.get("n_bars", {})

    L.append("# %s（%s）艾略特波浪分析报告（多周期）" % (index, symbol))
    L.append("")
    L.append("> 数据：%s" % multi.get("sources", {}).get("day", "腾讯行情接口"))
    L.append("> 引擎：elliott-index-wave v5（年线→月线→周线→日线 逐级收敛）")
    L.append("> 生成：%s" % time.strftime("%Y-%m-%d %H:%M"))
    L.append("")

    L.append("## 1. 分析对象")
    L.append("- 指数：%s / %s" % (index, symbol))
    L.append("- 市场：%s" % MARKET_NAMES.get(index, "A股"))
    L.append("- 多周期样本：日线 %d 根 / 周线 %d 根 / 月线 %d 根 / 年线 %d 根" % (
        n_bars.get("day", 0), n_bars.get("week", 0),
        n_bars.get("month", 0), n_bars.get("year", 0)))
    L.append("")

    # ---- 多周期浪级定位 ----
    L.append("## 2. 多周期浪级定位（大→小）")
    if year:
        L.append("### 年线（超长周期定方向）")
        L.append("- 状态：**%s**" % year["state"])
        L.append("- 历史高点：%s（%s年）｜历史低点：%s（%s年）" % (
            _fmt(year["hist_high"]), year["hist_high_year"],
            _fmt(year["hist_low"]), year["hist_low_year"]))
        L.append("- 近3年趋势：%s｜最新收盘 %s（%s年）" % (
            year["trend3"], _fmt(year["last_close"]), year["last_year"]))
        L.append("")
    if month:
        L.append("### 月线（长周期定浪级）")
        L.extend(_render_tf_block(month, "月线"))
        L.append("")
    if week:
        L.append("### 周线（中周期定结构）")
        L.extend(_render_tf_block(week, "周线"))
        L.append("")
    L.append("### 日线（交易周期定位置）")
    if day:
        L.extend(_render_tf_block(day, "日线"))
    L.append("")

    # ---- 日线 C 浪内部 ----
    if day and day.get("state") == "调整":
        L.append("## 3. 日线 C 浪内部子浪（C1-C2-C3-C4-C5）")
        c_sub = day.get("c_sub")
        if c_sub:
            L.append("- C1（首段下杀）：%s → %s（%s）" % (
                _fmt(day["B"]), _fmt(c_sub["c1"]["price"]), c_sub["c1"]["date"]))
            if "c2" in c_sub:
                L.append("- C2（反弹）：%s → %s（%s）" % (
                    _fmt(c_sub["c1"]["price"]), _fmt(c_sub["c2"]["price"]),
                    c_sub["c2"]["date"]))
            if "c3" in c_sub:
                tag = "（进行中）" if c_sub.get("c3_ongoing") else "（已现低点）"
                L.append("- C3（主跌段）：%s → %s（%s）%s" % (
                    _fmt(c_sub["c2"]["price"]), _fmt(c_sub["c3"]["price"]),
                    c_sub["c3"]["date"], tag))
            if "c4" in c_sub:
                tag = "（进行中）" if c_sub.get("c4_ongoing") else ""
                L.append("- C4（反弹）：%s → %s（%s）%s" % (
                    _fmt(c_sub["c3"]["price"]), _fmt(c_sub["c4"]["price"]),
                    c_sub["c4"]["date"], tag))
            if "c5" in c_sub:
                tag = "（进行中）" if c_sub.get("c5_ongoing") else "（已现低点）"
                L.append("- C5（末端下杀）：%s → %s（%s）%s" % (
                    _fmt(c_sub["c4"]["price"]), _fmt(c_sub["c5"]["price"]),
                    c_sub["c5"]["date"], tag))
        else:
            L.append("- C1-C2-C3 未细分（摆动幅度不足）")
        L.append("")

    # ---- 斐波那契目标（日线）----
    if day and day.get("state") == "调整":
        L.append("## 4. C 浪斐波那契目标（日线，以 B 为起点）")
        L.append("- C = 0.618×A：%s" % _fmt(day["fib"]["0.618"]))
        L.append("- C = A（等长）：%s" % _fmt(day["fib"]["1.000"]))
        L.append("- C = 1.618×A：%s" % _fmt(day["fib"]["1.618"]))
        L.append("")

    # ---- 关键位 ----
    if day and day.get("state") == "调整":
        L.append("## 5. 关键确认 / 失效位")
        L.append("- 4浪失效（向上）：收盘突破 **%s**" % _fmt(day["top"]))
        L.append("- C浪确认（向下）：有效跌破 **%s**" % _fmt(day["A"]))
        L.append("- C浪否定（向上）：重新站上 **%s** → 4浪可能结束、转5浪" % _fmt(day["B"]))
        L.append("- 支撑参考：C浪低 %s → A浪底 %s → 斐波那契 %s / %s" % (
            _fmt(day["C"]), _fmt(day["A"]),
            _fmt(day["fib"]["0.618"]), _fmt(day["fib"]["1.000"])))
        if day["vol_A"] > 0 and day["vol_C"] > 0:
            ratio = day["vol_C"] / day["vol_A"]
            L.append("- 量能：C段均量/A段均量 = %.2f（%s）" % (
                ratio, "缩量，符合调整特征" if ratio < 1 else "放量，警惕下杀加速"))
        L.append("")
        if day["retr_deep"] >= 0.786:
            L.append("## 6. ⚠️ 深度调整预警")
            L.append("- 调整已回撤末段涨幅的 %.1f%%（>78.6%% 警戒线），警惕更大级别调整。" % (
                day["retr_deep"] * 100))
            L.append("")

    # ---- 结论摘要 ----
    L.append("## 7. 多周期结论摘要")
    if day and day.get("state") == "调整":
        if year:
            L.append("- 年线：%s（历史高点 %s@%s年，近3年%s）" % (
                year["state"], _fmt(year["hist_high"]), year["hist_high_year"],
                year["trend3"]))
        for tf, r, lbl in (("month", month, "月线"), ("week", week, "周线")):
            if r and r.get("state") == "调整":
                L.append("- %s：%s顶后调整（A %s→%s，B回撤%.1f%%至%s，C低%s）" % (
                    lbl, "3浪" if r["wave3_like"] else "5浪",
                    _fmt(r["top"]), _fmt(r["A"]), r["retr_B"] * 100,
                    _fmt(r["B"]), _fmt(r["C"])))
        L.append("- 日线：%s顶后4浪调整，A浪 %s→%s（幅%s），B回撤%.1f%%，C低%s。" % (
            "3浪" if day["wave3_like"] else "5浪",
            _fmt(day["top"]), _fmt(day["A"]), _fmt(day["amp_A"]),
            day["retr_B"] * 100, _fmt(day["C"])))
        c_sub = day.get("c_sub")
        if c_sub and "c4" in c_sub:
            L.append("- C浪内部：C1-C2-C3 已现，C4 反弹高点 %s（反弹参考位）；若 C5 下杀，"
                     "最低点目标 %s → %s → 极端 %s。" % (
                _fmt(c_sub["c4"]["price"]),
                _fmt(day["fib"]["0.618"]), _fmt(day["fib"]["1.000"]),
                _fmt(day["fib"]["1.618"])))
        else:
            L.append("- C浪内部：C1-C2-C3 已现，C3 后反抽，反弹压力 %s，下杀目标 %s → %s。" % (
                _fmt(day["C"]), _fmt(day["fib"]["0.618"]), _fmt(day["fib"]["1.000"])))
        L.append("- 关键位：跌破 %s（A浪底）则C浪延续；站回 %s（B浪顶）则4浪结束转5浪。" % (
            _fmt(day["A"]), _fmt(day["B"])))
    elif day and day.get("state") == "冲顶":
        L.append("%s 仍处冲顶过程，尚未形成 A-B-C 调整；追高以高点 %s 失守作为转弱信号。" % (
            index, _fmt(day["top"])))
    elif day and day.get("state") == "A进行中":
        L.append("%s 见顶回落后处于 A 浪下行、B 浪尚未展开；先观察 A 浪低点能否企稳。" % index)
    else:
        L.append("%s 多周期数据不足，暂无法给出波浪判定。" % index)
    L.append("")
    L.append("---")
    L.append("> 本报告为程序化预筛 + 规则确认，最终浪型仍需人工复核。")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="指数艾略特波浪分析报告（多周期）")
    ap.add_argument("--index", default="上证指数", help="指数中文名，如 创业板指")
    ap.add_argument("--out", default="", help="markdown 输出路径")
    ap.add_argument("--datalen", type=int, default=2000)
    args = ap.parse_args()

    index = args.index
    for alias, canon in INDEX_ALIASES.items():
        if alias in index:
            index = canon
            break
    symbol = INDEX_CODES.get(index)
    if not symbol:
        print("# %s：暂不支持该指数的波浪分析" % index)
        return 1

    multi = _multi_analyze(symbol, args.datalen)
    if not multi.get("day") and not multi.get("week") and not multi.get("month"):
        print("# %s：行情数据获取失败（腾讯/新浪均无数据）" % index)
        return 1

    md = _render(multi, index, symbol)

    if args.out:
        try:
            import os
            os.makedirs(os.path.dirname(args.out), exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(md)
        except Exception as e:
            print("# 写报告失败: %s" % e, file=sys.stderr)

    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())

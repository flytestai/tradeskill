#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""elliott-index-wave —— 指数艾略特波浪分析报告生成器（纯标准库）。

数据源链（按序回退）：腾讯日K(fqkline) → 新浪日K(getKLineData) → 腾讯实时(qt.gtimg.cn)

用法：
    python generate_report.py --index 创业板指 --out /path/out.md

说明：
    本脚本是「程序化预筛 + 规则确认」的机器判定，输出为结构化 markdown 报告，
    供上层 AI 作为上下文进一步作答。最终浪型仍需人工复核。

对齐 skill_agent.py 的调用契约：
    subprocess: [python, gen, "--index", <中文指数名>, "--out", <md路径>]
    · 报告写入 --out（utf-8）
    · 同时打印到 stdout（供上层无文件时兜底）
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


def _http_get(url, timeout=15, headers=None):
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://gu.qq.com/",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 数据源 1：腾讯日K（OHLCV，字段顺序 [date, open, close, high, low, volume]）
# ---------------------------------------------------------------------------
def _fetch_tencent_daily(symbol, n=260):
    # 注：web.ifzq.gtimg.cn 对 urllib 返回 501（需 HTTP/2），改用 proxy.finance.qq.com
    # （同为腾讯官方源，支持 HTTP/1.1，字段一致）。
    url = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
           "?param=%s,day,,,%d,qfq" % (symbol, n))
    txt = _http_get(url, timeout=15)
    d = json.loads(txt)
    node = d.get("data", {}).get(symbol, {})
    rows = node.get("day") or node.get("qfqday") or []
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


# ---------------------------------------------------------------------------
# 数据源 2：新浪日K（day/open/high/low/close/volume）
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 数据源 3：腾讯实时（兜底单点，几乎不会用到）
# ---------------------------------------------------------------------------
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


def _fetch_daily(symbol, n=260):
    bars = _fetch_tencent_daily(symbol, n)
    if len(bars) >= 30:
        return bars, "腾讯行情接口（真实 OHLCV + 成交量）"
    bars = _fetch_sina_daily(symbol, n)
    if len(bars) >= 30:
        return bars, "新浪行情接口（真实 OHLCV）"
    return [], ""


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
    """识别 C 浪内部的 C1-C2-C3 子浪（在 B 高之后的一段日线里做细粒度 zigzag）。"""
    if n - b_off < 8:
        return None
    sub = bars[b_off:]
    piv = _zigzag(sub, pct=0.02)
    l_piv = [p for p in piv if p[2] == "L"]
    if not l_piv:
        return None
    c1 = l_piv[0]
    out = {"c1": {"price": c1[1], "date": sub[c1[0]]["date"]}}
    h_after = [p for p in piv if p[2] == "H" and p[0] > c1[0]]
    if not h_after:
        return out
    c2 = h_after[0]
    out["c2"] = {"price": c2[1], "date": sub[c2[0]]["date"]}
    c3_off = min(range(c2[0], len(sub)), key=lambda i: sub[i]["low"])
    out["c3"] = {"price": sub[c3_off]["low"], "date": sub[c3_off]["date"]}
    out["c3_ongoing"] = c3_off >= len(sub) - 2
    return out


def _analyze(bars):
    """识别顶(A-B-C)结构。返回 dict 或 None。"""
    n = len(bars)
    if n < 40:
        return None
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    last_close = closes[-1]
    last_date = bars[-1]["date"]

    # ---- 顶：近 130 根内最高点（排除末尾 3 根，避免把"正在创新高"当顶）----
    lookback = min(130, n)
    seg_hi = highs[-lookback:]
    top_off = max(range(len(seg_hi)), key=lambda i: seg_hi[i])
    top_i = n - lookback + top_off
    if top_i >= n - 3:
        # 顶就在最近 3 根：更可能仍在冲顶/第5浪，无 A-B-C 可判
        return {"state": "冲顶", "top": max(seg_hi),
                "top_date": bars[top_i]["date"], "last_close": last_close,
                "last_date": last_date}
    top = highs[top_i]
    top_date = bars[top_i]["date"]

    # ---- A 低：顶之后最低点 ----
    a_off = min(range(top_i + 1, n), key=lambda i: lows[i])
    A = lows[a_off]
    A_date = bars[a_off]["date"]

    # ---- B 高：A 之后最高点 ----
    if a_off >= n - 1:
        # A 就是最后一根，B 尚未形成
        return {"state": "A进行中", "top": top, "top_date": top_date,
                "A": A, "A_date": A_date, "last_close": last_close,
                "last_date": last_date}
    b_off = max(range(a_off + 1, n), key=lambda i: highs[i])
    B = highs[b_off]
    B_date = bars[b_off]["date"]

    # ---- C 当前：B 之后最低点（进行中）----
    c_off = min(range(b_off, n), key=lambda i: lows[i])
    C = lows[c_off]
    C_date = bars[c_off]["date"]

    # ---- 前段/末段涨幅（3浪 or 5浪 门槛）----
    # 末段：顶之前约一季度（80 根）内的最低点作为最后一轮上攻起点
    leg_start_off = min(range(max(0, top_i - 80), top_i + 1), key=lambda i: lows[i])
    leg_low = lows[leg_start_off]
    # 前段：leg_start 之前的上一段上升（再往前 80 根）
    pre_hi_off = max(range(max(0, leg_start_off - 80), leg_start_off + 1),
                     key=lambda i: highs[i]) if leg_start_off > 3 else leg_start_off
    pre_low_off = min(range(max(0, pre_hi_off - 80), pre_hi_off + 1),
                      key=lambda i: lows[i]) if pre_hi_off > 0 else 0
    pre_low = lows[pre_low_off]
    pre_hi = highs[pre_hi_off]

    last_leg = top - leg_low          # 末段涨幅
    prev_leg = pre_hi - pre_low       # 前段涨幅
    if prev_leg <= 0:
        wave3_like = True
    else:
        wave3_like = last_leg >= prev_leg  # 末段更长 → 更像3浪

    # ---- A-B-C 幅度与回撤 ----
    amp_A = top - A
    retr_B = (B - A) / amp_A if amp_A > 0 else 0.0
    # 调整形态：B 回撤 A 的比例
    if retr_B < 0.382:
        shape = "zigzag（B回撤A %.1f%%）" % (retr_B * 100)
    elif retr_B < 0.786:
        shape = "flat（B回撤A %.1f%%）" % (retr_B * 100)
    else:
        shape = "扩张/不规则（B回撤A %.1f%%）" % (retr_B * 100)

    # C 浪斐波那契目标（以 B 为起点）
    fib = {
        "0.618": B - 0.618 * amp_A,
        "1.000": B - amp_A,
        "1.618": B - 1.618 * amp_A,
    }

    # 深度调整预警：A 段已回撤末段涨幅的比例
    retr_deep = amp_A / last_leg if last_leg > 0 else 0.0

    # 量能：C 段近期均量 vs A 段均量
    vol_A = 0.0
    vol_C = 0.0
    if a_off - top_i >= 3:
        vol_A = sum(b["volume"] for b in bars[top_i:a_off]) / max(1, a_off - top_i)
    if n - b_off >= 3:
        vol_C = sum(b["volume"] for b in bars[b_off:n]) / max(1, n - b_off)

    c_sub = _c_subwaves(bars, b_off, n)

    return {
        "state": "调整",
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


def _render(bars, r, index, symbol, source):
    L = []
    L.append("# %s（%s）艾略特波浪分析报告" % (index, symbol))
    L.append("")
    L.append("> 数据：%s" % source)
    L.append("> 引擎：elliott-index-wave v4（自动 A-B-C 识别 + 3/5浪门槛 + 量能 + 失效位）")
    L.append("> 生成：%s" % time.strftime("%Y-%m-%d %H:%M"))
    L.append("")
    L.append("## 1. 分析对象")
    L.append("- 指数：%s / %s" % (index, symbol))
    L.append("- 市场：%s" % MARKET_NAMES.get(index, "A股"))
    L.append("- 样本：日线 %d 根（%s → %s）" % (len(bars), bars[0]["date"], bars[-1]["date"]))
    L.append("")

    if r.get("state") == "冲顶":
        L.append("## 2. 当前结论（机器判定）")
        L.append("- 主浪判定：**仍在上行，疑似第5浪冲顶段**")
        L.append("- 精确位置：**未见 A-B-C 调整结构**（近3根仍在刷新高点）")
        L.append("- 参考高点：**%s**（%s）" % (_fmt(r["top"]), r["top_date"]))
        L.append("- 最新收盘：%s（%s）" % (_fmt(r["last_close"]), r["last_date"]))
        L.append("")
        L.append("## 7. 结论摘要")
        L.append("%s 仍处冲顶过程，尚未形成可识别的 A-B-C 调整；"
                 "追高需以高点 %s 失守作为转弱信号。"
                 % (index, _fmt(r["top"])))
        L.append("")
        L.append("---")
        L.append("> 本报告为程序化预筛 + 规则确认，最终浪型仍需人工复核。")
        return "\n".join(L)

    if r.get("state") == "A进行中":
        L.append("## 2. 当前结论（机器判定）")
        L.append("- 主浪判定：**见顶回落，第4浪/A浪调整进行中**")
        L.append("- 精确位置：**第4浪的A浪（进行中）**")
        L.append("- 顶部：**%s**（%s）" % (_fmt(r["top"]), r["top_date"]))
        L.append("- A浪当前低点：**%s**（%s）" % (_fmt(r["A"]), r["A_date"]))
        L.append("")
        L.append("## 7. 结论摘要")
        L.append("%s 自 %s 见顶回落后进入调整，当前处于 A 浪下行、B 浪尚未展开。"
                 "先观察 A 浪低点能否企稳。"
                 % (index, _fmt(r["top"])))
        L.append("")
        L.append("---")
        L.append("> 本报告为程序化预筛 + 规则确认，最终浪型仍需人工复核。")
        return "\n".join(L)

    # 常规调整结构
    L.append("## 2. 当前结论（机器判定）")
    L.append("- 主浪判定：**%s顶，当前处于4浪调整**" % ("3浪" if r["wave3_like"] else "5浪"))
    L.append("- 精确位置：**第4浪的C浪（进行中）**")
    L.append("- 门槛判定：%s" % ("wave3_likely" if r["wave3_like"] else "wave5_likely"))
    L.append("- 末段涨幅：%s → %s（%s）" % (_fmt(r["leg_low"]), _fmt(r["top"]),
                                            _fmt(r["last_leg"])))
    L.append("- 前段涨幅：%s → %s（%s）" % (_fmt(r["pre_low"]), _fmt(r["pre_hi"]),
                                            _fmt(r["prev_leg"])))
    L.append("- 最长检验：%s 末段最长 → %s浪" % ("✅" if r["wave3_like"] else "❌",
                                                   "3" if r["wave3_like"] else "5"))
    L.append("")
    L.append("## 3. 4浪调整内部 A-B-C 结构（自动识别）")
    L.append("- **A浪**：%s（%s）→ %s（%s），幅 %s" % (
        _fmt(r["top"]), r["top_date"], _fmt(r["A"]), r["A_date"], _fmt(r["amp_A"])))
    L.append("- **B浪**：%s → %s（%s），回撤A的 %.1f%%" % (
        _fmt(r["A"]), _fmt(r["B"]), r["B_date"], r["retr_B"] * 100))
    L.append("- **C浪**：顶点 %s，当前最低 %s（进行中）" % (_fmt(r["B"]), _fmt(r["C"])))
    L.append("- 调整形态：**%s**" % r["shape"])
    c_sub = r.get("c_sub")
    if c_sub:
        L.append("- **C浪内部子浪（C1-C2-C3）**：")
        L.append("  · C1：%s → %s（%s）" % (_fmt(r["B"]), _fmt(c_sub["c1"]["price"]),
                                               c_sub["c1"]["date"]))
        if "c2" in c_sub:
            L.append("  · C2（反弹）：%s → %s（%s）" % (
                _fmt(c_sub["c1"]["price"]), _fmt(c_sub["c2"]["price"]), c_sub["c2"]["date"]))
            if "c3" in c_sub:
                tag = "（进行中）" if c_sub.get("c3_ongoing") else "（已现低点）"
                L.append("  · C3：%s → %s（%s）%s" % (
                    _fmt(c_sub["c2"]["price"]), _fmt(c_sub["c3"]["price"]),
                    c_sub["c3"]["date"], tag))
    L.append("")
    L.append("## 4. C浪斐波那契目标")
    L.append("- C = 0.618×A：%s" % _fmt(r["fib"]["0.618"]))
    L.append("- C = A（等长）：%s" % _fmt(r["fib"]["1.000"]))
    L.append("- C = 1.618×A：%s" % _fmt(r["fib"]["1.618"]))
    L.append("")
    L.append("## 5. 关键确认 / 失效位")
    L.append("- 4浪失效（向上）：收盘突破 **%s**" % _fmt(r["top"]))
    L.append("- C浪确认（向下）：有效跌破 **%s**" % _fmt(r["A"]))
    L.append("- C浪否定（向上）：重新站上 **%s** → 4浪可能结束、转5浪" % _fmt(r["B"]))
    if r["vol_A"] > 0 and r["vol_C"] > 0:
        ratio = r["vol_C"] / r["vol_A"]
        L.append("- 量能：C段均量/A段均量 = %.2f（%s）" % (
            ratio, "缩量，符合调整特征" if ratio < 1 else "放量，警惕下杀加速"))
    L.append("")
    if r["retr_deep"] >= 0.786:
        L.append("## 6. ⚠️ 深度调整预警")
        L.append("- 调整已回撤末段涨幅的 %.1f%%（>78.6%% 为警戒线），警惕转为更大级别调整。" % (
            r["retr_deep"] * 100))
        L.append("")
    L.append("## 7. 结论摘要")
    L.append("%s 见顶后进入4浪调整（A-B-C）：A浪 %s → B浪回撤 %.1f%% → C浪进行中。" % (
        index, _fmt(r["amp_A"]), r["retr_B"] * 100))
    L.append("关键看 %s（A浪底）：跌破则C浪延续，目标 %s / %s；"
             "守住并站回 %s 则4浪可能结束、转5浪。" % (
        _fmt(r["A"]), _fmt(r["fib"]["0.618"]), _fmt(r["fib"]["1.000"]), _fmt(r["B"])))
    L.append("")
    L.append("---")
    L.append("> 本报告为程序化预筛 + 规则确认，最终浪型仍需人工复核。")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="指数艾略特波浪分析报告")
    ap.add_argument("--index", default="上证指数", help="指数中文名，如 创业板指")
    ap.add_argument("--out", default="", help="markdown 输出路径")
    ap.add_argument("--datalen", type=int, default=260)
    args = ap.parse_args()

    index = args.index
    # 别名归一
    for alias, canon in INDEX_ALIASES.items():
        if alias in index:
            index = canon
            break
    symbol = INDEX_CODES.get(index)
    if not symbol:
        print("# %s：暂不支持该指数的波浪分析" % index)
        return 1

    bars, source = _fetch_daily(symbol, args.datalen)
    if not bars:
        print("# %s：行情数据获取失败（腾讯/新浪均无数据）" % index)
        return 1

    r = _analyze(bars)
    md = _render(bars, r, index, symbol, source)

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

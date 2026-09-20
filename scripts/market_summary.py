#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日盘前 / 收盘 / 午间汇总（零 token 后端版）。

行情/成交额/主力资金直连 API，套固定模板发到「荔枝种植交流群」；
盘前播报额外读取纳斯达克100行情、PE/PB、历史百分位、历史高点回撤，
并用固定规则给出短线加仓/减仓/持有倾向。

用法:
  python market_summary.py              # 收盘汇总（当日全部发言）
  python market_summary.py --premarket  # 交易日盘前播报（默认由 supervisor 08:45 触发）
  python market_summary.py --lunch      # 午间汇总（11:35 前发言）
  python market_summary.py --dry-run    # 只打印，不发群
"""
import argparse
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

from common import find_bash, load_holidays, connect_db, clean_wu2198_text

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = find_bash()
API_URL = "https://bee-ai.integrity.com.cn/skills/v1/query2data"
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")
LEVELS_FILE = os.path.join(SKILL_DIR, "data", "alert_levels.json")
NOTIFY = os.path.join(SKILL_DIR, "scripts", "notify_feishu.sh")
STATE_FILE = os.path.join(SKILL_DIR, "data", "_market_summary_state.txt")

INDICES = ["上证指数", "深证成指", "科创50", "创业板指"]
DISPLAY = {"上证指数": "上证", "深证成指": "深证", "科创50": "科创50", "创业板指": "创业板"}


def query_item(query):
    """查询单条数据，返回首个 datas 项 dict 或 None。

    改走 `bee_client` 统一适配器（默认 http 通道，请求形态与改造前一致）；
    设置 BEE_FALLBACK_LOCAL=1 可在蜜蜂网关不可达时自动降级为公开行情源。
    """
    try:
        from bee_client import query as _bee_query, SKILL_IDS
    except Exception as e:
        print("[WARN] bee_client 导入失败: %s" % e)
        return None
    skill_id = SKILL_IDS["index"] if any(
        k in query for k in ("指数", "创业板", "上证", "深证", "科创")) else SKILL_IDS["market"]
    try:
        data = _bee_query(query, skill_id=skill_id, limit=10)
    except Exception as e:
        print("[WARN] %s 查询失败: %s" % (query, e))
        return None
    datas = (data or {}).get("datas") or []
    if not datas:
        print("[WARN] %s 无数据" % query)
        return None
    return datas[0]


def query_index(index):
    """查询指数最新价与涨跌幅，返回 (price, chg) 或 None。"""
    it = query_item(index + "最新价")
    if not it:
        return None
    try:
        price = float(str(it.get("最新价", "")).replace(",", ""))
    except Exception:
        return None
    chg = None
    for k in ("最新涨跌幅:前复权", "最新涨跌幅", "涨跌幅"):
        if it.get(k) is not None:
            try:
                chg = float(it[k])
                break
            except Exception:
                continue
    return price, chg


TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q=usNDX"
EASTMONEY_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"

# 三层标的校验：代码 > 黑名单 > 白名单。
# 关键点：「纳斯达克」是「纳斯达克100」的子串，若只做包含匹配，
# 纳斯达克综合指数（IXIC，约26000）会被误当成纳斯达克100（NDX，约29000），
# 因此黑名单必须先于白名单判定。
NDX_CODE_OK = (".ndx", "ndx", "ndx100", ".ndx100")
NDX_CODE_BAD = (".ixic", "ixic", "comp", ".comp", "ccmp")
NDX_NAME_BAD = (
    "综合", "composite", "ixic", "comp",
    "纳斯达克综合", "nasdaq composite", "道琼斯", "dow", "标普", "s&p",
)
NDX_NAME_OK = (
    "纳斯达克100", "纳斯达克 100", "nasdaq 100", "nasdaq-100", "nasdaq100",
    "ndx", "纳斯达克一百",
)


def _is_ndx(code, name):
    """三层校验：代码优先，其次黑名单否决，最后白名单放行。"""
    low_code = (code or "").strip().lower()
    low_name = (name or "").strip().lower()

    # 第 1 层：代码字段（最可靠）
    if low_code:
        if any(c in low_code for c in NDX_CODE_BAD):
            return False
        if any(c in low_code for c in NDX_CODE_OK):
            return True

    # 第 2 层：名称黑名单（必须优先于白名单）
    if low_name and any(b in low_name for b in NDX_NAME_BAD):
        return False

    # 第 3 层：名称白名单
    if low_name and any(g in low_name for g in NDX_NAME_OK):
        return True

    # 三层都不命中：宁可放弃该数据源，也不用错误指数
    return False


def _query_ndx_eastmoney():
    """东方财富公开行情备份；字段不可用时返回 None，不伪造数据。"""
    params = urllib.parse.urlencode({
        "secid": "100.NDX",
        "fields": "f43,f44,f45,f46,f47,f48,f60,f107,f169,f170",
    })
    req = urllib.request.Request(
        EASTMONEY_QUOTE_URL + "?" + params,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read().decode("utf-8", "replace")).get("data") or {}
    name = str(data.get("f58") or "")
    code = str(data.get("f57") or "")
    if not _is_ndx(code, name):
        # 三层校验不通过（如返回纳斯达克综合指数），宁可放弃该源，也不用错数据。
        raise ValueError("东方财富返回标的非纳斯达克100: code=%r name=%r" % (code[:12], name[:20]))
    raw_price = float(data.get("f43")) if data.get("f43") is not None else 0
    raw_prev = float(data.get("f60")) if data.get("f60") is not None else 0
    # 东方财富指数价格有时按百分之一返回，按量级自动归一化。
    price = raw_price / 100 if raw_price > 100000 else raw_price
    prev_close = raw_prev / 100 if raw_prev > 100000 else raw_prev
    if price <= 0 or prev_close <= 0:
        return None
    pct = float(data.get("f170")) / 100 if data.get("f170") is not None else (price / prev_close - 1) * 100
    return {
        "price": price,
        "prev_close": prev_close,
        "change": price - prev_close,
        "pct": pct,
        "high": float(data.get("f44")) if data.get("f44") is not None else None,
        "low": float(data.get("f45")) if data.get("f45") is not None else None,
        "open": float(data.get("f46")) if data.get("f46") is not None else None,
        "amplitude": None,
        "timestamp": "",
        "source": "东方财富",
    }


def _query_ndx_tencent():
    """腾讯行情备用源。"""
    req = urllib.request.Request(
        TENCENT_QUOTE_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        text = r.read().decode("gbk", "replace")
    for line in text.split(";"):
        if '="' not in line:
            continue
        _, body = line.split('="', 1)
        parts = body.rstrip('"').split("~")
        if len(parts) < 33:
            continue
        name = parts[1] if len(parts) > 1 else ""
        code = parts[2] if len(parts) > 2 else ""
        if not _is_ndx(code, name):
            raise ValueError("腾讯返回标的非纳斯达克100: code=%r name=%r" % (code[:12], name[:20]))
        price = float(parts[3])
        prev_close = float(parts[4])
        return {
            "price": price,
            "prev_close": prev_close,
            "change": float(parts[31]) if parts[31] else None,
            "pct": float(parts[32]) if parts[32] else None,
            "high": float(parts[33]) if len(parts) > 33 and parts[33] else None,
            "low": float(parts[34]) if len(parts) > 34 and parts[34] else None,
            "open": float(parts[5]) if len(parts) > 5 and parts[5] else None,
            "amplitude": float(parts[43]) if len(parts) > 43 and parts[43] else None,
            "timestamp": parts[30] if len(parts) > 30 else "",
            "source": "腾讯",
        }
    return None


def query_ndx_quote():
    """腾讯 → 东方财富双源读取纳斯达克100行情（带标的名称校验）。"""
    for name, fn in (("腾讯", _query_ndx_tencent), ("东方财富", _query_ndx_eastmoney)):
        try:
            quote = fn()
            if quote:
                return quote
        except Exception as e:
            print("[WARN] %s纳斯达克100行情失败: %s" % (name, e))
    return None


def query_etf_volume():
    """读取 ETF 当日成交额/成交量/量比/换手率，以及区间累计量能，用于量能判断。"""
    out = {}
    day = query_item("纳指ETF易方达今日成交额成交量") or {}
    for key, val in day.items():
        if not key.startswith("成交额["):
            continue
        try:
            out["amount"] = float(val)
        except (TypeError, ValueError):
            pass
    for key, val in day.items():
        if not key.startswith("成交量["):
            continue
        try:
            out["volume"] = float(val)
        except (TypeError, ValueError):
            pass
    ratio, _ = _find_value(day, ("量比[",))
    turnover, _ = _find_value(day, ("换手率[",))
    amplitude, _ = _find_value(day, ("振幅[",))
    out["volume_ratio"] = ratio
    out["turnover"] = turnover
    out["amplitude"] = amplitude

    period = query_item("纳指ETF易方达近20日成交额成交量") or {}
    total_amount = None
    for key, val in period.items():
        if key.startswith("成交额["):
            try:
                total_amount = float(val)
            except (TypeError, ValueError):
                pass
    if total_amount is not None:
        out["avg_amount_20d"] = total_amount / 20.0
    return out


def volume_evaluation(vol, intraday=False):
    """量能评估：量比 + 成交额相对20日均值，输出强弱标签与评分影响。"""
    ratio = (vol or {}).get("volume_ratio")
    amount = (vol or {}).get("amount")
    avg20 = (vol or {}).get("avg_amount_20d")
    turnover = (vol or {}).get("turnover")

    label, score_delta, note = "量能正常", 0, "量能与近期水平相当"
    # 盘前尚未开盘时成交量为 0，量比会返回 0.00；这不是「缩量」，应视为无数据，
    # 否则会误判成「明显缩量」并据此扣分。
    if ratio is not None and ratio <= 0 and not (amount or 0):
        ratio = None
    if ratio is None and not (amount or 0):
        return {"label": "盘前无成交", "score_delta": 0,
                "note": "A股尚未开盘，量能待开盘后确认",
                "ratio": None, "turnover": turnover}
    if ratio is not None:
        if ratio >= 2:
            label, score_delta = "显著放量", 2
            note = "量比%.2f，资金关注度明显提升" % ratio
        elif ratio >= 1.2:
            label, score_delta = "温和放量", 1
            note = "量比%.2f，量能小幅放大" % ratio
        elif ratio <= 0.6:
            label, score_delta = "明显缩量", -1
            note = "量比%.2f，市场参与意愿偏低" % ratio
        elif ratio <= 0.8:
            label, score_delta = "小幅缩量", 0
            note = "量比%.2f，量能略弱" % ratio
    elif amount is not None and avg20:
        rel = amount / avg20
        if rel >= 1.5:
            label, score_delta = "显著放量", 2
            note = "成交额为20日均值%.2f倍" % rel
        elif rel <= 0.7:
            label, score_delta = "明显缩量", -1
            note = "成交额为20日均值%.2f倍" % rel
    if intraday:
        note += "（盘中数据，全天量能待确认）"
    return {"label": label, "score_delta": score_delta, "note": note,
            "ratio": ratio, "turnover": turnover}


def query_etf_amount():
    """读取纳指ETF易方达（159696）当日成交额（元），失败返回 None。"""
    url = "https://qt.gtimg.cn/q=sz159696"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode("gbk", "replace")
        for line in text.split(";"):
            if '="' not in line:
                continue
            _, body = line.split('="', 1)
            parts = body.rstrip('"').split("~")
            if len(parts) > 37 and parts[37]:
                return float(parts[37]) * 1e4  # 腾讯返回万元
    except Exception as e:
        print("[WARN] 纳指ETF成交额查询失败: %s" % e)
    return None


def rsi14(closes):
    """标准 Wilder RSI14；样本不足返回 None。"""
    vals = [float(c) for c in (closes or []) if c]
    if len(vals) < 16:
        return None
    gains, losses = [], []
    for i in range(1, len(vals)):
        diff = vals[i] - vals[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:14]) / 14.0
    avg_loss = sum(losses[:14]) / 14.0
    for i in range(14, len(gains)):
        avg_gain = (avg_gain * 13 + gains[i]) / 14.0
        avg_loss = (avg_loss * 13 + losses[i]) / 14.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def ma_alignment(closes):
    """均线排列 + MA60 中期结构：多头/空头/纠缠、MA60 斜率、价格相对 MA60 位置。"""
    vals = [float(c) for c in (closes or []) if c]
    if len(vals) < 20:
        return {}
    def ma(n, offset=0):
        end = len(vals) - offset
        if end < n or end <= 0:
            return None
        return sum(vals[end - n:end]) / n
    ma5, ma20, ma60 = ma(5), ma(20), ma(60)
    price = vals[-1]
    if ma5 and ma20 and ma60:
        if ma5 > ma20 > ma60:
            state = "多头排列"
        elif ma5 < ma20 < ma60:
            state = "空头排列"
        else:
            state = "均线纠缠"
    else:
        state = "样本不足"

    # MA60 斜率：当前 MA60 与 10 个交易日前 MA60 比较，判断中期方向。
    ma60_slope = None
    ma60_prev = ma(60, offset=10) if len(vals) >= 70 else None
    if ma60 and ma60_prev:
        ma60_slope = (ma60 / ma60_prev - 1) * 100

    # MA5 / MA20 斜率：超短线结构判断用（1~3 日节奏）。
    ma5_slope = None
    ma5_prev = ma(5, offset=5) if len(vals) >= 10 else None
    if ma5 and ma5_prev:
        ma5_slope = (ma5 / ma5_prev - 1) * 100

    ma20_slope = None
    ma20_prev = ma(20, offset=10) if len(vals) >= 30 else None
    if ma20 and ma20_prev:
        ma20_slope = (ma20 / ma20_prev - 1) * 100

    # 价格相对 MA60 / MA20 的位置（偏离百分比）：正值在上方。
    ma60_dev = None
    if ma60:
        ma60_dev = (price / ma60 - 1) * 100
    ma20_dev = None
    if ma20:
        ma20_dev = (price / ma20 - 1) * 100

    return {"ma5": ma5, "ma20": ma20, "ma60": ma60, "state": state,
            "ma60_slope": ma60_slope, "ma60_dev": ma60_dev,
            "ma5_slope": ma5_slope, "ma20_slope": ma20_slope, "ma20_dev": ma20_dev,
            "above_ma20": (price > ma20) if ma20 else None,
            "above_ma60": (price > ma60) if ma60 else None}


_DAILY_CACHE = {"date": "", "rows": []}


def _fetch_ndx_daily():
    """获取纳斯达克100日线 (high, low, close)，多源回退 + 当日缓存。

    Yahoo 接口已对本站返回 403，因此以腾讯为主源（us.nDX 可取 260 根日线）。
    日线在一天内不会变化，按日缓存可避免盘前/盘中多次调用重复请求，
    也能抵挡瞬时网络抖动造成的技术面指标为空。
    """
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    if _DAILY_CACHE["date"] == today and _DAILY_CACHE["rows"]:
        return _DAILY_CACHE["rows"]

    # 主源：站内行情接口的指数历史（近 120 个交易日，含高开低收）。
    # 说明：腾讯日线接口已对本站返回 501 反爬，Yahoo 返回 403，故以此为主源。
    try:
        item = query_item("纳斯达克100指数近120日最高价最低价收盘价") or {}
        buckets = {}
        for key, val in item.items():
            m = re.search(r"\[(\d{8})\]", key)
            if not m:
                continue
            day = m.group(1)
            try:
                num = float(val)
            except (TypeError, ValueError):
                continue
            if key.startswith("最高价"):
                buckets.setdefault(day, {})["high"] = num
            elif key.startswith("最低价"):
                buckets.setdefault(day, {})["low"] = num
            elif key.startswith("收盘价"):
                buckets.setdefault(day, {})["close"] = num
        rows = [(b["high"], b["low"], b["close"]) for _, b in sorted(buckets.items())
                if b.get("high") and b.get("low") and b.get("close")]
        if len(rows) >= 60:
            _DAILY_CACHE["date"] = today
            _DAILY_CACHE["rows"] = rows
            return rows
    except Exception as e:
        print("[WARN] 指数历史日线查询失败: %s" % str(e)[:120])

    # 备源：Yahoo（部分网络环境仍可用）
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENDX?range=180d&interval=1d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            result = (json.loads(r.read().decode("utf-8", "replace")).get("chart", {}).get("result") or [None])[0]
        q = (result or {}).get("indicators", {}).get("quote", [{}])[0]
        rows = [(float(h), float(l), float(c)) for h, l, c in
                zip(q.get("high") or [], q.get("low") or [], q.get("close") or [])
                if h is not None and l is not None and c is not None and c > 0]
        if len(rows) >= 60:
            return rows
    except Exception:
        pass
    return []


def macd(closes, fast=12, slow=26, signal=9):
    """MACD(12,26,9)：返回 (dif, dea, hist)。样本不足返回 (None, None, None)。"""
    vals = [float(c) for c in (closes or []) if c]
    if len(vals) < slow + signal + 5:
        return None, None, None
    def ema(seq, n):
        k = 2.0 / (n + 1)
        out = [seq[0]]
        for x in seq[1:]:
            out.append(out[-1] + k * (x - out[-1]))
        return out
    ef, es = ema(vals, fast), ema(vals, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema(dif[slow - 1:], signal)
    return dif[-1], dea[-1], (dif[-1] - dea[-1]) * 2


def bollinger(closes, n=20, k=2.0):
    """布林带：返回 (mid, upper, lower, 价格所处百分比位置)。"""
    vals = [float(c) for c in (closes or []) if c]
    if len(vals) < n:
        return None, None, None, None
    window = vals[-n:]
    mid = sum(window) / n
    var = sum((x - mid) ** 2 for x in window) / n
    sd = math.sqrt(var)
    upper, lower = mid + k * sd, mid - k * sd
    pos = None
    if upper > lower:
        pos = (vals[-1] - lower) / (upper - lower) * 100
    return mid, upper, lower, pos


def support_resistance(closes, lookback=60):
    """近期高点/低点作为支撑阻力，并给出价格在区间中的位置百分比。"""
    vals = [float(c) for c in (closes or []) if c]
    if len(vals) < 20:
        return {}
    window = vals[-lookback:]
    hi, lo = max(window), min(window)
    pos = (vals[-1] - lo) / (hi - lo) * 100 if hi > lo else None
    return {"high": hi, "low": lo, "pos": pos}


def query_ndx_risk():
    """计算纳斯达克100技术面：ATR、波动率、RSI、均线、MACD、布林带、支撑阻力。"""
    try:
        rows = _fetch_ndx_daily()
        if len(rows) < 30:
            return {}
        trs = []
        returns = []
        prev = None
        for high, low, close in rows:
            tr = high - low if prev is None else max(high - low, abs(high - prev), abs(low - prev))
            trs.append(tr)
            if prev and prev > 0:
                returns.append(math.log(close / prev))
            prev = close
        if len(trs) < 25:
            return {}
        atr14 = sum(trs[-14:]) / 14
        baseline = trs[-74:-14] if len(trs) >= 74 else trs[:-14]
        avg_tr = sum(baseline) / len(baseline) if baseline else atr14
        rets5, rets20, rets60 = returns[-5:], returns[-20:], returns[-60:]
        def annual_vol(values):
            if len(values) < 2:
                return None
            mean = sum(values) / len(values)
            variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
            return math.sqrt(variance) * math.sqrt(252) * 100
        vol5, vol20, vol60 = annual_vol(rets5), annual_vol(rets20), annual_vol(rets60)
        clist = [c for _, _, c in rows]
        rsi = rsi14(clist)
        align = ma_alignment(clist)
        dif, dea, hist = macd(clist)
        boll_mid, boll_up, boll_low, boll_pos = bollinger(clist)
        sr = support_resistance(clist)
        return {
            "atr14": atr14,
            "atr_pct": atr14 / rows[-1][2] * 100,
            "atr_ratio": atr14 / avg_tr if avg_tr else None,
            "vol5": vol5,
            "vol20": vol20,
            "vol60": vol60,
            "vol_ratio": (vol5 / vol20) if vol5 is not None and vol20 else None,
            "rsi14": rsi,
            "macd_dif": dif, "macd_dea": dea, "macd_hist": hist,
            "boll_mid": boll_mid, "boll_up": boll_up, "boll_low": boll_low,
            "boll_pos": boll_pos,
            "sr_high": sr.get("high"), "sr_low": sr.get("low"), "sr_pos": sr.get("pos"),
            "ma_state": align.get("state"),
            "above_ma20": align.get("above_ma20"),
            "above_ma60": align.get("above_ma60"),
            "ma60_slope": align.get("ma60_slope"),
            "ma60_dev": align.get("ma60_dev"),
            "ma5_slope": align.get("ma5_slope"),
            "ma20_slope": align.get("ma20_slope"),
            "ma20_dev": align.get("ma20_dev"),
            "ma20": align.get("ma20"),
            "ma60": align.get("ma60"),
            "bars": len(rows),
        }
    except Exception as e:
        print("[WARN] 纳斯达克100技术指标计算失败: %s" % e)
        return {}


def _find_value(item, prefixes):
    """从带日期后缀的字段中取最新值。"""
    for prefix in prefixes:
        for key, value in (item or {}).items():
            if key.startswith(prefix):
                try:
                    return float(value), key
                except (TypeError, ValueError):
                    return None, key
    return None, ""


def query_ndx_valuation():
    """读取公开 QQQ 估值代理；公开源缺失的 PB/百分位不再用内部接口补齐。"""
    # QQQ 跟踪 Nasdaq-100，公开资料中的 QQQ PE 可作为指数PE的近似代理。
    # StockAnalysis 可能触发反爬，失败时返回缺失，绝不回退到好人好股估值接口。
    url = "https://stockanalysis.com/etf/qqq/"
    pe = None
    source = "公开QQQ代理"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8", "replace")
        if "Just a moment" not in text and "cf-chl" not in text.lower():
            patterns = (
                r"(?:PE Ratio|peRatio)[^0-9]{0,100}([0-9]+(?:\.[0-9]+)?)",
                r"(?:P/E|price.?earnings)[^0-9]{0,100}([0-9]+(?:\.[0-9]+)?)",
            )
            for pattern in patterns:
                m = re.search(pattern, text, re.I)
                if m:
                    pe = float(m.group(1))
                    break
    except Exception as e:
        print("[WARN] 公开QQQ PE查询失败: %s" % e)
    if pe is None:
        # 公开Q​​QQ页面受反爬限制时，仅用现有接口补当前PE；不取PB、不取百分位。
        try:
            item = query_item("纳斯达克100指数当前PE") or {}
            pe, _ = _find_value(item, ("市盈率(pe,ttm)[", "市盈率(pe,ttm)"))
        except Exception:
            pe = None
    return {
        "pe": pe,
        "pb": None,
        "pe_pct": None,
        "pb_pct": None,
        "basis": "PE",
        "public_source": url,
    }


def query_ndx_etf():
    """查询易方达纳斯达克100 ETF（159696）走势、均线和溢价率。"""
    item = None
    queries = (
        "纳指ETF易方达近5日、近20日行情走势、溢价率、均线、最新价、涨跌幅",
        "纳指ETF易方达近20日行情走势、近5日行情走势、均线、溢价率",
        "纳指ETF易方达行情、涨跌幅、溢价率",
    )
    for query in queries:
        candidate = query_item(query)
        if candidate and any(str(k).startswith("收盘价[") for k in candidate):
            item = candidate
            break
    if not item:
        return {}
    try:
        price = float(item.get("最新收盘价"))
    except (TypeError, ValueError):
        price = None
    try:
        pct = float(item.get("最新涨跌幅"))
    except (TypeError, ValueError):
        pct = None
    premium, _ = _find_value(item, ("折溢价",))
    ma, _ = _find_value(item, ("ma[",))
    closes = []
    for key, value in item.items():
        if key.startswith("收盘价["):
            try:
                closes.append((key, float(value)))
            except (TypeError, ValueError):
                pass
    closes.sort(key=lambda x: x[0])
    # 主查询通常只返回约 20 个交易日；单独取 60 日序列，用于计算中期关键位。
    if len(closes) < 60:
        long_item = query_item("纳指ETF易方达近60日收盘价") or {}
        merged = dict((k, v) for k, v in closes)
        for key, value in long_item.items():
            if not key.startswith("收盘价["):
                continue
            try:
                merged[key] = float(value)
            except (TypeError, ValueError):
                pass
        if len(merged) > len(closes):
            closes = sorted(merged.items(), key=lambda x: x[0])
    # 盘前A股尚未开盘时，"最新收盘价/涨跌幅"可能为空或为0；回退到最近一个交易日收盘。
    price_is_fallback = False
    if price is None and closes:
        price = closes[-1][1]
        price_is_fallback = True
    if pct is None or pct == 0:
        # 盘前接口常把涨跌幅置 0，且当天会先落一条与前一交易日相同的占位收盘价。
        # 这里跳过重复占位，用真正的两个相邻交易日还原涨跌幅。
        last = len(closes) - 1
        while last > 0 and closes[last][1] == closes[last - 1][1]:
            last -= 1
        if last >= 1 and closes[last - 1][1]:
            pct = (closes[last][1] / closes[last - 1][1] - 1) * 100
            price_is_fallback = True
            if not price:
                price = closes[last][1]
    # MA5 缺失时用收盘价序列自算（接口的 ma[] 字段并不总是返回）。
    if ma is None and len(closes) >= 5:
        ma = sum(v for _, v in closes[-5:]) / 5.0
    trend5 = None
    trend20 = None
    if len(closes) >= 2 and closes[0][1] > 0:
        trend5_start = closes[-5][1] if len(closes) >= 5 else closes[0][1]
        trend5 = (closes[-1][1] / trend5_start - 1) * 100
        trend20 = (closes[-1][1] / closes[0][1] - 1) * 100
    premiums = []
    for key, value in item.items():
        if key.startswith("折溢价["):
            try:
                premiums.append((key, float(value)))
            except (TypeError, ValueError):
                pass
    premiums.sort(key=lambda x: x[0])
    premium_avg5 = (sum(v for _, v in premiums[-5:]) / min(5, len(premiums))) if premiums else None
    # 溢价率历史分位：单看绝对值无法判断「高溢价是否已回落」，分位更可靠。
    premium_pct = None
    if premium is not None and len(premiums) >= 5:
        values = [v for _, v in premiums]
        below = sum(1 for v in values if v <= premium)
        premium_pct = below / len(values) * 100

    # ETF 自身关键位：可直接用于挂单，避免用户按指数点位手动换算（溢价率每天变化）。
    # 注意：接口通常只返回约 20 个交易日收盘价，样本不足以支撑 60 日区间时
    # 不重复展示中期关键位，避免出现「短中期数值完全相同」的误导。
    etf_levels = {}
    if closes:
        window20 = [v for _, v in closes[-20:]]
        window60 = [v for _, v in closes[-60:]]
        if window20:
            etf_levels["20日"] = {"low": min(window20), "high": max(window20)}
        if len(window60) > len(window20):
            etf_levels["60日"] = {"low": min(window60), "high": max(window60)}

    if premium is None:
        premium_level = "溢价率缺失"
        etf_action = "暂不判断"
        reason = "溢价率数据暂缺"
    elif premium >= 5:
        # 结合历史分位：绝对高但已在区间低位时，说明溢价正在收敛，可适度放宽。
        if premium_pct is not None and premium_pct <= 30:
            premium_level = "高溢价（回落中）"
            etf_action = "谨慎小仓"
            # ⚠️ 字面量里的 '%' 必须写成 '%%'，否则被当成格式符 →
            #    ValueError: unsupported format character '?' (0xff0c)
            #    触发条件：溢价率 ≥5% 且处于近 30 日低位。
            #    而 query_ndx_etf() 在盘前播报中是无异常保护调用的，
            #    一旦触发会直接崩掉整份播报。
            reason = ("溢价率虽高于5%%，但处于近%s日低位（%.0f%%分位），正在收敛"
                      % (len(premiums), premium_pct))
        else:
            premium_level = "高溢价"
            etf_action = "不适合直接加仓"
            reason = "溢价率超过5%，存在溢价回落风险，建议等待溢价收敛"
    elif premium >= 2:
        premium_level = "中等溢价"
        etf_action = "谨慎分批"
        reason = "溢价率仍有一定水平，适合小仓位分批而非追涨"
    else:
        premium_level = "溢价合理"
        etf_action = "可小仓分批"
        reason = "溢价率处于相对合理区间"

    if ma is not None and price is not None and price < ma:
        reason += "；ETF价格低于MA5"
    if trend5 is not None and trend5 < 0:
        reason += "；近5日走势偏弱"
    if trend20 is not None and trend20 < 0:
        reason += "；近20日走势偏弱"
    return {
        "code": "159696",
        "price": price,
        "pct": pct,
        "price_is_fallback": price_is_fallback,
        "trend5": trend5,
        "trend20": trend20,
        "ma": ma,
        "premium": premium,
        "premium_avg5": premium_avg5,
        "premium_pct": premium_pct,
        "premium_days": len(premiums),
        "levels": etf_levels,
        "premium_level": premium_level,
        "action": etf_action,
        "reason": reason,
    }


def query_ndx_high():
    """查询历史最高价，并返回接口标注的统计区间。"""
    item = query_item("纳斯达克100指数历史最高价")
    if not item:
        return None
    for key, value in item.items():
        if not key.startswith("最高价最大值"):
            continue
        try:
            high = float(value)
        except (TypeError, ValueError):
            return None
        m = re.search(r"\[([^]]+)\]", key)
        return {"high": high, "period": m.group(1) if m else "历史样本"}
    return None


def query_ndx_levels():
    """查询纳斯达克100近20日/60日高低点，生成支撑与阻力参考。"""
    item = query_item("纳斯达克100指数近20日、60日最高价和最低价")
    if not item:
        return {}
    buckets = {}
    for key, value in item.items():
        if not (key.startswith("最高价最大值") or key.startswith("最低价最小值")):
            continue
        m = re.search(r"\[([^]]+)\]", key)
        if not m:
            continue
        try:
            start, end = m.group(1).split("-", 1)
            span = (datetime.strptime(end, "%Y%m%d") -
                    datetime.strptime(start, "%Y%m%d")).days
            val = float(value)
        except (ValueError, TypeError):
            continue
        period = "20日" if span <= 40 else "60日"
        if period not in buckets:
            buckets[period] = {}
        if key.startswith("最高价"):
            buckets[period]["high"] = val
        else:
            buckets[period]["low"] = val
    return buckets


def short_quant_evaluation(quote, high_info, levels, etf, risk_info=None, volume=None):
    """超短线1~3个交易日评分：弱化PE/PB，强调动量、均线、关键位和溢价变化。"""
    price = quote.get("price") if quote else None
    daily_pct = quote.get("pct") if quote else None
    high = high_info.get("high") if high_info else None
    drawdown = (price / high - 1) * 100 if price and high else None
    etf_price = etf.get("price")
    etf_ma = etf.get("ma")
    trend5 = etf.get("trend5")
    trend20 = etf.get("trend20")
    premium = etf.get("premium")
    premium_avg5 = etf.get("premium_avg5")
    support20 = (levels.get("20日") or {}).get("low")
    resistance20 = (levels.get("20日") or {}).get("high")

    # 超短线100分：海外动量25、ETF走势30、溢价执行25、关键位15、风险5。
    overseas = 0
    if daily_pct is not None:
        overseas += 20 if daily_pct >= 1 else 14 if daily_pct >= 0 else 8 if daily_pct > -1.5 else 3
    if drawdown is not None and drawdown <= -5:
        overseas += 5
    overseas = min(overseas, 25)

    # 超短线趋势35分，按 1~3 日节奏拆解：
    #   MA5斜率10 + 价格vs MA5(12) + MA20位置8 + 近5日动量5
    # 核心目的：区分「强势回调」（跌破MA5但站在MA20上且MA20上行）与「弱势破位」。
    ma5_slope = (risk_info or {}).get("ma5_slope")
    ma20_slope = (risk_info or {}).get("ma20_slope")
    above_ma20 = (risk_info or {}).get("above_ma20")

    slope_s = 0
    if ma5_slope is not None:
        slope_s = 10 if ma5_slope >= 0.5 else 6 if ma5_slope >= 0 else 2

    price_vs_ma5 = 0
    if etf_price is not None and etf_ma is not None:
        price_vs_ma5 = 12 if etf_price >= etf_ma else 3

    ma20_s = 4
    if above_ma20 is not None and ma20_slope is not None:
        if above_ma20 and ma20_slope > 0:
            ma20_s = 8     # 站上MA20且MA20上行：短线结构完好
        elif above_ma20:
            ma20_s = 5     # 站上但MA20走平/下行：支撑力度一般
        elif ma20_slope > 0:
            ma20_s = 3     # 跌破但MA20仍上行：强势回调
        else:
            ma20_s = 0     # 跌破且MA20下行：弱势破位
    elif above_ma20 is not None:
        ma20_s = 6 if above_ma20 else 2

    momentum_s = 0
    if trend5 is not None:
        momentum_s = 5 if trend5 >= 2 else 3 if trend5 >= 0 else 2 if trend5 > -3 else 0

    trend = min(35, slope_s + price_vs_ma5 + ma20_s + momentum_s)

    execution = 0
    if premium is not None:
        execution += 25 if premium <= 3 else 18 if premium <= 5 else 10 if premium <= 8 else 5
    if premium is not None and premium_avg5 is not None:
        if premium <= premium_avg5 - 1:
            execution += 3
        elif premium <= premium_avg5:
            execution += 1
    execution = min(execution, 25)

    position = 6
    if price and support20 and price / support20 <= 1.03:
        position = 15
    elif price and resistance20 and resistance20 / price <= 1.03:
        position = 3
    elif price and support20 and price / support20 <= 1.08:
        position = 10
    position = min(position, 15)

    risk = 5
    if premium is not None and premium > 8:
        risk = 0
    elif premium is not None and premium > 5:
        risk = 2
    if daily_pct is not None and abs(daily_pct) > 2:
        risk = min(risk, 2)
    if risk_info:
        if risk_info.get("atr_ratio") is not None and risk_info["atr_ratio"] > 2:
            risk = 0
        elif risk_info.get("atr_ratio") is not None and risk_info["atr_ratio"] > 1.5:
            risk = min(risk, 2)
        if risk_info.get("vol_ratio") is not None and risk_info["vol_ratio"] > 1.5:
            risk = min(risk, 2)

    # 量能：放量代表资金参与度提升，缩量代表观望情绪，作为趋势的确认项。
    vol_eval = volume_evaluation(volume or {}, intraday=False)
    # RSI 与均线排列：超卖有反弹空间，超买有回落风险；均线多头/空头确认趋势结构。
    rsi = (risk_info or {}).get("rsi14")
    ma_state = (risk_info or {}).get("ma_state")
    rsi_adj = 0
    if rsi is not None:
        if rsi <= 30:
            rsi_adj = 6
        elif rsi <= 40:
            rsi_adj = 3
        elif rsi >= 75:
            rsi_adj = -4
        elif rsi >= 65:
            rsi_adj = -2
    ma_adj = {"多头排列": 4, "空头排列": -4}.get(ma_state, 0)
    # MACD 金叉/死叉 + 布林带位置：短线择时的确认项
    macd_dif = (risk_info or {}).get("macd_dif")
    macd_dea = (risk_info or {}).get("macd_dea")
    boll_pos = (risk_info or {}).get("boll_pos")
    macd_adj = 0
    if macd_dif is not None and macd_dea is not None:
        if macd_dif >= macd_dea and macd_dif >= 0:
            macd_adj = 3      # 零轴上金叉：动能最强
        elif macd_dif >= macd_dea:
            macd_adj = 1      # 零轴下金叉：弱反弹
        elif macd_dif < macd_dea and macd_dif < 0:
            macd_adj = -3     # 零轴下死叉：弱势
        else:
            macd_adj = -1
    boll_adj = 0
    if boll_pos is not None:
        if boll_pos <= 10:
            boll_adj = 3      # 贴近下轨：超跌反弹概率上升
        elif boll_pos >= 90:
            boll_adj = -2     # 贴近上轨：短线过热
    volume_score = 0
    if vol_eval["label"] == "显著放量":
        volume_score = 15
        if trend >= 20:
            volume_score = 18  # 放量上涨：趋势有资金确认
        elif trend <= 8:
            volume_score = 8   # 放量下跌：抛压真实存在
    elif vol_eval["label"] == "温和放量":
        volume_score = 10
    elif vol_eval["label"] == "小幅缩量":
        volume_score = 6
    elif vol_eval["label"] == "明显缩量":
        volume_score = 4

    scores = {"动量": overseas, "ETF趋势": trend, "溢价执行": execution,
              "关键位": position, "量能": volume_score,
              "技术": rsi_adj + ma_adj + macd_adj + boll_adj, "风险": risk}
    # 各维度上限：动量25+趋势35+溢价28+关键位15+量能18+技术16+风险5 = 142，归一化到 100。
    raw_total = sum(scores.values())
    total = min(100, max(0, round(raw_total / 142 * 100)))
    if total >= 70:
        action, layers = "加仓", "1～2层（20%～40%）"
    elif total >= 55:
        action, layers = "试仓", "1层（20%）"
    elif total >= 40:
        action, layers = "观望", "0～1层（0%～20%）"
    else:
        action, layers = "减仓", "0层（防守）"
    if risk < 2:
        action, layers = "观望", "0～1层（0%～20%）"
    elif risk < 4 and action == "加仓":
        action, layers = "试仓", "1层（20%）"
    short_risk_cap = 2 if risk >= 4 else 1 if risk >= 2 else 0
    # 弱势破位（跌破MA20 且 MA20 下行）：超短线不允许加仓，最多观望。
    short_broken = (above_ma20 is False and ma20_slope is not None and ma20_slope <= 0)
    if short_broken:
        short_risk_cap = 0
        if action in ("加仓", "试仓"):
            action, layers = "观望", "0～1层（0%～20%）"
    short_layer_count = 2 if action == "加仓" else 1 if action == "试仓" else 1 if action == "观望" else 0
    if action == "观望" and short_risk_cap == 0:
        # 观望 + 风险偏高：允许 0～1 层的观察仓，不直接压成 0～0 层。
        short_layer_count = 1
    else:
        short_layer_count = min(short_layer_count, short_risk_cap)

    reasons = []
    if trend5 is not None and trend5 < 0:
        reasons.append("ETF近5日偏弱")
    if trend20 is not None and trend20 < 0:
        reasons.append("ETF近20日偏弱")
    if etf_price is not None and etf_ma is not None and etf_price < etf_ma:
        reasons.append("ETF低于MA5")
    if above_ma20 is False and ma20_slope is not None and ma20_slope > 0:
        reasons.append("跌破MA20但MA20上行（强势回调）")
    elif short_broken:
        reasons.append("跌破MA20且MA20下行（弱势破位）")
    if premium is not None and premium > 5:
        reasons.append("溢价偏高但超短线不作绝对否决")
    if price and support20 and price / support20 <= 1.03:
        reasons.append("接近短线支撑")
    if not reasons:
        reasons.append("短线动量、趋势和关键位信号中性")

    return {
        "total": total, "scores": scores, "action": action, "layers": layers,
        "layer_count": short_layer_count, "risk_cap": short_risk_cap,
        "reason": "；".join(reasons),
        "add_condition": "ETF站上MA5、近5日转强且溢价率不再扩大",
        "reduce_condition": "跌破短线支撑或收盘跌破MA5",
    }


def fmt_optional(value, suffix="", decimals=2):
    """格式化数值并去掉多余小数位。

    注意：只有当存在小数点时才去掉末尾的 0，否则 70 会被 rstrip 成 7、29100 会变成 291。
    """
    if value is None:
        return "--"
    text = ("%%.%df" % decimals) % value
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text + suffix


def rsi_zone(rsi):
    """RSI 区间解读。"""
    if rsi is None:
        return "区间 --"
    if rsi >= 75:
        return "超买"
    if rsi >= 65:
        return "偏强"
    if rsi > 45:
        return "中性"
    if rsi > 30:
        return "偏弱"
    return "超卖"


def macd_zone(dif, dea):
    """MACD 金叉/死叉与零轴位置解读。"""
    if dif is None or dea is None:
        return "数据 --"
    cross = "金叉" if dif >= dea else "死叉"
    side = "零轴上" if dif >= 0 else "零轴下"
    return "%s·%s" % (cross, side)


def fmt_level_space(level, current):
    """返回关键位相对现价的绝对距离，分别展示为下跌/上涨空间。"""
    if level is None or current is None or current == 0:
        return "--"
    return "%.2f%%" % abs((level / current - 1) * 100)


def quant_evaluation(quote, valuation, high_info, levels, etf, risk_info=None, volume=None):
    """综合估值、趋势、动量、位置、ETF执行条件和风险，输出0~100评分及仓位。"""
    price = quote.get("price") if quote else None
    daily_pct = quote.get("pct") if quote else None
    high = high_info.get("high") if high_info else None
    drawdown = (price / high - 1) * 100 if price and high else None
    etf_price = etf.get("price")
    etf_ma = etf.get("ma")
    etf_trend5 = etf.get("trend5")
    etf_trend20 = etf.get("trend20")
    premium = etf.get("premium")
    premium_avg5 = etf.get("premium_avg5")
    amplitude = quote.get("amplitude") if quote else None
    tracking_gap = None
    if daily_pct is not None and etf.get("pct") is not None:
        tracking_gap = etf.get("pct") - daily_pct

    # 趋势35分，拆成四个子项，区分「长期上升中回调」与「长期下跌中反弹」：
    #   短期结构 8 + 中期结构 12 + 趋势强度 8 + 均线排列 7
    ma60_slope = (risk_info or {}).get("ma60_slope")
    ma60_dev = (risk_info or {}).get("ma60_dev")
    ma_state = (risk_info or {}).get("ma_state")
    above_ma60 = (risk_info or {}).get("above_ma60")

    # 短期结构：价格相对 MA5（ETF 自身均线）
    short_structure = 0
    if etf_price is not None and etf_ma is not None:
        short_structure = 8 if etf_price >= etf_ma else 3

    # 中期结构：MA60 斜率方向 + 价格站位（中期健康度的核心）
    mid_structure = 6
    if ma60_slope is not None and above_ma60 is not None:
        if ma60_slope > 0.5 and above_ma60:
            mid_structure = 12      # 中期向上且站上 MA60：健康
        elif ma60_slope > 0 and above_ma60:
            mid_structure = 10
        elif ma60_slope > 0 and not above_ma60:
            mid_structure = 6       # 中期向上但跌破 MA60：回调中
        elif ma60_slope <= 0 and above_ma60:
            mid_structure = 5       # 中期走平/向下但仍在 MA60 上方：反弹
        else:
            mid_structure = 2       # 中期向下且跌破 MA60：弱势
    elif above_ma60 is not None:
        mid_structure = 8 if above_ma60 else 3

    # 趋势强度：近20日涨幅分档
    strength = 0
    if etf_trend20 is not None:
        strength = (8 if etf_trend20 >= 8 else 6 if etf_trend20 >= 3
                    else 4 if etf_trend20 >= 0 else 2 if etf_trend20 > -5 else 0)

    # 均线排列：多头/空头/纠缠
    align_score = {"多头排列": 7, "均线纠缠": 4}.get(ma_state, 0 if ma_state == "空头排列" else 4)

    trend_score = min(35, short_structure + mid_structure + strength + align_score)

    # 动量15分：近5日走势与最近一日波动。
    momentum_score = 0
    if etf_trend5 is not None:
        momentum_score += 8 if etf_trend5 >= 2 else 6 if etf_trend5 >= 0 else 3 if etf_trend5 >= -3 else 0
    if daily_pct is not None:
        momentum_score += 7 if daily_pct >= 0 else 4 if daily_pct > -1.5 else 1

    position_score = 0
    if drawdown is not None:
        position_score += 10 if drawdown <= -10 else 8 if drawdown <= -5 else 5 if drawdown <= -3 else 2
    support20 = (levels.get("20日") or {}).get("low")
    if price and support20 and price / support20 <= 1.03:
        position_score += 5
    elif price and support20 and price / support20 <= 1.08:
        position_score += 3
    position_score = min(position_score, 15)

    etf_score = 0
    if premium is not None:
        etf_score += 12 if premium <= 2 else 8 if premium <= 5 else 3 if premium <= 8 else 0
    if etf_price is not None and etf_ma is not None and etf_price >= etf_ma:
        etf_score += 4
    if premium is not None and premium_avg5 is not None:
        if premium <= premium_avg5 - 1:
            etf_score += 4
        elif premium <= premium_avg5:
            etf_score += 2
    etf_score = min(etf_score, 20)

    # 风险10分：溢价3、ATR3、波动率2、指数/ETF偏离2。
    risk_score = 3 if premium is None or premium <= 2 else 1 if premium <= 5 else 0
    if risk_info and risk_info.get("atr_ratio") is not None:
        risk_score += 3 if risk_info["atr_ratio"] <= 1.2 else 2 if risk_info["atr_ratio"] <= 1.5 else 1 if risk_info["atr_ratio"] <= 2 else 0
    else:
        risk_score += 1
    if risk_info and risk_info.get("vol_ratio") is not None:
        risk_score += 2 if risk_info["vol_ratio"] <= 1.2 else 1 if risk_info["vol_ratio"] <= 1.5 else 0
    else:
        risk_score += 1
    if tracking_gap is None or abs(tracking_gap) <= 1:
        risk_score += 2
    elif abs(tracking_gap) <= 1.5:
        risk_score += 1
    if daily_pct is not None and abs(daily_pct) > 2:
        risk_score = min(risk_score, 4)
    if amplitude is not None and amplitude > 3:
        risk_score = min(risk_score, 4)
    risk_score = min(risk_score, 10)

    # 量能（波段视角）：放量确认趋势有效，缩量代表趋势动能不足。
    vol_eval = volume_evaluation(volume or {}, intraday=False)
    # 技术面（波段视角）：RSI 定位超买超卖，均线排列确认中期结构。
    rsi = (risk_info or {}).get("rsi14")
    ma_state = (risk_info or {}).get("ma_state")
    tech_score = 0
    if rsi is not None:
        if rsi <= 30:
            tech_score += 5
        elif rsi <= 45:
            tech_score += 3
        elif rsi >= 75:
            tech_score -= 4
        elif rsi >= 65:
            tech_score -= 2
    if ma_state == "多头排列":
        tech_score += 5
    elif ma_state == "空头排列":
        tech_score -= 5
    # MACD 中期动能 + 布林带位置（波段视角）
    macd_dif = (risk_info or {}).get("macd_dif")
    macd_dea = (risk_info or {}).get("macd_dea")
    boll_pos = (risk_info or {}).get("boll_pos")
    if macd_dif is not None and macd_dea is not None:
        if macd_dif >= macd_dea and macd_dif >= 0:
            tech_score += 4
        elif macd_dif >= macd_dea:
            tech_score += 2
        elif macd_dif < 0:
            tech_score -= 4
        else:
            tech_score -= 2
    if boll_pos is not None:
        if boll_pos <= 10:
            tech_score += 3
        elif boll_pos >= 90:
            tech_score -= 2
    volume_score = 0
    if vol_eval["label"] == "显著放量":
        volume_score = 15 if trend_score >= 20 else 8
    elif vol_eval["label"] == "温和放量":
        volume_score = 10
    elif vol_eval["label"] == "小幅缩量":
        volume_score = 6
    elif vol_eval["label"] == "明显缩量":
        volume_score = 3

    scores = {
        "趋势": trend_score,
        "动量": momentum_score,
        "位置": position_score,
        "ETF执行": etf_score,
        "量能": volume_score,
        "技术": tech_score,
        "风险": risk_score,
    }
    # PE/PB及百分位不参与操作建议；其余维度合计上限 35+15+15+20+15+16+10=126，归一化到 100。
    raw_total = sum(scores.values())
    total = min(100, max(0, round(raw_total / 126 * 100)))
    if total >= 80:
        action, layers = "加仓", "4～5层（80%～100%）"
    elif total >= 65:
        action, layers = "加仓", "3层（60%）"
    elif total >= 50:
        action, layers = "观望", "1～2层（20%～40%）"
    elif total >= 35:
        action, layers = "观望", "0～1层（0%～20%）"
    else:
        action, layers = "减仓", "0层（防守）"

    reasons = []
    if etf_trend5 is not None and etf_trend5 < 0:
        reasons.append("ETF近5日偏弱")
    if etf_trend20 is not None and etf_trend20 < 0:
        reasons.append("ETF近20日偏弱")
    if etf_price is not None and etf_ma is not None and etf_price < etf_ma:
        reasons.append("ETF低于MA5")
    if premium is not None and premium > 5:
        reasons.append("ETF溢价过高")
    if premium is not None and premium_avg5 is not None and premium < premium_avg5:
        reasons.append("溢价较5日均值回落")
    if tracking_gap is not None and abs(tracking_gap) > 1.5:
        reasons.append("ETF与纳指当日走势偏离")
    if amplitude is not None and amplitude > 3:
        reasons.append("纳指日内波动较大")
    if vol_eval["label"] in ("显著放量", "温和放量"):
        reasons.append("量能" + vol_eval["label"])
    elif vol_eval["label"] in ("明显缩量", "小幅缩量"):
        reasons.append("量能" + vol_eval["label"])
    if rsi is not None and rsi <= 30:
        reasons.append("RSI超卖")
    elif rsi is not None and rsi >= 75:
        reasons.append("RSI超买")
    if ma_state == "空头排列":
        reasons.append("纳指均线空头排列")
    if ma60_slope is not None and ma60_slope > 0 and above_ma60:
        reasons.append("中期结构健康（MA60上行且站上）")
    elif above_ma60 is False and ma60_slope is not None and ma60_slope <= 0:
        reasons.append("中期结构偏弱（MA60下行且跌破）")

    hard_veto = (premium is not None and premium > 5) or (
        etf_price is not None and etf_ma is not None and etf_price < etf_ma and
        etf_trend5 is not None and etf_trend5 < 0)
    if hard_veto and action == "加仓":
        action, layers = "观望", "0～1层（0%～20%）"
        reasons.append("执行条件未满足，暂不追价")
    if premium is not None and premium > 8:
        action, layers = "观望", "0～1层（0%～20%）"

    risk_cap = 5 if risk_score >= 8 else 3 if risk_score >= 6 else 2 if risk_score >= 4 else 1
    # 中期结构限制：MA60 下行且价格在其下方时，波段不重仓，避免在中期弱势中加仓。
    mid_weak = (above_ma60 is False and ma60_slope is not None and ma60_slope <= 0)
    if mid_weak:
        risk_cap = min(risk_cap, 2)
    desired_layers = 5 if total >= 80 else 3 if total >= 65 else 2 if total >= 50 else 1 if total >= 35 else 0
    if desired_layers > risk_cap:
        action = "观望" if risk_cap <= 2 else "试仓"
        layers = "0～%d层（0%%～%d%%）" % (risk_cap, risk_cap * 20)
        reasons.append("ATR/波动率或中期结构触发仓位上限")
    swing_layer_count = min(desired_layers, risk_cap)

    add_condition = "评分≥65、溢价率回落至5%以下、ETF站上MA5且近20日转强"
    reduce_condition = "评分<35，或跌破短线支撑/中期支撑失守"
    return {
        "total": total,
        "scores": scores,
        "action": action,
        "layers": layers,
        "layer_count": swing_layer_count,
        "risk_cap": risk_cap,
        "reason": "；".join(reasons) or "各项量化指标中性",
        "add_condition": add_condition,
        "reduce_condition": reduce_condition,
    }


def build_premarket_message(intraday=False):
    """构建交易日盘前播报，区分纳指指数信号与 ETF 实际交易评估。"""
    now = datetime.now(timezone(timedelta(hours=8)))
    quote = query_ndx_quote()
    valuation = query_ndx_valuation()
    high_info = query_ndx_high()
    ndx_levels = query_ndx_levels()
    risk_info = query_ndx_risk()
    # ⚠️ 这三个是「可选增强块」，任何一个出错都不应拖垮整份播报。
    #    实测教训：query_ndx_etf() 内一处格式化字符串写错（'5%' 应为 '5%%'），
    #    触发时抛 ValueError，而 build_premarket_message 被 main() 直接调用、
    #    上层无 try —— 整份盘前播报会崩掉，且被 cron 静默吞掉。
    #    故隔离可选块：拿不到就降级为 None，其余部分照常发出。
    try:
        etf = query_ndx_etf()
    except Exception as e:
        print("[WARN] ETF 数据获取失败，已降级: %s: %s" % (type(e).__name__, str(e)[:120]))
        etf = None
    try:
        etf_amount = query_etf_amount() if intraday else None
    except Exception as e:
        print("[WARN] ETF 成交额获取失败，已降级: %s" % str(e)[:120])
        etf_amount = None
    try:
        volume = query_etf_volume()
        vol_eval = volume_evaluation(volume, intraday=intraday)
    except Exception as e:
        print("[WARN] 成交量数据获取失败，已降级: %s" % str(e)[:120])
        volume, vol_eval = None, None
    # 空值兜底：下游有 15 处直接 etf.get(...) / vol_eval[...] 访问，
    # 用空容器替代 None，避免降级后再触发 AttributeError。
    if etf is None:
        etf = {}
    if vol_eval is None:
        vol_eval = {"label": "数据缺失", "note": "成交量数据未取到"}

    price = quote.get("price") if quote else None
    pct = quote.get("pct") if quote else None
    high = high_info.get("high") if high_info else None
    drawdown = (price / high - 1) * 100 if price and high else None
    quant = quant_evaluation(quote, valuation, high_info, ndx_levels, etf, risk_info, volume)
    short_quant = short_quant_evaluation(quote, high_info, ndx_levels, etf, risk_info, volume)
    action = quant["action"]
    icon = "🟢" if action == "加仓" else "🔴" if action == "减仓" else "🟡"

    raw_timestamp = quote.get("timestamp") if quote else ""
    us_trade_date = raw_timestamp.split(" ", 1)[0] if raw_timestamp else "最近可用美股交易日"
    period = high_info.get("period", "历史样本") if high_info else "历史样本"
    etf_action = etf.get("action", "暂不判断")
    etf_premium = etf.get("premium")
    support20_value = (ndx_levels.get("20日") or {}).get("low")
    support60_value = (ndx_levels.get("60日") or {}).get("low")
    resistance20_value = (ndx_levels.get("20日") or {}).get("high")
    resistance60_value = (ndx_levels.get("60日") or {}).get("high")
    support20 = fmt_optional(support20_value, decimals=0)
    support60 = fmt_optional(support60_value, decimals=0)
    resistance20 = fmt_optional(resistance20_value, decimals=0)
    resistance60 = fmt_optional(resistance60_value, decimals=0)
    support20_space = fmt_level_space(support20_value, price)
    support60_space = fmt_level_space(support60_value, price)
    resistance20_space = fmt_level_space(resistance20_value, price)
    resistance60_space = fmt_level_space(resistance60_value, price)
    risk_score = quant["scores"].get("风险", 0)
    short_positive = short_quant["action"] in ("加仓", "试仓")
    swing_positive = quant["action"] in ("加仓", "试仓")
    if short_quant["action"] == "减仓" and quant["action"] != "加仓":
        final_action = "减仓"
    elif short_positive and swing_positive:
        final_action = "加仓" if short_quant["action"] == "加仓" and quant["action"] == "加仓" else "试仓"
    else:
        final_action = "观望"
    final_layers = min(short_quant["layer_count"], quant["layer_count"])
    # 上限是「最多允许几层」，取两个周期中较宽松者，且至少 1 层；
    # 短周期单日风险分波动较大，不适合把整体上限直接压成 0 层。
    final_cap = max(1, max(short_quant["risk_cap"], quant["risk_cap"]))
    final_layers_text = "0～%d层（0%%～%d%%）" % (final_layers, final_layers * 20)
    final_cap_text = "%d层（%d%%）" % (final_cap, final_cap * 20)
    # ETF 自身关键位（可直接挂单），与纳指点位并列展示
    etf_levels = etf.get("levels") or {}
    etf_price = etf.get("price")
    etf_low20 = (etf_levels.get("20日") or {}).get("low")
    etf_high20 = (etf_levels.get("20日") or {}).get("high")
    etf_low60 = (etf_levels.get("60日") or {}).get("low")
    etf_high60 = (etf_levels.get("60日") or {}).get("high")
    etf_level_lines = []
    if etf_price and etf_low20:
        etf_level_lines = [
            "🎯 **ETF关键位**",
            "↘️ %s（短线支撑，↓%s）｜跌破：暂停加仓" % (
                fmt_optional(etf_low20, decimals=3), fmt_level_space(etf_low20, etf_price)),
            "⬆️ %s（短线阻力，↑%s）｜站稳：小仓加仓" % (
                fmt_optional(etf_high20, decimals=3), fmt_level_space(etf_high20, etf_price)),
        ]
        if etf_low60:
            etf_level_lines = [
                etf_level_lines[0],
                "↘️ %s（短线支撑，↓%s）｜跌破：暂停加仓" % (
                    fmt_optional(etf_low20, decimals=3), fmt_level_space(etf_low20, etf_price)),
                "↘️ %s（中期支撑，↓%s）｜跌破：降低仓位" % (
                    fmt_optional(etf_low60, decimals=3), fmt_level_space(etf_low60, etf_price)),
                "⬆️ %s（短线阻力，↑%s）｜站稳：小仓加仓" % (
                    fmt_optional(etf_high20, decimals=3), fmt_level_space(etf_high20, etf_price)),
                "⬆️ %s（中期阻力，↑%s）｜站稳：趋势转强" % (
                    fmt_optional(etf_high60, decimals=3), fmt_level_space(etf_high60, etf_price)),
            ]
    risk_cap = 5 if risk_score >= 8 else 3 if risk_score >= 6 else 2 if risk_score >= 4 else 1
    if risk_info:
        risk_line = "⚠️ **波动风险**：ATR14 %s（历史倍数%s）｜5日波动率 %s｜20日波动率 %s｜仓位上限%s层" % (
            fmt_optional(risk_info.get("atr_pct"), "%"),
            fmt_optional(risk_info.get("atr_ratio"), "倍"),
            fmt_optional(risk_info.get("vol5"), "%"),
            fmt_optional(risk_info.get("vol20"), "%"), risk_cap)
    else:
        risk_line = "⚠️ **波动风险**：ATR/波动率数据暂缺，建议控制仓位"

    lines = [
        "📣 **【盘中播报】**" if intraday else "📣 **【盘前播报】**",
        "",
        "🌙 **纳斯达克100（NDX）**",
        "📈 **行情**：%s（%s）" % (fmt_optional(price), fmt_optional(pct, "%")),
        "🗓️ **美股最近交易日**：%s" % us_trade_date,
        "📊 **估值（%s）**：PE(TTM) %s" % (
            valuation.get("basis", "接口口径"), fmt_optional(valuation.get("pe"))),
        "📉 **距历史高点**：高点 %s（%s）｜回撤 %s" % (
            fmt_optional(high), period, fmt_optional(drawdown, "%")),
        "",
        "📦 **纳指ETF易方达（159696）**",
        "📈 **行情走势**：%s（%s）｜近5日 %s｜近20日 %s｜MA5 %s%s" % (
            fmt_optional(etf.get("price"), decimals=3), fmt_optional(etf.get("pct"), "%"),
            fmt_optional(etf.get("trend5"), "%"), fmt_optional(etf.get("trend20"), "%"),
            fmt_optional(etf.get("ma")),
            "（上一交易日收盘）" if etf.get("price_is_fallback") else ""),
        "💰 **溢价率**：%s（%s）%s" % (
            fmt_optional(etf_premium, "%"), etf.get("premium_level", "数据缺失"),
            ("｜近%s日 %s分位" % (etf.get("premium_days"),
                                fmt_optional(etf.get("premium_pct"), "%", decimals=0)))
            if etf.get("premium_pct") is not None else ""),
        "📊 **量能**：%s｜%s" % (
            vol_eval["label"], vol_eval["note"]),
        "📐 **技术面**：RSI14 %s｜均线 %s｜%s" % (
            fmt_optional((risk_info or {}).get("rsi14"), decimals=1),
            (risk_info or {}).get("ma_state") or "--",
            rsi_zone((risk_info or {}).get("rsi14"))),
        "📊 **MACD**：%s｜DIF %s｜DEA %s" % (
            macd_zone((risk_info or {}).get("macd_dif"), (risk_info or {}).get("macd_dea")),
            fmt_optional((risk_info or {}).get("macd_dif"), decimals=1),
            fmt_optional((risk_info or {}).get("macd_dea"), decimals=1)),
        "📉 **布林带**：上轨 %s｜中轨 %s｜下轨 %s｜位置 %s" % (
            fmt_optional((risk_info or {}).get("boll_up"), decimals=0),
            fmt_optional((risk_info or {}).get("boll_mid"), decimals=0),
            fmt_optional((risk_info or {}).get("boll_low"), decimals=0),
            fmt_optional((risk_info or {}).get("boll_pos"), "%", decimals=0)),
        "📏 **60日区间**：高 %s｜低 %s｜位置 %s" % (
            fmt_optional((risk_info or {}).get("sr_high"), decimals=0),
            fmt_optional((risk_info or {}).get("sr_low"), decimals=0),
            fmt_optional((risk_info or {}).get("sr_pos"), "%", decimals=0)),
    ] + ([
        "💵 **上午成交额**：%s" % (fmt_yi(etf_amount) if etf_amount else "--"),
    ] if intraday else []) + [
        "",
        "⚡ **超短线（1～3日）**：%s｜建议仓位：%s" % (short_quant["action"], short_quant["layers"]),
        "🧮 **超短线评分**：%d/100" % short_quant["total"],
        "",
        "🟡 **波段（1～4周）**：%s｜建议仓位：%s" % (quant["action"], quant["layers"]),
        "🧮 **波段评分**：%d/100" % quant["total"],
        "📐 **中期结构**：MA60 %s｜价格%sMA60｜%s" % (
            ("上行" if (risk_info or {}).get("ma60_slope") and risk_info["ma60_slope"] > 0
             else "下行" if (risk_info or {}).get("ma60_slope") and risk_info["ma60_slope"] <= 0 else "--"),
            ("位于" if (risk_info or {}).get("above_ma60") else "低于")
            if (risk_info or {}).get("above_ma60") is not None else "--",
            (risk_info or {}).get("ma_state") or "--"),
        "",
        "📦 **ETF综合建议**：**%s**｜建议仓位：%s｜上限：%s" % (
            final_action, final_layers_text, final_cap_text),
        "",
        risk_line,
        "",
        "🎯 **纳指关键位**",
        "↘️ %s（短线支撑，↓%s）｜跌破：暂停加仓" % (support20, support20_space),
        "↘️ %s（中期支撑，↓%s）｜跌破：降低仓位" % (support60, support60_space),
        "⬆️ %s（短线阻力，↑%s）｜站稳+溢价≤5%%：小仓加仓" % (resistance20, resistance20_space),
        "⬆️ %s（中期阻力，↑%s）｜站稳2日：趋势转强" % (resistance60, resistance60_space),
        "⚠️ 指数突破但ETF溢价过高时，不追价加仓。",
    ] + etf_level_lines + [
        "ℹ️ 纳指点位用于判方向；ETF价位可直接挂单，按各自近20/60日区间计算。",
        "---",
        "⚠️ **免责声明**：以上为程序化盘前信息整理和规则信号，仅供参考，不构成投资建议。",
    ]
    return "\n".join(lines)


def query_amount(query, field_prefix):
    """查询带日期后缀的金额字段（如 成交额[20260902]），返回元或 None。"""
    it = query_item(query)
    if not it:
        return None
    for k, v in it.items():
        if k.startswith(field_prefix):
            try:
                return float(v)
            except Exception:
                return None
    return None


def fmt_yi(yuan):
    """元 → 亿/万亿 可读字符串。"""
    yi = yuan / 1e8
    if abs(yi) >= 10000:
        return "%.2f万亿" % (yi / 10000)
    return "%.2f亿" % yi


def fmt_chg(chg):
    if chg is None:
        return ""
    return "%+.2f%%" % chg


def wu2198_texts(day, before_time=None):
    """取 wu2198 当天（可选时间点前）发言原文列表，去 VIP 标记与语气词。"""
    conn = connect_db(DB_PATH)
    try:
        cur = conn.cursor()
        sql = ("select content from kol_records "
               "where kol_name='wu2198' and record_date like ?")
        params = [day + "%"]
        if before_time:
            sql += " and record_date <= ?"
            params.append(day + " " + before_time)
        sql += " order by record_date asc"
        rows = [r[0] for r in cur.execute(sql, params)]
    finally:
        conn.close()

    texts = []
    for t in rows:
        t = clean_wu2198_text(t)
        if t:
            texts.append(t)
    return texts


def raw_view_line(texts):
    """AI 不可用时的兜底：取最后 2 条原文截断拼接。"""
    out = []
    for t in texts[-2:]:
        if len(t) > 60:
            t = t[:60] + "…"
        out.append(t)
    return "；".join(out)


def read_view_line(lunch):
    """读取蜜蜂任务预生成的一句话观点；不存在/为空返回 None。"""
    fn = "_view_lunch.txt" if lunch else "_view_close.txt"
    try:
        with open(os.path.join(SKILL_DIR, "data", fn), encoding="utf-8") as f:
            t = f.read().strip()
        return t or None
    except Exception:
        return None


def key_levels_line():
    """从 alert_levels.json 汇总关键位（确定性，非主观判断）。"""
    try:
        with open(LEVELS_FILE, encoding="utf-8") as f:
            levels = json.load(f)
    except Exception:
        levels = {}
    parts = []
    for name in ("上证指数", "创业板指"):
        entries = levels.get(name, [])
        lv = "/".join(str(e["level"]) for e in entries if e.get("level") is not None)
        if lv:
            parts.append("%s %s" % (DISPLAY.get(name, name), lv))
    return "；".join(parts) or "关键位待更新"


def is_trading_day():
    d = datetime.now(timezone(timedelta(hours=8)))
    if d.weekday() >= 5:
        return False
    return d.strftime("%Y-%m-%d") not in load_holidays(SKILL_DIR)


def build_message(lunch=False, premarket=False, intraday=False):
    if premarket:
        return build_premarket_message(intraday=False)
    if intraday:
        return build_premarket_message(intraday=True)

    now = datetime.now(timezone(timedelta(hours=8)))
    day = now.strftime("%Y-%m-%d")

    idx_lines = []
    for name in INDICES:
        r = query_index(name)
        if not r:
            print("[ABORT] %s 行情查询失败，不发消息" % name)
            return None
        price, chg = r
        chg_txt = fmt_chg(chg)
        if chg_txt:
            chg_txt = "（%s）" % chg_txt
        idx_lines.append("📈 **%s**：%s%s" % (DISPLAY[name], _fmt_num(price), chg_txt))

    turnover = query_amount("两市成交额", "成交额")
    fund = query_amount("两市主力资金净流入", "主力净买入额")
    if turnover is None or fund is None:
        print("[ABORT] 成交额/主力资金查询失败，不发消息")
        return None

    if fund >= 0:
        fund_text = "净流入 " + fmt_yi(fund)
    else:
        fund_text = "净流出 " + fmt_yi(abs(fund))

    texts = wu2198_texts(day, before_time=("11:35" if lunch else None))
    if not texts:
        views = "今日暂无发言"
    else:
        views = read_view_line(lunch) or raw_view_line(texts)

    title = "每日午间汇总" if lunch else "每日收盘汇总"
    focus_label = "下午关注" if lunch else "明日关注"
    time_label = "%s（午间）" % day if lunch else day

    lines = [
        "📊 **【%s】**" % title,
        "🕐 **时间**：%s" % time_label,
    ] + idx_lines + [
        "💰 **两市成交额**：%s" % fmt_yi(turnover),
        "💸 **主力资金**：%s" % fund_text,
        "💬 **wu2198观点**：%s" % views,
        "🎯 **%s**：%s" % (focus_label, key_levels_line()),
        "---",
        "⚠️ **免责声明**：以上内容仅为信息整理与观点复盘，仅供参考，不构成投资建议。",
    ]
    return "\n".join(lines)


def _fmt_num(v):
    """价格保留两位小数，去掉多余 0。"""
    return ("%.2f" % v).rstrip("0").rstrip(".")


def _config_value(key):
    try:
        with open(os.path.join(SKILL_DIR, "data", "local_config.env"), encoding="utf-8") as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.strip().split("=", 1)[1]
    except Exception:
        pass
    return ""


def _native_lark_cli():
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        p = os.path.join(appdata, "bee_ai_test", "agent-runtime", "npm-global",
                         "node_modules", "@larksuite", "cli", "bin", "lark-cli.exe")
        if os.path.exists(p):
            return p
    return ""


def build_premarket_card(msg):
    """把盘前播报拆成分区卡片，失败时由调用方回退 Markdown。"""
    lines = msg.splitlines()
    if lines and lines[0].startswith("📣"):
        lines = lines[1:]
    sections = []
    current = []
    for line in lines:
        if not line.strip():
            if current:
                sections.append("\n".join(current).strip())
                current = []
            continue
        if line.strip() == "---":
            continue
        current.append(line)
    if current:
        sections.append("\n".join(current).strip())

    elements = []
    for idx, section in enumerate(sections):
        if not section:
            continue
        if "免责声明" in section:
            elements.append({"tag": "markdown", "content": section, "text_size": "notation"})
            continue
        background = "blue-50" if idx % 3 == 0 else "grey-50" if idx % 3 == 1 else "violet-50"
        border = "blue-100" if idx % 3 == 0 else "grey-200" if idx % 3 == 1 else "violet-100"
        elements.append({
            "tag": "interactive_container",
            "width": "fill",
            "has_border": True,
            "border_color": border,
            "corner_radius": "8px",
            "background_style": background,
            "padding": "12px 12px 12px 12px",
            "vertical_spacing": "4px",
            "elements": [{"tag": "markdown", "content": section.replace("\\n", "<br>")}],
        })
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    # 标题按内容自动识别，避免盘中播报显示成「盘前播报」。
    if "【盘中播报】" in (msg or ""):
        card_title = "盘中播报"
    elif "【午间汇总】" in (msg or ""):
        card_title = "午间汇总"
    elif "【每日收盘汇总】" in (msg or ""):
        card_title = "收盘汇总"
    else:
        card_title = "盘前播报"
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": card_title},
            "subtitle": {"tag": "plain_text", "content": now},
            "template": "blue",
            "icon": {"tag": "standard_icon", "token": "myai_colorful"},
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 20px 12px",
            "vertical_spacing": "8px",
            "elements": elements,
        },
    }


def send(msg, dry_run=False, tag="summary"):
    if dry_run:
        print("---- DRY-RUN（不发群）----")
        print(msg)
        print("--------------------------")
        return True
    day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    user_id = _config_value("USER_OPEN_ID")
    native = _native_lark_cli()
    try:
        # 盘前播报统一私信，不再发送到群聊；优先使用 Card 2.0 分区卡片。
        if native and user_id:
            # 飞书幂等键上限 50 字符：tag 截断，保证不超限。
            idem = "ms_%s_%s" % (tag[:16], day)
            # 盘前/盘中等播报统一使用 Card 2.0；只有普通汇总类走 Markdown。
            card_tags = ("premarket", "intraday", "pm_", "card_")
            card = build_premarket_card(msg) if tag.startswith(card_tags) else None
            if card is not None:
                args = [native, "im", "+messages-send", "--user-id", user_id,
                        "--as", "bot", "--idempotency-key", idem,
                        "--msg-type", "interactive", "--content", json.dumps(card, ensure_ascii=False), "--json"]
            else:
                args = [native, "im", "+messages-send", "--user-id", user_id,
                        "--as", "bot", "--idempotency-key", idem,
                        "--markdown", msg, "--json"]
            r = subprocess.run(args, capture_output=True, text=True, timeout=45, cwd=SKILL_DIR, encoding='utf-8', errors='replace')
            try:
                data = json.loads(r.stdout or "{}")
            except json.JSONDecodeError:
                data = {}
            if r.returncode == 0 and data.get("ok"):
                print("[OK] 汇总已发送")
                return True
            print("[WARN] 汇总发送失败: %s" % ((r.stderr or r.stdout or "").strip()[:200]))
            return False

        # 没有原生 exe 时保留文件方式兜底，兼容旧环境。
        tmp_rel = os.path.join("data", "_market_summary_%s.txt" % tag).replace("\\", "/")
        tmp_path = os.path.join(SKILL_DIR, tmp_rel.replace("/", os.sep))
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(msg)
        try:
            r = subprocess.run([BASH, NOTIFY, "@" + tmp_rel],
                               capture_output=True, text=True, timeout=30, cwd=SKILL_DIR, encoding='utf-8', errors='replace')
            if r.returncode != 0:
                print("[WARN] 汇总发送失败: %s" % ((r.stderr or r.stdout or "").strip()[:200]))
                return False
            print("[OK] 汇总已发送")
            return True
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    except Exception as e:
        print("[WARN] 发送失败: %s" % e)
        return False


def already_sent(key):
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return key in f.read().splitlines()
    except Exception:
        return False


def mark_sent(key):
    try:
        with open(STATE_FILE, "a", encoding="utf-8") as f:
            f.write(key + "\n")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="每日盘前/午间/收盘汇总（零 token 后端版）")
    ap.add_argument("--premarket", action="store_true", help="交易日盘前播报（默认 08:45）")
    ap.add_argument("--intraday", action="store_true", help="交易日盘中播报（默认 10:00，基于早盘行情）")
    ap.add_argument("--lunch", action="store_true", help="午间汇总（11:35 前发言）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不发群")
    args = ap.parse_args()

    chosen = [x for x in (args.premarket, args.intraday, args.lunch) if x]
    if len(chosen) > 1:
        ap.error("--premarket / --intraday / --lunch 只能选一个")
    if not is_trading_day():
        print("[SKIP] 非交易日，跳过")
        return

    period = "premarket" if args.premarket else "intraday" if args.intraday else ("lunch" if args.lunch else "close")
    key = "%s|%s" % (datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"), period)
    if already_sent(key):
        print("[SKIP] %s 已发送过，跳过（防重复）" % key)
        return

    # ⚠️ 顶层保护：播报构建失败必须**显式报错**，不能静默退出。
    #    cron 把 stdout/stderr 都丢了（>/dev/null 2>&1），
    #    若这里不把异常打出来，故障会完全没有痕迹。
    try:
        msg = build_message(lunch=args.lunch, premarket=args.premarket,
                            intraday=args.intraday)
    except Exception as e:
        import traceback
        print("[ERROR] 播报构建失败（%s）：%s" % (type(e).__name__, e))
        traceback.print_exc()
        sys.exit(2)
    if msg is None:
        sys.exit(1)
    if send(msg, dry_run=args.dry_run, tag=period) and not args.dry_run:
        mark_sent(key)


if __name__ == "__main__":
    main()

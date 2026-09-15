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


def _headers():
    return {
        "Content-Type": "application/json",
        "X-Claw-Call-Type": "normal",
        "X-Claw-Skill-Id": "hithink-market-query",
        "X-Claw-Skill-Version": "1.0.0",
        "X-Claw-Plugin-Id": "none",
        "X-Claw-Plugin-Version": "none",
        "X-Claw-Trace-Id": secrets.token_hex(32),
    }


def query_item(query):
    """查询单条数据，返回首个 datas 项 dict 或 None。"""
    body = json.dumps({"query": query, "page": "1", "limit": "10",
                       "is_cache": "1", "expand_index": "true"}).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, headers=_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print("[WARN] %s 查询失败: %s" % (query, e))
        return None
    datas = data.get("datas", [])
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

# 名称白名单：只有明确是「纳斯达克100 / NDX」的返回才被接受，
# 避免把纳斯达克综合指数（IXIC，约26000）误当成纳斯达克100（NDX，约29000）。
NDX_NAME_HINTS = ("纳斯达克100", "nasdaq 100", "nasdaq-100", "ndx")


def _is_ndx_name(name):
    low = (name or "").strip().lower()
    if not low:
        return False
    if "综合" in low or "composite" in low or "ixic" in low:
        return False
    return any(h in low for h in NDX_NAME_HINTS)


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
    if not _is_ndx_name(name):
        # 标的名称不是纳斯达克100，宁可放弃该源，也不能用错指数数据。
        raise ValueError("东方财富返回标的非纳斯达克100: %r" % name[:30])
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
        if not _is_ndx_name(name):
            raise ValueError("腾讯返回标的非纳斯达克100: %r" % name[:30])
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


def query_ndx_risk():
    """读取公开Yahoo日线，计算ATR14及短中期实现波动率。失败时返回空。"""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENDX?range=180d&interval=1d"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
        with urllib.request.urlopen(req, timeout=20) as r:
            result = (json.loads(r.read().decode("utf-8", "replace")).get("chart", {}).get("result") or [None])[0]
        if not result:
            return {}
        q = result.get("indicators", {}).get("quote", [{}])[0]
        closes = q.get("close") or []
        highs = q.get("high") or []
        lows = q.get("low") or []
        rows = [(float(h), float(l), float(c)) for h, l, c in zip(highs, lows, closes)
                if h is not None and l is not None and c is not None and c > 0]
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
        return {
            "atr14": atr14,
            "atr_pct": atr14 / rows[-1][2] * 100,
            "atr_ratio": atr14 / avg_tr if avg_tr else None,
            "vol5": vol5,
            "vol20": vol20,
            "vol60": vol60,
            "vol_ratio": (vol5 / vol20) if vol5 is not None and vol20 else None,
            "data_date": str((result.get("meta") or {}).get("regularMarketTime", "")),
        }
    except Exception as e:
        print("[WARN] 纳斯达克100 ATR/波动率查询失败: %s" % e)
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
    if price_is_fallback:
        if ma is None:
            ma_values = []
            for key, value in item.items():
                if key.startswith("ma["):
                    try:
                        ma_values.append((key, float(value)))
                    except (TypeError, ValueError):
                        pass
            ma_values.sort(key=lambda x: x[0])
            if ma_values:
                ma = ma_values[-1][1]
        if ma is None:
            ma_values = []
            for key, value in item.items():
                if key.startswith("ma["):
                    try:
                        ma_values.append((key, float(value)))
                    except (TypeError, ValueError):
                        pass
            ma_values.sort(key=lambda x: x[0])
            if ma_values:
                ma = ma_values[-1][1]
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

    if premium is None:
        premium_level = "溢价率缺失"
        etf_action = "暂不判断"
        reason = "溢价率数据暂缺"
    elif premium >= 5:
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


def short_quant_evaluation(quote, high_info, levels, etf, risk_info=None):
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

    trend = 0
    if etf_price is not None and etf_ma is not None:
        trend += 15 if etf_price >= etf_ma else 4
    if trend5 is not None:
        trend += 10 if trend5 >= 2 else 7 if trend5 >= 0 else 4 if trend5 > -3 else 0
    if trend20 is not None and trend20 >= 0:
        trend += 5
    trend = min(trend, 30)

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

    scores = {"动量": overseas, "ETF趋势": trend, "溢价执行": execution,
              "关键位": position, "风险": risk}
    total = sum(scores.values())
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
    if value is None:
        return "--"
    text = ("%%.%df" % decimals) % value
    return text.rstrip("0").rstrip(".") + suffix


def fmt_level_space(level, current):
    """返回关键位相对现价的绝对距离，分别展示为下跌/上涨空间。"""
    if level is None or current is None or current == 0:
        return "--"
    return "%.2f%%" % abs((level / current - 1) * 100)


def quant_evaluation(quote, valuation, high_info, levels, etf, risk_info=None):
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

    # 趋势25分：价格与MA5、近20日方向、日线方向；避免把同一指标重复计权。
    trend_score = 0
    if etf_price is not None and etf_ma is not None:
        trend_score += 10 if etf_price >= etf_ma else 2
    if etf_trend20 is not None:
        trend_score += 15 if etf_trend20 >= 3 else 10 if etf_trend20 >= 0 else 5 if etf_trend20 > -5 else 0

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

    scores = {
        "趋势": trend_score,
        "动量": momentum_score,
        "位置": position_score,
        "ETF执行": etf_score,
        "风险": risk_score,
    }
    # PE/PB及百分位不参与操作建议；剩余可量化维度合计85分，归一化到100分。
    raw_total = sum(scores.values())
    total = round(raw_total / 85 * 100)
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

    hard_veto = (premium is not None and premium > 5) or (
        etf_price is not None and etf_ma is not None and etf_price < etf_ma and
        etf_trend5 is not None and etf_trend5 < 0)
    if hard_veto and action == "加仓":
        action, layers = "观望", "0～1层（0%～20%）"
        reasons.append("执行条件未满足，暂不追价")
    if premium is not None and premium > 8:
        action, layers = "观望", "0～1层（0%～20%）"

    risk_cap = 5 if risk_score >= 8 else 3 if risk_score >= 6 else 2 if risk_score >= 4 else 1
    desired_layers = 5 if total >= 80 else 3 if total >= 65 else 2 if total >= 50 else 1 if total >= 35 else 0
    if desired_layers > risk_cap:
        action = "观望" if risk_cap <= 2 else "试仓"
        layers = "0～%d层（0%%～%d%%）" % (risk_cap, risk_cap * 20)
        reasons.append("ATR/波动率触发仓位上限")
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
    etf = query_ndx_etf()
    etf_amount = query_etf_amount() if intraday else None

    price = quote.get("price") if quote else None
    pct = quote.get("pct") if quote else None
    high = high_info.get("high") if high_info else None
    drawdown = (price / high - 1) * 100 if price and high else None
    quant = quant_evaluation(quote, valuation, high_info, ndx_levels, etf, risk_info)
    short_quant = short_quant_evaluation(quote, high_info, ndx_levels, etf, risk_info)
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
        "💰 **溢价率**：%s（%s）" % (
            fmt_optional(etf_premium, "%"), etf.get("premium_level", "数据缺失")),
    ] + ([
        "💵 **上午成交额**：%s" % (fmt_yi(etf_amount) if etf_amount else "--"),
    ] if intraday else []) + [
        "",
        "⚡ **超短线（1～3日）**：%s｜建议仓位：%s" % (short_quant["action"], short_quant["layers"]),
        "🧮 **超短线评分**：%d/100" % short_quant["total"],
        "",
        "🟡 **波段（1～4周）**：%s｜建议仓位：%s" % (quant["action"], quant["layers"]),
        "🧮 **波段评分**：%d/100" % quant["total"],
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
        "ℹ️ 支撑/阻力采用纳斯达克100近20日、60日高低点参考；指数按美股最近交易日取数。",
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
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "盘前播报"},
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
            idem = "market_summary_private_%s_%s" % (tag, day)
            card = build_premarket_card(msg) if (tag.startswith("premarket") or tag.startswith("pm_") or tag.startswith("card_")) else None
            if card is not None:
                args = [native, "im", "+messages-send", "--user-id", user_id,
                        "--as", "bot", "--idempotency-key", idem,
                        "--msg-type", "interactive", "--content", json.dumps(card, ensure_ascii=False), "--json"]
            else:
                args = [native, "im", "+messages-send", "--user-id", user_id,
                        "--as", "bot", "--idempotency-key", idem,
                        "--markdown", msg, "--json"]
            r = subprocess.run(args, capture_output=True, text=True, timeout=45, cwd=SKILL_DIR)
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
                               capture_output=True, text=True, timeout=30, cwd=SKILL_DIR)
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

    msg = build_message(lunch=args.lunch, premarket=args.premarket, intraday=args.intraday)
    if msg is None:
        sys.exit(1)
    if send(msg, dry_run=args.dry_run, tag=period) and not args.dry_run:
        mark_sent(key)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票数据接口服务：免费公开源实时取数，供 AI 分析随时调用。

覆盖（全部免 key，已在服务器实测可达）：
  1. 实时行情        腾讯 qt.gtimg.cn        现价/涨跌/换手/PE/PB/流通·总市值/量比/涨跌停/内外盘
  2. 日/周/月/年 K线  腾讯 proxy.finance.qq.com newfqkline（qfq 前复权）
  3. 60 分钟 K线      新浪 quotes.sina.cn      getKLineData scale=60
  4. 财务指标        东财 emweb F10          营收/净利/ROE/经营现金流/毛利率/净利率/负债率/应收周转
  5. 商誉            东财 emweb F10 资产负债表 GOODWILL
  6. 主力资金流      新浪 vip.stock.finance   主力净流入/超大单（日级）
  7. 龙虎榜          东财 datacenter-web      上榜原因/净买入/买卖额
  8. 融资融券        东财 datacenter-web      融资余额/融券余额/融资净买入

说明：筹码分布、PE 历史分位、月/周线结构属「计算/历史长度依赖」指标，本服务不直接给现成值；
      新股上市初期月线/周线/89MA 天然不存在，属客观缺数，非接口缺失。

运行模式：
  serve：python stock_data_service.py --serve          # 常驻 HTTP（systemd 托管，127.0.0.1:8023）
  cli  ：python stock_data_service.py --code 601091 --kind all [--json]

HTTP 端点（serve）：
  GET  /health
  POST /skills/v1/query2data   body {"query":"601091"}   → {"datas":[...]}（hithink 风格）
  GET  /quote?code=601091  /kline?code=601091&period=day&limit=320
  GET  /finance?code=601091 /moneyflow?code=601091 /lhb?code=601091 /margin?code=601091

配置（环境变量）：
  STOCKDATA_SERVICE_HOST  默认 127.0.0.1
  STOCKDATA_SERVICE_PORT  默认 8023
  STOCKDATA_CACHE_TTL     行情/资金/K线缓存秒数，默认 120
  STOCKDATA_SLOW_TTL      财务/龙虎榜/融资缓存秒数，默认 3600
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("STOCKDATA_SERVICE_HOST", "127.0.0.1")
PORT = int(os.environ.get("STOCKDATA_SERVICE_PORT", "8023"))
CACHE_TTL = int(os.environ.get("STOCKDATA_CACHE_TTL", "120"))
SLOW_TTL = int(os.environ.get("STOCKDATA_SLOW_TTL", "3600"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: int = 15, referer: str = None) -> bytes:
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _norm_code(text) -> str:
    """从任意文本抽 6 位代码。"""
    m = re.search(r"\b(\d{6})\b", text or "")
    return m.group(1) if m else ""


def _prefix(code: str) -> str:
    c = str(code).zfill(6)
    if c[0] == "6" or c[0] == "9":
        return "sh"
    if c[0] in ("0", "3"):
        return "sz"
    if c[0] in ("4", "8"):
        return "bj"
    return "sh"


def _symbol(code: str) -> str:
    return _prefix(code) + str(code).zfill(6)


def _secid(code: str) -> str:
    p = _prefix(code)
    return ("1." if p == "sh" else "0.") + str(code).zfill(6)


def _num(x, nd=2):
    if x is None or x == "" or x == "-":
        return None
    try:
        f = float(x)
        return round(f, nd)
    except (ValueError, TypeError):
        return x


def _numstr(x, nd=2):
    v = _num(x, nd)
    return "" if v is None else str(v)


# ---------------------------------------------------------------------------
# 数据源
# ---------------------------------------------------------------------------

def fetch_quote(code: str) -> dict:
    """腾讯实时快照。"""
    sym = _symbol(code)
    raw = _http_get("https://qt.gtimg.cn/q=%s" % sym, timeout=10)
    txt = raw.decode("gbk", "replace")
    if '="' not in txt:
        return {"error": "行情接口无返回"}
    data = txt.split('="')[1].rsplit('"', 1)[0]
    p = data.split("~")

    def f(i):
        return p[i] if i < len(p) and p[i] not in ("", "-", "--") else None

    return {
        "name": f(1), "code": f(2),
        "price": _num(f(3)), "prev_close": _num(f(4)), "open": _num(f(5)),
        "change": _num(f(31)), "change_pct": _num(f(32)),
        "high": _num(f(33)), "low": _num(f(34)),
        "volume_hand": _num(f(36), 0), "amount_wan": _num(f(37), 0),
        "turnover_pct": _num(f(38)), "pe": _num(f(39)),
        "amp_pct": _num(f(43)),
        "float_mv_yi": _num(f(44)), "total_mv_yi": _num(f(45)),
        "pb": _num(f(46)), "limit_up": _num(f(47)), "limit_down": _num(f(48)),
        "vol_ratio": _num(f(49)),
        "outer_hand": _num(f(7), 0), "inner_hand": _num(f(8), 0),
        "time": (f(30) or "")[:14] if f(30) else "",
    }


def fetch_kline(code: str, period: str = "day", limit: int = 320) -> list:
    """K线。period: day/week/month/year 走腾讯；m5/m15/m30/m60 走新浪。"""
    sym = _symbol(code)
    if period in ("m5", "m15", "m30", "m60"):
        return _fetch_sina_min(sym, period, limit)
    url = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
           "?param=%s,%s,,,%d,qfq" % (sym, period, limit))
    try:
        d = json.loads(_http_get(url, timeout=15).decode("utf-8", "replace"))
    except Exception as e:
        return [{"error": "K线接口失败: %s" % type(e).__name__}]
    node = (d.get("data") or {}).get(sym) or {}
    rows = node.get(period) or node.get("qfq" + period) or []
    out = []
    for r in rows:
        try:
            out.append({"date": str(r[0]), "open": _num(r[1]), "close": _num(r[2]),
                        "high": _num(r[3]), "low": _num(r[4]),
                        "volume": _num(r[5], 0) if len(r) > 5 else None})
        except (TypeError, IndexError, ValueError):
            continue
    return out


def _fetch_sina_min(sym: str, period: str, limit: int) -> list:
    scale = period.lstrip("m")
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%%20_=/"
           "CN_MarketDataService.getKLineData?symbol=%s&scale=%s&ma=no&datalen=%d"
           % (sym, scale, limit))
    try:
        txt = _http_get(url, timeout=15).decode("utf-8", "replace")
    except Exception as e:
        return [{"error": "分钟K接口失败: %s" % type(e).__name__}]
    s, e = txt.find("["), txt.rfind("]")
    if s < 0 or e <= s:
        return []
    try:
        arr = json.loads(txt[s:e + 1])
    except Exception:
        return []
    out = []
    for r in arr:
        out.append({"date": r.get("day"), "open": _num(r.get("open")),
                    "close": _num(r.get("close")), "high": _num(r.get("high")),
                    "low": _num(r.get("low")), "volume": _num(r.get("volume"), 0),
                    "amount": _num(r.get("amount"), 0)})
    return out


def fetch_finance(code: str) -> dict:
    """东财 F10 主要财务指标 + 资产负债表商誉。"""
    sec = ("SH" if _prefix(code) == "sh" else
           ("SZ" if _prefix(code) == "sz" else "BJ")) + str(code).zfill(6)
    url = ("https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/"
           "ZYZBAjaxNew?type=0&code=%s" % sec)
    try:
        d = json.loads(_http_get(url, timeout=15).decode("utf-8", "replace"))
    except Exception as e:
        return {"error": "财务接口失败: %s" % type(e).__name__}
    data = d.get("data") or []
    rows = []
    for r in data[:5]:
        rows.append({
            "报告期": (r.get("REPORT_DATE") or "")[:10],
            "报告类型": r.get("REPORT_TYPE") or "",
            "营业总收入": _num(r.get("TOTALOPERATEREVE")),
            "归母净利润": _num(r.get("PARENTNETPROFIT")),
            "扣非净利润": _num(r.get("KCFJCXSYJLR")),
            "加权ROE": _num(r.get("ROEJQ")),
            "毛利率": _num(r.get("XSMLL")),
            "净利率": _num(r.get("XSJLL")),
            "资产负债率": _num(r.get("ZCFZL")),
            "每股经营现金流": _num(r.get("MGJYXJJE")),
            "每股收益": _num(r.get("EPSJB")),
            "每股净资产": _num(r.get("BPS")),
            "应收周转天数": _num(r.get("YSZKZZTS")),
            "应收周转率": _num(r.get("YSZKZZL")),
        })
    goodwill = None
    if data:
        latest = (data[0].get("REPORT_DATE") or "")[:10]
        if latest:
            try:
                bd = json.loads(_http_get(
                    "https://emweb.securities.eastmoney.com/PC_HSF10/"
                    "NewFinanceAnalysis/zcfzbAjaxNew?companyType=4&reportDateType=0"
                    "&reportType=1&dates=%s&code=%s" % (latest, sec),
                    timeout=15).decode("utf-8-sig"))
                brec = (bd.get("data") or [{}])[0]
                goodwill = _num(brec.get("GOODWILL"))
            except Exception:
                pass
    return {"rows": rows, "商誉": goodwill}


def fetch_moneyflow(code: str) -> list:
    """新浪主力资金流（日级，近 5 日）。"""
    sym = _symbol(code)
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=5&sort=opendate&asc=0&daima=%s"
           % sym)
    try:
        txt = _http_get(url, timeout=15, referer="https://finance.sina.com.cn/").decode("utf-8", "replace")
        arr = json.loads(txt)
    except Exception as e:
        return [{"error": "资金流接口失败: %s" % type(e).__name__}]
    out = []
    for r in arr[:5]:
        out.append({
            "日期": r.get("opendate"),
            "收盘": _num(r.get("trade")),
            "涨跌幅": _num(r.get("changeratio")),
            "主力净流入元": _num(r.get("netamount"), 0),
            "净流入占比": _num(r.get("ratioamount")),
            "超大单净额": _num(r.get("r0_net"), 0),
            "超大单占比": _num(r.get("r0_ratio")),
        })
    return out


def fetch_lhb(code: str) -> list:
    """东财龙虎榜（近 10 条）。"""
    c = str(code).zfill(6)
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL"
           "&filter=(SECURITY_CODE%%3D%%22%s%%22)"
           "&pageNumber=1&pageSize=10&sortTypes=-1&sortColumns=TRADE_DATE" % c)
    try:
        d = json.loads(_http_get(url, timeout=15, referer="https://data.eastmoney.com/").decode("utf-8", "replace"))
    except Exception as e:
        return [{"error": "龙虎榜接口失败: %s" % type(e).__name__}]
    data = ((d.get("result") or {}).get("data")) or []
    out = []
    for r in data[:10]:
        out.append({
            "上榜日期": (r.get("TRADE_DATE") or "")[:10],
            "上榜原因": (r.get("EXPLANATION") or "")[:50],
            "收盘价": _num(r.get("CLOSE_PRICE")),
            "涨跌幅": _num(r.get("CHANGE_RATE")),
            "净买入额元": _num(r.get("BILLBOARD_NET_AMT")),
            "买入额元": _num(r.get("BILLBOARD_BUY_AMT")),
            "卖出额元": _num(r.get("BILLBOARD_SELL_AMT")),
            "成交额元": _num(r.get("BILLBOARD_DEAL_AMT")),
            "换手率": _num(r.get("TURNOVERRATE")),
        })
    return out


def fetch_margin(code: str) -> list:
    """东财融资融券（近 3 条）。"""
    c = str(code).zfill(6)
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPTA_WEB_RZRQ_GGMX&columns=ALL"
           "&filter=(SCODE%%3D%%22%s%%22)"
           "&pageNumber=1&pageSize=3&sortTypes=-1&sortColumns=DATE" % c)
    try:
        d = json.loads(_http_get(url, timeout=15, referer="https://data.eastmoney.com/").decode("utf-8", "replace"))
    except Exception as e:
        return [{"error": "融资融券接口失败: %s" % type(e).__name__}]
    data = ((d.get("result") or {}).get("data")) or []
    out = []
    for r in data[:3]:
        out.append({
            "日期": (r.get("DATE") or "")[:10],
            "融资余额元": _num(r.get("RZYE"), 0),
            "融券余额元": _num(r.get("RQYE"), 0),
            "融资融券余额元": _num(r.get("RZRQYE"), 0),
            "融资净买入元": _num(r.get("RZJME"), 0),
            "融资买入额元": _num(r.get("RZMRE"), 0),
            "融资余额占比": _num(r.get("RZYEZB")),
            "收盘价": _num(r.get("SPJ")),
        })
    return out


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------

def _fmt_yi(x):
    """元 → 亿元 字符串（约简）。"""
    if x is None:
        return ""
    try:
        return "%.2f亿" % (float(x) / 1e8)
    except (ValueError, TypeError):
        return str(x)


def _fmt_wan(x):
    if x is None:
        return ""
    try:
        return "%.2f万" % float(x)
    except (ValueError, TypeError):
        return str(x)


def _kline_recent_text(bars, n=6):
    if not bars:
        return "无"
    lines = []
    for b in bars[-n:]:
        lines.append("%s 开%s 收%s 高%s 低%s 量%s" % (
            (b.get("date") or "")[:10], _numstr(b.get("open")), _numstr(b.get("close")),
            _numstr(b.get("high")), _numstr(b.get("low")), _numstr(b.get("volume"), 0)))
    return "; ".join(lines)


def get_stock_data(code: str) -> dict:
    """聚合单只个股全维度数据，返回 hithink 风格单条 datas 记录。"""
    c = str(code).zfill(6)
    q = fetch_quote(c)
    kd = fetch_kline(c, "day", 320)
    km = fetch_kline(c, "m60", 40)
    fin = fetch_finance(c)
    mf = fetch_moneyflow(c)
    lhb = fetch_lhb(c)
    mg = fetch_margin(c)

    def qv(k):
        return q.get(k) if isinstance(q, dict) else None

    # 行情
    quote_txt = ("现价%s 涨跌幅%s%% 换手%s%% PE%s PB%s 流通市值%s亿 总市值%s亿 "
                 "量比%s 涨停%s 跌停%s 最高%s 最低%s 外盘%s 内盘%s" % (
                     _numstr(qv("price")),
                     _numstr(qv("change_pct")), _numstr(qv("turnover_pct")),
                     _numstr(qv("pe")), _numstr(qv("pb")),
                     _numstr(qv("float_mv_yi")), _numstr(qv("total_mv_yi")),
                     _numstr(qv("vol_ratio")), _numstr(qv("limit_up")),
                     _numstr(qv("limit_down")), _numstr(qv("high")), _numstr(qv("low")),
                     _numstr(qv("outer_hand"), 0), _numstr(qv("inner_hand"), 0)))

    # 财务
    fin_txt = "无"
    goodwill_txt = ""
    if isinstance(fin, dict) and fin.get("rows"):
        fl = []
        for r in fin["rows"][:3]:
            fl.append("%s(%s) 营收%s 归母净利%s 扣非%s ROE%s%% 毛利率%s%% 净利率%s%% "
                      "负债率%s%% 每股经营现金流%s EPS%s BPS%s" % (
                          r["报告期"], r["报告类型"], _numstr(r["营业总收入"]),
                          _numstr(r["归母净利润"]), _numstr(r["扣非净利润"]),
                          _numstr(r["加权ROE"]), _numstr(r["毛利率"]),
                          _numstr(r["净利率"]), _numstr(r["资产负债率"]),
                          _numstr(r["每股经营现金流"]), _numstr(r["每股收益"]),
                          _numstr(r["每股净资产"])))
        fin_txt = "; ".join(fl)
        if fin.get("商誉") is not None:
            goodwill_txt = "商誉 %s 元" % _numstr(fin["商誉"])

    # 资金流
    mf_txt = "无"
    if isinstance(mf, list) and mf and "error" not in mf[0]:
        ml = ["%s 主力净流入%s 超大单%s" % (
            r["日期"], _fmt_yi(r["主力净流入元"]), _fmt_yi(r["超大单净额"])) for r in mf[:3]]
        mf_txt = "; ".join(ml)

    # 龙虎榜
    lhb_txt = "近期无上榜记录"
    if isinstance(lhb, list) and lhb and "error" not in lhb[0]:
        ll = ["%s %s 净买入%s(买%s/卖%s)" % (
            r["上榜日期"], r["上榜原因"], _fmt_yi(r["净买入额元"]),
            _fmt_yi(r["买入额元"]), _fmt_yi(r["卖出额元"])) for r in lhb[:3]]
        lhb_txt = "; ".join(ll)

    # 融资融券
    mg_txt = "无"
    if isinstance(mg, list) and mg and "error" not in mg[0]:
        r = mg[0]
        mg_txt = ("%s 融资余额%s 融资净买入%s 融券余额%s 融资余额占比%s%%" % (
            r["日期"], _fmt_yi(r["融资余额元"]), _fmt_yi(r["融资净买入元"]),
            _fmt_yi(r["融券余额元"]), _numstr(r["融资余额占比"])))

    return {
        "标的": "%s(%s.%s)" % (qv("name") or "", c, _prefix(c).upper()),
        "实时行情": quote_txt,
        "日K近6日": _kline_recent_text(kd, 6) + ("（共%d根）" % len(kd) if isinstance(kd, list) else ""),
        "60分钟K近5": _kline_recent_text(km, 5),
        "财务指标近3期": fin_txt,
        "商誉": goodwill_txt or "未披露/无",
        "主力资金流近3日": mf_txt,
        "龙虎榜": lhb_txt,
        "融资融券": mg_txt,
        "数据日期": (qv("time") or "")[:8] or "实时",
    }


# ---------------------------------------------------------------------------
# 缓存（serve 模式）
# ---------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()


def _cached(key, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


# ---------------------------------------------------------------------------
# HTTP（serve 模式）
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _q(self):
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(self.path).query)

    def do_GET(self):
        path = self.path.rstrip("/").split("?")[0]
        if path in ("/health", "/healthz", "/"):
            return self._send({"status": "ok", "service": "stock-data"})
        q = self._q()
        code = (q.get("code") or [""])[0]
        if path == "/quote":
            return self._send({"datas": [fetch_quote(code)] if code else []})
        if path == "/kline":
            period = (q.get("period") or ["day"])[0]
            try:
                limit = int((q.get("limit") or ["320"])[0])
            except ValueError:
                limit = 320
            return self._send({"datas": [{"kline": fetch_kline(code, period, limit)}] if code else []})
        if path == "/finance":
            return self._send({"datas": [fetch_finance(code)] if code else []})
        if path == "/moneyflow":
            return self._send({"datas": [{"flow": fetch_moneyflow(code)}] if code else []})
        if path == "/lhb":
            return self._send({"datas": [{"lhb": fetch_lhb(code)}] if code else []})
        if path == "/margin":
            return self._send({"datas": [{"margin": fetch_margin(code)}] if code else []})
        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.rstrip("/") != "/skills/v1/query2data":
            return self._send({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b"{}"
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}
        query = body.get("query") or body.get("text") or ""
        code = _norm_code(query)
        if not code:
            return self._send({"datas": [], "_error": "未识别证券代码"})
        try:
            data = _cached("all:" + code, CACHE_TTL, lambda: get_stock_data(code))
            return self._send({"datas": [data], "_source": "stock-data-service"})
        except Exception as e:
            return self._send({"datas": [], "_error": "%s: %s" % (type(e).__name__, str(e)[:160])})

    def log_message(self, fmt, *args):
        sys.stderr.write("[stock-data] %s\n" % (fmt % args))


def serve():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    sys.stderr.write("stock-data service on %s:%d\n" % (HOST, PORT))
    srv.serve_forever()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_text(code, kind):
    if kind == "quote":
        q = fetch_quote(code)
        return json.dumps(q, ensure_ascii=False, indent=1)
    if kind == "kline":
        return json.dumps(fetch_kline(code, "day", 320), ensure_ascii=False, indent=1)
    if kind == "finance":
        return json.dumps(fetch_finance(code), ensure_ascii=False, indent=1)
    if kind == "moneyflow":
        return json.dumps(fetch_moneyflow(code), ensure_ascii=False, indent=1)
    if kind == "lhb":
        return json.dumps(fetch_lhb(code), ensure_ascii=False, indent=1)
    if kind == "margin":
        return json.dumps(fetch_margin(code), ensure_ascii=False, indent=1)
    # all
    d = get_stock_data(code)
    lines = []
    for k in ("标的", "实时行情", "日K近6日", "60分钟K近5", "财务指标近3期",
              "商誉", "主力资金流近3日", "龙虎榜", "融资融券", "数据日期"):
        if k in d:
            lines.append("%s：%s" % (k, d[k]))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="股票数据接口服务")
    ap.add_argument("--serve", action="store_true", help="常驻 HTTP 服务")
    ap.add_argument("--code", help="6 位证券代码")
    ap.add_argument("--kind", default="all",
                    choices=["all", "quote", "kline", "finance", "moneyflow", "lhb", "margin"])
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if args.serve:
        serve()
        return 0

    code = _norm_code(args.code or "")
    if not code:
        ap.print_help()
        return 1

    if args.kind == "all" and not args.json:
        print(_cli_text(code, "all"))
    elif args.kind == "all" and args.json:
        print(json.dumps(get_stock_data(code), ensure_ascii=False, indent=1))
    else:
        print(_cli_text(code, args.kind))
    return 0


if __name__ == "__main__":
    sys.exit(main())

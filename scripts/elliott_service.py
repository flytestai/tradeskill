#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""elliott-index-wave 波浪技能 HTTP 服务（hithink 风格 query2data 契约，多周期版）。

把本地 elliott-index-wave 波浪分析封装成常驻 HTTP 服务，对外暴露与蜜蜂网关
同构的 /skills/v1/query2data 端点，返回 {"datas":[...]} 结构化结果，
供 skill_agent 当普通远程技能统一编排（skill_id 进 CATALOG、端点/超时可配置）。

数据源链：腾讯 proxy.finance.qq.com 多周期K（年线/月线/周线/日线）→ 新浪日K。

配置（环境变量）：
    ELLIOTT_SERVICE_HOST  默认 127.0.0.1
    ELLIOTT_SERVICE_PORT  默认 8022
    ELLIOTT_CACHE_TTL     内存缓存秒数，默认 600（10min），0=禁用

由 systemd 单元 kol-elliott.service 常驻托管。
"""

import importlib.util
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOST = os.environ.get("ELLIOTT_SERVICE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ELLIOTT_SERVICE_PORT", "8022"))
CACHE_TTL = int(os.environ.get("ELLIOTT_CACHE_TTL", "600"))


# ---------------------------------------------------------------------------
# 懒加载 generate_report（放 try 里，避免启动时因路径问题直接崩）
# ---------------------------------------------------------------------------
_gen = None
_gen_err = None
_gen_lock = threading.Lock()


def _load_gen():
    global _gen, _gen_err
    if _gen is not None or _gen_err is not None:
        return _gen
    with _gen_lock:
        if _gen is not None or _gen_err is not None:
            return _gen
        candidates = [
            os.path.join(SKILL_DIR, "skills", "elliott-index-wave",
                         "scripts", "generate_report.py"),
            os.path.join(SKILL_DIR, "vendor", "elliott-index-wave",
                         "scripts", "generate_report.py"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                try:
                    spec = importlib.util.spec_from_file_location(
                        "elliott_generate_report", path)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    _gen = mod
                    return _gen
                except Exception as e:  # noqa: BLE001
                    _gen_err = "%s: %s" % (type(e).__name__, str(e)[:160])
                    return None
        _gen_err = "generate_report.py 未找到（skills/ 或 vendor/ 下均无）"
        return None


def _resolve_index(query):
    gen = _load_gen()
    if gen is None:
        return ""
    q = (query or "").strip()
    for name in sorted(gen.INDEX_CODES, key=len, reverse=True):
        if name in q:
            return name
    for alias in sorted(gen.INDEX_ALIASES, key=len, reverse=True):
        if alias in q:
            return gen.INDEX_ALIASES[alias]
    return ""


def _fmt(x):
    if isinstance(x, float):
        return "%.2f" % x
    return "" if x is None else str(x)


def _tf_line(r):
    """单周期 A-B-C 摘要一行（字段名已含周期标签，故不重复前缀）。"""
    if not r:
        return "数据不足"
    if r.get("state") == "冲顶":
        return "疑似第5浪冲顶（高点%s@%s）" % (_fmt(r["top"]), r["top_date"])
    if r.get("state") == "A进行中":
        return "A浪进行中（顶%s→A低%s）" % (_fmt(r["top"]), _fmt(r["A"]))
    return "%s顶后调整 A%s→%s B%s C低%s" % (
        "3浪" if r["wave3_like"] else "5浪",
        _fmt(r["top"]), _fmt(r["A"]), _fmt(r["B"]), _fmt(r["C"]))


def _to_datas(index, symbol, multi):
    """把多周期分析结果转成 hithink 风格 datas（单条，字段值均 ≤60 字符）。"""
    day = multi.get("day")
    year = multi.get("year")

    def year_line():
        if not year:
            return ""
        return "%s（高点%s@%s年，近3年%s）" % (
            year["state"], _fmt(year["hist_high"]), year["hist_high_year"],
            year["trend3"])

    if day is None:
        return []
    if day.get("state") == "冲顶":
        return [{
            "指数": index, "指数代码": symbol,
            "年线方向": year_line(),
            "浪级定位": "仍在上行，疑似第5浪冲顶段（未见A-B-C调整结构）",
            "参考高点": _fmt(day.get("top")), "参考高点日期": day.get("top_date", ""),
            "最新收盘": _fmt(day.get("last_close")), "数据日期": day.get("last_date", ""),
        }]
    if day.get("state") == "A进行中":
        return [{
            "指数": index, "指数代码": symbol,
            "年线方向": year_line(),
            "浪级定位": "见顶回落，第4浪A浪进行中（B浪未展开）",
            "顶部": _fmt(day.get("top")), "顶部日期": day.get("top_date", ""),
            "A浪低点": _fmt(day.get("A")), "A浪低点日期": day.get("A_date", ""),
            "最新收盘": _fmt(day.get("last_close")), "数据日期": day.get("last_date", ""),
        }]

    c_sub = day.get("c_sub") or {}
    labels = []
    for k, lbl in (("c1", "C1"), ("c2", "C2"), ("c3", "C3"),
                   ("c4", "C4"), ("c5", "C5")):
        if c_sub.get(k):
            labels.append("%s %s" % (lbl, _fmt(c_sub[k]["price"])))
    c_inner = " ".join(labels)[:58] or "C1-C2-C3 未细分"

    fib = day.get("fib") or {}
    reb = _fmt(c_sub["c4"]["price"]) if c_sub.get("c4") else _fmt(day.get("C"))

    return [{
        "指数": index, "指数代码": symbol,
        "年线方向": year_line(),
        "月线浪级": _tf_line(multi.get("month")),
        "周线浪级": _tf_line(multi.get("week")),
        "主浪判定": ("3浪顶/4浪调整" if day.get("wave3_like") else "5浪顶/4浪调整"),
        "精确位置": "第4浪的C浪（%s）" % ("C5进行中" if c_sub.get("c5") else "进行中"),
        "A浪低点": _fmt(day.get("A")),
        "B浪高点": _fmt(day.get("B")),
        "C浪低点": _fmt(day.get("C")),
        "C浪内部": c_inner,
        "反弹参考位": "C4反弹高点%s" % reb,
        "下杀目标": "%.2f→%.2f→%.2f" % (
            fib.get("0.618") or 0, fib.get("1.000") or 0, fib.get("1.618") or 0),
        "失效位": "上%s 破%s续C 站回%s转5浪" % (
            _fmt(day.get("top")), _fmt(day.get("A")), _fmt(day.get("B"))),
        "数据日期": day.get("last_date", ""),
    }]


# ---- 内存 TTL 缓存 ----
_cache = {}
_cache_lock = threading.Lock()


def _analyze_index(index):
    gen = _load_gen()
    if gen is None:
        return None
    symbol = gen.INDEX_CODES.get(index)
    if not symbol:
        return None
    multi = gen._multi_analyze(symbol, 2000)
    if not multi or not multi.get("day"):
        return None
    return _to_datas(index, symbol, multi)


def _get_datas(query):
    index = _resolve_index(query)
    if not index:
        if _gen_err:
            return {"datas": [], "_error": _gen_err}
        return {"datas": [], "_error": "未能从问句识别指数"}
    now = time.time()
    with _cache_lock:
        hit = _cache.get(index)
        if hit and CACHE_TTL > 0 and now - hit[0] < CACHE_TTL:
            return {"datas": hit[1], "_source": "elliott-index-wave(cache)"}
    try:
        datas = _analyze_index(index)
    except Exception as e:  # noqa: BLE001
        return {"datas": [], "_error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    if not datas:
        return {"datas": [], "_error": "波浪分析无数据"}
    if CACHE_TTL > 0:
        with _cache_lock:
            _cache[index] = (now, datas)
    return {"datas": datas, "_source": "elliott-index-wave"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/healthz", "/"):
            return self._send({"status": "ok", "service": "elliott-index-wave",
                               "indexes": []})
        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.rstrip("/") != "/skills/v1/query2data":
            return self._send({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b"{}"
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:  # noqa: BLE001
            body = {}
        query = body.get("query") or body.get("text") or ""
        try:
            resp = _get_datas(query)
        except Exception as e:  # noqa: BLE001
            resp = {"datas": [], "_error": "%s: %s" % (type(e).__name__, str(e)[:160])}
        self._send(resp)

    def log_message(self, fmt, *args):
        sys.stderr.write("[elliott-service] %s\n" % (fmt % args))


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    sys.stderr.write("elliott-index-wave service on %s:%d (skill_dir=%s)\n"
                     % (HOST, PORT, SKILL_DIR))
    srv.serve_forever()


if __name__ == "__main__":
    main()

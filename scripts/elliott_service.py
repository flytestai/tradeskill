#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""elliott-index-wave 波浪技能 HTTP 服务（hithink 风格 query2data 契约）。

把本地 elliott-index-wave 波浪分析封装成常驻 HTTP 服务，对外暴露与蜜蜂网关
同构的 /skills/v1/query2data 端点，返回 {"datas":[...]} 结构化结果，
供 skill_agent 当普通远程技能统一编排（skill_id 进 CATALOG、端点/超时可配置）。

数据源链：腾讯 proxy.finance.qq.com 日K → 新浪日K（见 generate_report.py）。

配置（环境变量）：
    ELLIOTT_SERVICE_HOST  默认 127.0.0.1
    ELLIOTT_SERVICE_PORT  默认 8022
    ELLIOTT_CACHE_TTL     内存缓存秒数，默认 21600（6h），0=禁用

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
CACHE_TTL = int(os.environ.get("ELLIOTT_CACHE_TTL", "21600"))


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


def _to_datas(index, symbol, r):
    """把 generate_report._analyze 的结果转成 hithink 风格 datas（单条）。"""
    if r.get("state") == "冲顶":
        return [{
            "指数": index, "指数代码": symbol,
            "浪级定位": "仍在上行，疑似第5浪冲顶段（未见A-B-C调整结构）",
            "参考高点": _fmt(r.get("top")), "参考高点日期": r.get("top_date", ""),
            "最新收盘": _fmt(r.get("last_close")), "数据日期": r.get("last_date", ""),
        }]
    if r.get("state") == "A进行中":
        return [{
            "指数": index, "指数代码": symbol,
            "浪级定位": "见顶回落，第4浪A浪进行中（B浪未展开）",
            "顶部": _fmt(r.get("top")), "顶部日期": r.get("top_date", ""),
            "A浪低点": _fmt(r.get("A")), "A浪低点日期": r.get("A_date", ""),
            "最新收盘": _fmt(r.get("last_close")), "数据日期": r.get("last_date", ""),
        }]

    c_sub = r.get("c_sub") or {}
    c_inner = ""
    if c_sub.get("c1"):
        parts = ["C1 %s→%s" % (_fmt(r.get("B")), _fmt(c_sub["c1"].get("price")))]
        if c_sub.get("c2"):
            parts.append("C2 %s→%s" % (_fmt(c_sub["c1"].get("price")),
                                       _fmt(c_sub["c2"].get("price"))))
        if c_sub.get("c3"):
            tag = "进行中" if c_sub.get("c3_ongoing") else "已现低点"
            parts.append("C3 %s→%s(%s)" % (_fmt(c_sub["c2"].get("price")),
                                           _fmt(c_sub["c3"].get("price")), tag))
        c_inner = " | ".join(parts)

    fib = r.get("fib") or {}
    return [{
        "指数": index, "指数代码": symbol,
        "主浪判定": ("3浪顶/4浪调整" if r.get("wave3_like") else "5浪顶/4浪调整"),
        "精确位置": "第4浪的C浪（进行中）",
        "C浪内部": c_inner or "C1-C2-C3 未细分",
        "A浪低点": _fmt(r.get("A")),
        "B浪高点": _fmt(r.get("B")),
        "C浪低点": _fmt(r.get("C")),
        "斐波那契目标": "0.618=%s / 等长=%s / 1.618=%s" % (
            _fmt(fib.get("0.618")), _fmt(fib.get("1.000")), _fmt(fib.get("1.618"))),
        "失效位向上": _fmt(r.get("top")),
        "C浪确认向下": _fmt(r.get("A")),
        "C浪否定向上": _fmt(r.get("B")),
        "数据日期": r.get("last_date", ""),
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
    bars, _source = gen._fetch_daily(symbol, 260)
    if not bars:
        return None
    r = gen._analyze(bars)
    if not r:
        return None
    return _to_datas(index, symbol, r)


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

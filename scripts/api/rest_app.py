#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flask REST 接口层。

启动
----
    python -m api.rest_app              # 默认 127.0.0.1:8000
    PLATFORM_PORT=8080 python -m api.rest_app

接口
----
    GET  /healthz                    健康检查（含蜜蜂通道探测）
    GET  /api/v1/capabilities        能力清单
    GET  /api/v1/kol/list            列出大V
    GET  /api/v1/kol/records         查询言论
    GET  /api/v1/kol/summary         数据概览
    GET  /api/v1/kol/accuracy        准确率报告
    GET  /api/v1/kol/predictions     预测列表
    POST /api/v1/kol/predictions     新增预测
    GET  /api/v1/kol/compare         多KOL对比
    GET  /api/v1/kol/backtest        跟单回测
    GET  /api/v1/levels              关键点位
    GET  /api/v1/market/summary      行情汇总
    GET  /api/v1/market/quote        行情查询
    GET  /api/v1/system/alerts       提醒状态
    GET  /api/v1/llm/status          LLM(Kimi) 配置状态
    POST /api/v1/llm/ask             调用 Kimi 回答问题
    POST /api/v1/llm/summarize       对内容做归纳解读
    GET  /api/v1/system/qa-queue     群问答队列状态

鉴权：X-API-Key 或 Authorization: Bearer <key>（见 platform/auth.py）
"""
from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request  # noqa: E402

import config          # noqa: E402
import services        # noqa: E402
from auth import AuthError, require_auth  # noqa: E402

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False


# --------------------------------------------------------------------------
# 统一响应
# --------------------------------------------------------------------------

def ok(data, **extra):
    payload = {"ok": True, "data": data}
    payload.update(extra)
    return jsonify(payload)


def fail(message: str, status: int = 400, type_: str = "error"):
    return jsonify({"ok": False, "error": {"type": type_, "message": message}}), status


@app.errorhandler(AuthError)
def _auth_err(e):
    return fail(str(e), 401, "auth")


@app.errorhandler(services.ServiceError)
def _svc_err(e):
    return fail(str(e), 502, "service")


@app.errorhandler(Exception)
def _any_err(e):
    """全局兜底。

    ⚠️ 必须先把 HTTPException 放行（实测踩坑）：
      Flask 的 `errorhandler(Exception)` 会捕获**所有**异常，包括
      werkzeug 的 NotFound / MethodNotAllowed 等 HTTPException。
      原实现把它们一律当成 500，导致：
        · 访问不存在的路径（如扫描器探 /.env）返回 **500 而非 404**
          —— 外部监控会把正常的路由未命中误判为服务故障
        · 每个 404 都打印完整 traceback，日志被扫描流量刷满，
          真实故障的堆栈反而被淹没
      故：HTTPException 按其自身状态码原样返回，只有真正的未预期异常才记 500。
    """
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        code = e.code or 500
        return fail(str(e.description or e.name), code, "http")
    traceback.print_exc()
    return fail("内部错误: %s" % str(e)[:300], 500, "internal")


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------

def _cached_bee_health(ttl: int = 120):
    """带缓存的蜜蜂通道健康检查。

    为什么要缓存：bee_health() 会实测 http/local 两个外部通道，耗时约 2 秒。
    在单线程模式（受限容器）下，若每次 /healthz 都实测，会阻塞后续请求
    —— 实测出现「连续两次调用其中一次超时 25 秒」的排队现象。
    缓存 TTL 默认 120 秒，与 Docker healthcheck 的 60s 间隔相配。
    """
    import time
    now = time.time()
    hit = _HEALTH_CACHE.get("bee")
    if hit and now - hit[0] < ttl:
        return hit[1], True
    try:
        bee = services.bee_health()
    except Exception as e:
        bee = {"ok": False, "error": str(e)[:200]}
    _HEALTH_CACHE["bee"] = (now, bee)
    return bee, False


_HEALTH_CACHE = {}


@app.get("/healthz")
def healthz():
    """健康检查（供 Docker healthcheck 与监控调用，无需鉴权）。

    轻量版：只报告本服务与配置，**不实测外部通道**，响应在毫秒级。
    需要实测外部通道时用 /healthz?deep=1（结果缓存 120 秒）。
    """
    deep = request.args.get("deep", "").lower() in ("1", "true", "yes")
    body = {
        "ok": True,
        "service": "skills-platform",
        "config": config.summary(),
    }
    if deep:
        bee, cached = _cached_bee_health()
        body["bee"] = bee
        body["bee_cached"] = cached
        body["ok"] = bool(bee.get("ok"))
        return jsonify(body), 200 if body["ok"] else 207
    return jsonify(body), 200


@app.get("/api/v1/capabilities")
@require_auth()
def capabilities(ctx):
    return ok(services.capabilities())


# --------------------------------------------------------------------------
# KOL
# --------------------------------------------------------------------------

@app.get("/api/v1/kol/list")
@require_auth("kol:read")
def kol_list(ctx):
    return ok(services.list_kols())


@app.get("/api/v1/kol/records")
@require_auth("kol:read")
def kol_records(ctx):
    a = request.args
    try:
        days = int(a.get("days", 30))
        latest = int(a.get("latest", 0))
    except ValueError:
        return fail("days/latest 必须是整数")
    data = services.query_records(
        kol_name=a.get("kol_name", ""),
        days=days,
        latest=latest,
        vip_only=a.get("vip_only", "").lower() in ("1", "true", "yes"),
        all_time=a.get("all", "").lower() in ("1", "true", "yes"),
    )
    return ok(data, count=len(data))


@app.get("/api/v1/kol/summary")
@require_auth("kol:read")
def kol_summary(ctx):
    name = request.args.get("kol_name", "")
    if not name:
        return fail("缺少 kol_name")
    return ok(services.summary(name))


@app.get("/api/v1/kol/accuracy")
@require_auth("kol:read")
def kol_accuracy(ctx):
    name = request.args.get("kol_name", "")
    if not name:
        return fail("缺少 kol_name")
    return ok(services.accuracy_report(name))


@app.get("/api/v1/kol/predictions")
@require_auth("kol:read")
def kol_predictions(ctx):
    return ok(services.list_predictions(request.args.get("kol", "")))


@app.post("/api/v1/kol/predictions")
@require_auth("kol:write")
def kol_predictions_add(ctx):
    b = request.get_json(silent=True) or {}
    for f in ("kol", "pred"):
        if not b.get(f):
            return fail("缺少字段: %s" % f)
    return ok(services.add_prediction(
        kol=b["kol"], pred=b["pred"], ptype=b.get("type", "点位"),
        target=str(b.get("target", "")), direction=b.get("dir", ""),
        date=b.get("date", "")))


@app.get("/api/v1/kol/compare")
@require_auth("kol:read")
def kol_compare(ctx):
    kols = request.args.getlist("kol") or None
    return ok(services.compare_kols(kols))


@app.get("/api/v1/kol/backtest")
@require_auth("kol:read")
def kol_backtest(ctx):
    return ok(services.backtest(request.args.get("strategy", "half")))


@app.get("/api/v1/levels")
@require_auth("kol:read")
def levels(ctx):
    idx = request.args.get("index", "")
    price = request.args.get("price")
    try:
        p = float(price) if price not in (None, "") else None
    except ValueError:
        return fail("price 必须是数字")
    return ok(services.levels(idx, p))


# --------------------------------------------------------------------------
# 行情
# --------------------------------------------------------------------------

@app.get("/api/v1/market/summary")
@require_auth("market:read")
def market_summary(ctx):
    return ok(services.market_summary(request.args.get("period", "premarket")))


@app.get("/api/v1/market/quote")
@require_auth("market:read")
def market_quote(ctx):
    q = request.args.get("query", "")
    if not q:
        return fail("缺少 query")
    return ok(services.quote(q, channel=request.args.get("channel", "")))


# --------------------------------------------------------------------------
# 系统
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# LLM（Kimi）
# --------------------------------------------------------------------------

@app.get("/api/v1/llm/status")
@require_auth("system:read")
def llm_status(ctx):
    return ok(services.llm_status())


@app.post("/api/v1/llm/ask")
@require_auth("llm:use")
def llm_ask(ctx):
    b = request.get_json(silent=True) or {}
    q = b.get("question") or request.args.get("question", "")
    if not q:
        return fail("缺少 question")
    return ok(services.llm_ask(q, b.get("context", "")))


@app.post("/api/v1/llm/summarize")
@require_auth("llm:use")
def llm_summarize(ctx):
    b = request.get_json(silent=True) or {}
    c = b.get("content") or ""
    if not c:
        return fail("缺少 content")
    return ok(services.llm_summarize(c, b.get("instruction", "")))


@app.get("/api/v1/system/qa-queue")
@require_auth("kol:read")
def qa_queue(ctx):
    return ok(services.qa_queue_status())


@app.get("/api/v1/system/alerts")
@require_auth("kol:read")
def system_alerts(ctx):
    return ok(services.alert_status())


def supports_threads() -> bool:
    """探测当前环境能否创建线程。

    背景：部分受限容器（老版 Docker + 特定内核/cgroup 组合）会拒绝创建线程，
    报 `RuntimeError: can't start new thread`。此时若 Flask 用默认的
    threaded=True，每个请求都会失败（空响应 / 502）。

    策略：优先读环境变量 PLATFORM_THREADED；未设置时实测一次。
    """
    env = os.environ.get("PLATFORM_THREADED", "").strip().lower()
    if env in ("0", "false", "no"):
        return False
    if env in ("1", "true", "yes"):
        return True
    try:
        import threading
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()
        return True
    except Exception as e:
        print("[platform] ⚠️ 环境不支持多线程（%s），降级为单线程模式" % e)
        return False


def main():
    threaded = supports_threads()
    print("[platform] 启动 REST 服务 http://%s:%s" % (config.HOST, config.PORT))
    print("[platform] 鉴权: %s" % ("已启用（%d 个 Key）" % len(config.API_KEYS)
                                   if config.API_KEYS else "未配置（仅限本机）"))
    print("[platform] 并发模式: %s" % ("多线程" if threaded else "单线程（串行处理）"))
    # threaded=False 时 Flask 使用内置单线程 WSGI，完全不创建线程。
    # 请求串行处理 —— 对本场景（低频数据查询）足够，且能在受限容器中存活。
    app.run(host=config.HOST, port=config.PORT, debug=False, threaded=threaded)


if __name__ == "__main__":
    main()

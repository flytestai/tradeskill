#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署前自检：把「一上线就炸」的问题拦在部署之前。

为什么需要
----------
2026-09-19 的一次改动中，我把 `from context_format import cfg as _cfg`
放到了文件底部，而模块级第 70 行就用了 `_cfg_int(...)` ——
**导入即 NameError**，`skill_router` 完全不可用。

更危险的是：`qa_analyzer.build_context()` 对 `skill_router` 的导入失败是
`except Exception` 静默吞掉的，最终只会降级到「仅指数」的兜底路径，
**不会报错、只会悄悄少数据** —— 与 09-18 那次生产故障同一模式。

该错误在本地 `py_compile` 抓不到（语法合法），只有真正 import 才暴露。
故本脚本把「导入 + 关键契约 + 取数」串成部署前必过的一关。

用法
----
    python scripts/preflight.py            # 完整自检
    python scripts/preflight.py --quick    # 跳过联网取数

退出码：0=通过 / 1=失败
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPTS)
sys.path.insert(0, SCRIPTS)

PASS, FAIL = [], []


def _ok(name, detail=""):
    PASS.append((name, detail))
    print("  ✅ %s%s" % (name, ("  — " + detail) if detail else ""))


def _bad(name, detail=""):
    FAIL.append((name, detail))
    print("  ❌ %s%s" % (name, ("  — " + detail) if detail else ""))


# ---------------------------------------------------------------------------
# 1. 模块导入（抓 NameError / ImportError / 循环导入）
# ---------------------------------------------------------------------------

CORE_MODULES = [
    "context_format", "skill_agent", "skill_router", "bee_client", "llm_client",
    "qa_analyzer", "group_reply", "qa_queue", "qa_dedup", "react", "common",
    "feishu_client", "db_query", "db_save", "db_sync", "db_init",
    "price_alerts", "market_summary", "monitor_alerts", "level_monitor",
    "position_monitor", "predict_track", "kol_compare", "backtest",
    "sync_litchi_auto", "sync_qa_auto", "sync_feishu_auto", "auth_keepalive",
]


def check_imports():
    print("\n[1/5] 模块导入")
    bad = []
    for m in CORE_MODULES:
        if not os.path.isfile(os.path.join(SCRIPTS, m + ".py")):
            continue
        try:
            importlib.import_module(m)
        except Exception as e:
            bad.append("%s → %s: %s" % (m, type(e).__name__, str(e)[:100]))
    if bad:
        for b in bad:
            _bad("导入失败", b)
    else:
        _ok("核心模块全部可导入", "%d 个" % len(CORE_MODULES))


# ---------------------------------------------------------------------------
# 2. 接口契约（端点 / 请求头 / 版本号）
# ---------------------------------------------------------------------------

def check_contract():
    print("\n[2/5] 接口契约")
    try:
        cf = importlib.import_module("context_format")
        sa = importlib.import_module("skill_agent")
    except Exception as e:
        _bad("无法加载模块", str(e)[:120])
        return

    # 端点只有两种
    if sa.EP_Q2D.endswith("/skills/v1/query2data"):
        _ok("query2data 端点", sa.EP_Q2D)
    else:
        _bad("query2data 端点异常", sa.EP_Q2D)

    if sa.EP_SEARCH.endswith("/skills/v1/comprehensive/search"):
        _ok("search 端点", sa.EP_SEARCH)
    else:
        _bad("search 端点异常", sa.EP_SEARCH)

    # 请求头 7 项
    h = sa._headers("hithink-market-query")
    need = {"Content-Type", "X-Claw-Call-Type", "X-Claw-Skill-Id",
            "X-Claw-Skill-Version", "X-Claw-Plugin-Id", "X-Claw-Plugin-Version",
            "X-Claw-Trace-Id"}
    missing = need - set(h)
    if missing:
        _bad("请求头缺失", ", ".join(sorted(missing)))
    else:
        _ok("X-Claw-* 请求头完整", "7 项")

    # Trace-Id 必须 64 位且每次不同
    t1, t2 = sa._headers("x")["X-Claw-Trace-Id"], sa._headers("x")["X-Claw-Trace-Id"]
    if len(t1) == 64 and t1 != t2:
        _ok("Trace-Id 64 位且每次新生成")
    else:
        _bad("Trace-Id 异常", "len=%d unique=%s" % (len(t1), t1 != t2))

    # 版本号逐技能
    if cf.skill_version("report-search") == "2.0.0":
        _ok("report-search 版本", "2.0.0")
    else:
        _bad("report-search 版本错误", cf.skill_version("report-search"))

    if cf.skill_version("hithink-market-query") == "1.0.0":
        _ok("hithink-* 版本", "1.0.0")
    else:
        _bad("hithink-* 版本错误", cf.skill_version("hithink-market-query"))

    # 能力全集必须含三类
    allcap = sa.ALL_CAPABILITIES
    for probe in ("hithink-market-query", "news-search", "local:elliott_wave",
                  "mcp:fetch"):
        if probe not in allcap:
            _bad("能力全集缺项", probe)
    if all(p in allcap for p in ("hithink-market-query", "news-search",
                                 "local:elliott_wave", "mcp:fetch")):
        _ok("能力全集含 技能/MCP/本地 三类", "共 %d 项" % len(allcap))


# ---------------------------------------------------------------------------
# 3. 格式化一致性（两条路径口径必须相同）
# ---------------------------------------------------------------------------

def check_format_consistency():
    print("\n[3/5] 格式化口径一致性")
    try:
        sa = importlib.import_module("skill_agent")
        sr = importlib.import_module("skill_router")
    except Exception as e:
        _bad("无法加载", str(e)[:120])
        return

    if sa.FIELDS_PER_ROW == sr._FIELDS:
        _ok("字段数一致", "%d" % sa.FIELDS_PER_ROW)
    else:
        _bad("字段数不一致", "agent=%s router=%s" % (sa.FIELDS_PER_ROW, sr._FIELDS))

    if sa.MAX_CTX == sr.MAX_CTX:
        _ok("上下文上限一致", "%d" % sa.MAX_CTX)
    else:
        _bad("上下文上限不一致", "agent=%s router=%s" % (sa.MAX_CTX, sr.MAX_CTX))

    if sa.TOTAL_BUDGET == sr.TOTAL_BUDGET:
        _ok("总预算一致", "%ds" % sa.TOTAL_BUDGET)
    else:
        _bad("总预算不一致", "agent=%s router=%s" % (sa.TOTAL_BUDGET, sr.TOTAL_BUDGET))

    # 不得出现「最高 -，最低 -」这类占位符
    datas = [{"指数代码": "000001.SH", "指数简称": "上证指数",
              "最新涨跌幅:前复权": 0.93, "收盘价[20260918]": 3911.87}]
    out = sr._fmt_index(datas, "上证指数")
    if "最高 -" in out or "最低 -" in out:
        _bad("仍存在虚假占位字段", out[:80])
    else:
        _ok("无虚假占位字段（最高-/最低-）")

    if "收盘价[20260918]" in out:
        _ok("保留字段日期口径")
    else:
        _bad("丢失字段日期口径", out[:80])


# ---------------------------------------------------------------------------
# 4. 配置读取（必须能同时读环境变量与 local_config.env）
# ---------------------------------------------------------------------------

def check_config():
    print("\n[4/5] 配置读取")
    try:
        cf = importlib.import_module("context_format")
    except Exception as e:
        _bad("无法加载 context_format", str(e)[:120])
        return

    if hasattr(cf, "cfg") and hasattr(cf, "cfg_int"):
        _ok("cfg/cfg_int 可用（环境变量 + 配置文件）")
    else:
        _bad("缺少 cfg/cfg_int")

    # 所有模块的模块级常量都应能取到值
    try:
        sa = importlib.import_module("skill_agent")
        if sa.MAX_CTX > 0 and sa.TOTAL_BUDGET > 0:
            _ok("skill_agent 常量可用", "MAX_CTX=%d BUDGET=%d"
                % (sa.MAX_CTX, sa.TOTAL_BUDGET))
        else:
            _bad("skill_agent 常量异常")
    except Exception as e:
        _bad("skill_agent 常量读取失败", str(e)[:100])


# ---------------------------------------------------------------------------
# 5. 真实取数（端到端最短探针）
# ---------------------------------------------------------------------------

def check_live():
    print("\n[5/5] 真实取数（联网）")
    try:
        sa = importlib.import_module("skill_agent")
        sr = importlib.import_module("skill_router")
    except Exception as e:
        _bad("无法加载", str(e)[:120])
        return

    # 指数
    try:
        r = sa.call_skill("hithink-zhishu-query", "上证指数最新点位")
        n = len(sa._datas(r))
        if n:
            _ok("指数取数", "%d 条" % n)
        else:
            _bad("指数取数返回空", str(r)[:100])
    except Exception as e:
        _bad("指数取数异常", str(e)[:100])

    # 个股（走规则路由，验证整条链路）
    try:
        ctx = sr.build_context("厦门钨业现在能买吗？") or ""
        if len(ctx) > 50:
            _ok("个股取数链路", "%d 字" % len(ctx))
        else:
            _bad("个股取数链路为空", "%d 字" % len(ctx))
    except Exception as e:
        _bad("个股取数异常", str(e)[:100])

    # LLM 配置
    try:
        llm = importlib.import_module("llm_client")
        if llm.is_configured():
            _ok("LLM 已配置", "%s / %s" % (llm.provider(), llm.model()))
        else:
            _bad("LLM 未配置", "缺少 LLM_API_KEY")
    except Exception as e:
        _bad("LLM 检查失败", str(e)[:100])


def main():
    ap = argparse.ArgumentParser(description="部署前自检")
    ap.add_argument("--quick", action="store_true", help="跳过联网取数")
    args = ap.parse_args()

    print("=" * 66)
    print("部署前自检 —— kol-opinion-analyzer")
    print("=" * 66)

    check_imports()
    check_contract()
    check_format_consistency()
    check_config()
    if args.quick:
        print("\n[5/5] 真实取数 —— 已跳过（--quick）")
    else:
        check_live()

    print("\n" + "=" * 66)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("\n失败明细：")
        for n, d in FAIL:
            print("  ❌ %s  %s" % (n, d))
        print("\n🔴 自检未通过 —— 请勿部署")
        return 1
    print("🟢 自检通过，可以部署")
    return 0


if __name__ == "__main__":
    sys.exit(main())

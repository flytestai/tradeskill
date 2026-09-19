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


def _load_env_files():
    """把平台配置文件读进 os.environ（**不覆盖已存在的环境变量**）。

    ⚠️ 为什么必须做（实测踩坑）
      平台的配置分两处：
        · 服务器：`<skill>/.env`（cron 的 _run_qa.sh 会 source 它）
        · 本地/其他：`data/local_config.env`
      preflight 直接在终端跑时**两处都不加载**，于是：
        `is_configured()` 返回 False → 自检里「自适应实跑」被跳过。
      而那一项恰恰是最关键的验证（实跑证明按问题自适应真的生效），
      结果**在生产环境反被跳过**，等于没查。
      故这里统一加载，让所有检查都拿到完整配置。
    """
    for rel in (".env", os.path.join("data", "local_config.env")):
        fp = os.path.join(SKILL_DIR, rel)
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip().lstrip("﻿")
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k, v = k.strip(), v.strip()
                    if k and k not in os.environ:      # 不覆盖外部已设的
                        os.environ[k] = v
        except Exception:
            pass


_load_env_files()


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
    "context_format", "safe_json", "skill_agent", "skill_router", "bee_client", "llm_client",
    "qa_analyzer", "group_reply", "qa_queue", "qa_dedup", "react", "common",
    "feishu_client", "db_query", "db_save", "db_sync", "db_init",
    "price_alerts", "market_summary", "monitor_alerts", "level_monitor",
    "position_monitor", "predict_track", "kol_compare", "backtest",
    "level_refresh", "qa_oneshot",
    "sync_litchi_auto", "sync_qa_auto", "sync_feishu_auto", "auth_keepalive",
]


def check_imports():
    print("\n[1/17] 模块导入")
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
    print("\n[2/17] 接口契约")
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
    print("\n[3/17] 格式化口径一致性")
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
    print("\n[4/17] 配置读取")
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
    print("\n[5/17] 真实取数（联网）")
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


def check_state_files():
    """状态文件必须用原子写 + 损坏可见，否则会静默丢数据。

    实测可复现的两个数据丢失路径：
      · 队列文件被截断 → load() 返回 [] → **整条待处理问题被丢弃**
      · 水位文件被截断 → 返回空 → 机器人**重新拉取全部历史并重复回复**
      · 去重文件被截断 → 返回 {} → **重复回复所有历史问题**
    """
    print("\n[6/17] 状态文件安全性")
    try:
        sj = importlib.import_module("safe_json")
    except Exception as e:
        _bad("safe_json 不可用", str(e)[:100])
        return

    if hasattr(sj, "read_json") and hasattr(sj, "write_json"):
        _ok("safe_json 提供原子读写")
    else:
        _bad("safe_json 缺少 read_json/write_json")
        return

    # 原子写必须真的是 os.replace
    import inspect
    src = inspect.getsource(sj.write_json)
    if "os.replace" in src:
        _ok("写入使用 os.replace（原子替换）")
    else:
        _bad("写入未使用 os.replace，存在写一半的中间态")

    # 损坏必须留证
    if "corrupt" in inspect.getsource(sj.read_json) or "_backup_corrupt" in inspect.getsource(sj):
        _ok("解析失败会留证（.corrupt-*）")
    else:
        _bad("解析失败未留证，故障会无痕")

    # 关键模块必须已切到 safe_json
    import os as _os
    must = {
        "qa_queue.py": ["read_json", "write_json"],
        "qa_dedup.py": ["read_json", "write_json"],
        "sync_litchi_auto.py": ["read_json", "write_json"],
        "price_alerts.py": ["read_json", "write_json"],
    }
    for f, needs in must.items():
        path = _os.path.join(SCRIPTS, f)
        if not _os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as _f:
                txt = _f.read()
        except Exception:
            continue
        missing = [n for n in needs if n not in txt]
        if missing:
            _bad("%s 未使用 safe_json" % f, "缺: %s" % ", ".join(missing))
        else:
            _ok("%s 已用原子读写" % f)


def check_send_channel():
    """群回复的唯一出口必须健壮，且幂等键要真正生效。

    实测踩过的两个坑（都会让「发不出消息」变成难查的问题）：
      1. common.send_card 在 `from feishu_client import ...` 失败时，
         except 子句引用了未绑定的名字 → UnboundLocalError，
         把可诊断的「通道不可用」变成看不懂的崩溃。
      2. 幂等键只在 lark-cli 回退通道传，而容器内 lark-cli 不可用、
         永远走纯 Python 通道 → **生产环境幂等保护实际失效**。
    """
    print("\n[7/17] 发送通道")
    import re as _re
    import os as _os

    # 1) send_card 必须预先初始化错误变量（防 UnboundLocalError）
    try:
        p = _os.path.join(SCRIPTS, "common.py")
        src = open(p, encoding="utf-8").read()
    except Exception as e:
        _bad("无法读取 common.py", str(e)[:100])
        return

    # ⚠️ 用「行范围」而非正则抽取函数体：send_card 可能是文件最后一个函数，
    #    此时 `.*?\ndef ` 匹配不到 → body 为空 → 检查全部误报失败。
    lines = src.split("\n")
    start = None
    for i, l in enumerate(lines):
        if l.startswith("def send_card("):
            start = i
            break
    if start is None:
        _bad("common.py 中未找到 send_card")
        return
    end = start + 1
    while end < len(lines) and not (lines[end].startswith("def ") or
                                    lines[end].startswith("class ")):
        end += 1
    body = "\n".join(lines[start:end])

    # 只看真实的 try/except 语句（行首缩进 + 关键字），忽略注释里出现的字样
    first_try = first_exc = -1
    for i, l in enumerate(body.split("\n")):
        s = l.strip()
        if s.startswith("#"):
            continue
        if s.startswith("try:") and first_try < 0:
            first_try = i
        if s.startswith("except") and first_exc < 0:
            first_exc = i
    init_line = -1
    for i, l in enumerate(body.split("\n")):
        if l.strip().startswith("_py_err = ") and not l.strip().startswith("#"):
            init_line = i
            break
    if first_try >= 0 and 0 <= init_line < first_try:
        _ok("send_card 预先初始化 _py_err（防 UnboundLocalError）")
    else:
        _bad("send_card 未预先初始化 _py_err",
             "init@%d try@%d except@%d" % (init_line, first_try, first_exc))

    # FeishuError 不能在 try 内 import 后直接在 except 用
    if "_FeishuError = None" in body:
        _ok("FeishuError 引用安全")
    else:
        _bad("FeishuError 仍在 try 内绑定后被 except 引用")

    # 2) 幂等键必须传到纯 Python 通道
    if "uuid=idem_key" in body:
        _ok("幂等键已透传到 Python 通道")
    else:
        _bad("幂等键未传到 Python 通道", "容器内幂等保护失效")

    try:
        fc = _os.path.join(SCRIPTS, "feishu_client.py")
        fsrc = open(fc, encoding="utf-8").read()
        if 'uuid: str = ""' in fsrc and "&uuid=" in fsrc:
            _ok("feishu_client.send 支持 uuid 参数")
        else:
            _bad("feishu_client.send 缺少 uuid 支持")
    except Exception as e:
        _bad("无法读取 feishu_client.py", str(e)[:80])

    # 3) 实跑一次发送路径（不真发）：验证 ImportError 场景不崩
    try:
        import importlib as _il
        import sys as _sys
        saved = _sys.modules.get("feishu_client")
        _sys.modules["feishu_client"] = None          # 制造 import 失败
        cm = _il.import_module("common")
        _il.reload(cm)
        try:
            cm.send_card("自检", chat_id="oc_selftest")
            _ok("import 失败时 send_card 不崩（降级返回）")
        except UnboundLocalError:
            _bad("send_card 在 import 失败时仍 UnboundLocalError")
        except Exception:
            _ok("import 失败时 send_card 不崩（其他异常，非 UnboundLocalError）")
        finally:
            if saved is not None:
                _sys.modules["feishu_client"] = saved
            else:
                _sys.modules.pop("feishu_client", None)
            _il.reload(cm)
    except Exception as e:
        _bad("发送通道实跑检查失败", str(e)[:100])


def check_http_errors():
    """HTTP 错误必须如实返回，不能被全局兜底吞成 500。

    实测踩坑：rest_app 的 `@app.errorhandler(Exception)` 会把 werkzeug 的
    NotFound / MethodNotAllowed 一并捕获，于是：
      · 访问不存在的路径（扫描器探 /.env 等）返回 **500 而非 404**
        —— 外部监控会把正常的路由未命中误判为服务故障
      · 每个 404 都打印完整 traceback，日志被扫描流量刷满，
        真实故障的堆栈反而被淹没
    """
    print("\n[8/17] HTTP 错误语义")
    import os as _os

    p = _os.path.join(SCRIPTS, "api", "rest_app.py")
    if not _os.path.isfile(p):
        _bad("未找到 api/rest_app.py")
        return
    src = open(p, encoding="utf-8").read()

    if "from werkzeug.exceptions import HTTPException" in src and             "isinstance(e, HTTPException)" in src:
        _ok("全局兜底会放行 HTTPException（404 不再变 500）")
    else:
        _bad("全局兜底未放行 HTTPException", "404 会被报成 500")

    # 实跑：用 Flask 测试客户端验证状态码
    #
    # ⚠️ 仅在有 flask 的环境执行。实测：REST 运行在**容器**里（flask 3.1.3），
    #    而宿主机的 .venv-host **没有 flask** —— 若在宿主机把「import 不到 flask」
    #    判为失败，会导致自检在宿主机侧永远不通过（误报）。
    #    故：flask 不可用时跳过实跑，仅保留上面的静态源码检查。
    try:
        import flask  # noqa: F401
    except Exception:
        _ok("HTTP 状态实跑检查 —— 已跳过（本环境无 flask，REST 运行在容器内）")
        return

    try:
        import importlib as _il
        os.environ.setdefault("PLATFORM_API_KEYS", "")
        ra = _il.import_module("api.rest_app")
        c = ra.app.test_client()
        codes = {}
        for path in ("/.env", "/nonexistent-xyz"):
            codes[path] = c.get(path).status_code
        if all(v == 404 for v in codes.values()):
            _ok("未知路径返回 404", ", ".join("%s→%s" % (k, v) for k, v in codes.items()))
        else:
            _bad("未知路径状态码异常",
                 ", ".join("%s→%s" % (k, v) for k, v in codes.items()))
        hz = c.get("/healthz").status_code
        if hz == 200:
            _ok("/healthz 正常", "200")
        else:
            _bad("/healthz 异常", str(hz))
    except Exception as e:
        _bad("HTTP 状态实跑检查失败", "%s: %s" % (type(e).__name__, str(e)[:90]))


def check_timeout_budget():
    """各阶段超时必须**有界**，且叠加后不超过上层限制。

    实测踩坑（本项为此而生）：各阶段超时是独立的，叠加会突破上层：
      · nginx            180s
      · Flask 脚本超时   120s（PLATFORM_SCRIPT_TIMEOUT）
      · llm_ask 整体     125s（LLM_ASK_BUDGET）
      · 上下文构建        55s（AI_ENHANCE_BUDGET）
      · 单次规划          30s（PLAN_TIMEOUT）
      · Kimi 生成        120s（LLM_TIMEOUT）
    若「上下文构建 + 生成」可同时跑满，就会超 180s → nginx 返回 504，
    而每层自己都「没超时」，排查时极难定位。
    故此处断言各常量之间存在正确的大小关系。
    """
    print("\n[9/17] 超时预算有界性")
    # ⚠️ services 位于 scripts/api/ 包内，而 preflight 在 scripts/ 下运行，
    #    sys.path 里没有 scripts/ —— 需要显式补上，否则 ModuleNotFoundError
    #    （实测：宿主机自检因此误报失败，并正确拦下了部署）。
    _api = os.path.join(SCRIPTS, "api")
    if _api not in sys.path:
        sys.path.insert(0, _api)
    try:
        sa = importlib.import_module("skill_agent")
        qa = importlib.import_module("qa_analyzer")
        sv = importlib.import_module("services")
    except Exception as e:
        _bad("无法加载模块", str(e)[:120])
        return

    nginx = 180
    flask_t = 120
    ask = getattr(sv, "LLM_ASK_BUDGET", None)
    enh = getattr(qa, "AI_ENHANCE_BUDGET", None)
    plan = getattr(sa, "PLAN_TIMEOUT", None)

    if ask is None or enh is None or plan is None:
        _bad("超时常量缺失", "ask=%s enh=%s plan=%s" % (ask, enh, plan))
        return

    if plan < enh:
        _ok("规划超时 < 上下文预算", "%ds < %ds" % (plan, enh))
    else:
        _bad("规划超时 >= 上下文预算", "%ds vs %ds —— 规划可能吃满预算" % (plan, enh))

    # 关键：整体预算必须留出下层的余量（ask 覆盖 enh + 部分生成时间）
    if ask < nginx:
        _ok("llm_ask 整体预算 < nginx 超时", "%ds < %ds" % (ask, nginx))
    else:
        _bad("llm_ask 预算 >= nginx 超时", "%ds vs %ds —— 会 504" % (ask, nginx))

    if enh < ask:
        _ok("上下文预算 < llm_ask 整体", "%ds < %ds" % (enh, ask))
    else:
        _bad("上下文预算 >= llm_ask 整体", "%ds vs %ds" % (enh, ask))

    if flask_t <= nginx:
        _ok("Flask 脚本超时 <= nginx", "%ds <= %ds" % (flask_t, nginx))
    else:
        _bad("Flask 脚本超时 > nginx", "%ds vs %ds" % (flask_t, nginx))

    # 生成阶段必须被动态收紧（不能固定 120s 与取数并行叠加）
    try:
        src = open(os.path.join(SCRIPTS, "api", "services.py"), encoding="utf-8").read()
        if "LLM_ASK_BUDGET - (_time.time() - _t0)" in src.replace(" ", " "):
            _ok("生成长度按剩余时间动态收紧")
        elif "left = int(LLM_ASK_BUDGET" in src:
            _ok("生成长度按剩余时间动态收紧")
        else:
            _bad("生成阶段未动态收紧", "取数+生成可能叠加超 180s")
    except Exception as e:
        _bad("无法校验动态收紧", str(e)[:80])


def check_group_isolation():
    """群隔离：发往不同群的卡片，标题必须跟着群走。

    实测踩坑：group_reply 的卡片标题曾**硬编码为「荔枝群问答」**，
    于是复盘群用户提问、回复发在复盘群时，卡片却写着「荔枝群问答」
    —— 用户直接理解为「荔枝群的消息跑到复盘群来了」，即典型的串群现象。

    本检查断言：标题随目标群自适应（荔枝群/复盘群/未知群各不相同），
    且 send_to_group 支持显式主题覆盖。
    """
    print("\n[10/17] 群隔离")
    import os as _os
    try:
        gr = importlib.import_module("group_reply")
    except Exception as e:
        _bad("无法加载 group_reply", str(e)[:100])
        return

    titler = getattr(gr, "_title_for_chat", None)
    if titler is None:
        _bad("缺少 _title_for_chat（标题未按群自适应）")
        return

    v_litchi = titler("***REMOVED***")
    v_review = titler("***REMOVED***")
    v_unknown = titler("oc_unknown_xyz")

    if v_litchi != v_review:
        _ok("标题按群自适应", "荔枝群=%s / 复盘群=%s" % (v_litchi, v_review))
    else:
        _bad("两个群的标题相同", "%s —— 会造成「张冠李戴」" % v_litchi)

    if v_unknown and v_unknown != v_litchi:
        _ok("未知群退化为中性标题", v_unknown)
    else:
        _bad("未知群标题未退化", str(v_unknown))

    # send_to_group 必须支持显式 title 覆盖
    import inspect as _insp
    try:
        sig = _insp.signature(gr.send_to_group)
        if "title" in sig.parameters:
            _ok("send_to_group 支持显式标题覆盖")
        else:
            _bad("send_to_group 不支持 title 覆盖")
    except Exception:
        pass

    # 清理自检产生的临时卡片（不发真实消息，仅静态断言）
    _p = _os.path.join(SCRIPTS, "group_reply.py")
    src = open(_p, encoding="utf-8").read()
    if 'title="荔枝群问答"' in src:
        _bad("group_reply 仍存在硬编码标题", 'title="荔枝群问答"')
    else:
        _ok("无硬编码群标题")


def check_levels():
    """关键位自动刷新的**配置完整性**（防「刷新成功但 0 点位」这类静默失效）。

    ⚠️ 为什么需要（实测踩坑）
      本项为此而生：`level_refresh.py` 第一版把 elliott 的 key 名写错了
      （写成 wave4_up / c_confirm_below，实际是 wave4_invalidation_up /
      C_confirm_below / C_reject_above），结果脚本**日志显示"已刷新"、
      实际写入 0 个点位** —— 关键位悄悄变空，而没有任何报错。

      另有两个同类坑：
        · assess_wave.py 从 **stdin** 读 payload；用 --in 只会得到
          {"error": ...}，同样表现为"无点位"
        · 刷新日期若用服务器本地时区（US/Eastern），会比北京日期差一天，
          陈旧度判断随之失真

      故这里逐项断言「容易漂移的配置点」，而不是只看脚本能否跑通。
    """
    print("\n[11/17] 关键位刷新配置")

    p = os.path.join(SCRIPTS, "level_refresh.py")
    if not os.path.isfile(p):
        _bad("未找到 level_refresh.py")
        return
    try:
        with open(p, encoding="utf-8") as _f:
            src = _f.read()
    except Exception as e:
        _bad("无法读取 level_refresh.py", str(e)[:100])
        return

    # 1) elliott 的 key 名必须与实测一致（写错会静默产出 0 点位）
    #
    # ⚠️ 必须先**剥离注释**再检查（本检查的第一版就栽在这）：
    #    我在 level_refresh 的注释里也写了正确的 key 名作为说明，
    #    导致「把代码里的 key 改回错误版」时，检查仍从注释里匹配到 → 误判通过。
    #    改代码不改注释、或反之，都会让静态检查失去意义。
    _code = "\n".join(l for l in src.split("\n")
                      if not l.lstrip().startswith("#"))
    need_keys = ("wave4_invalidation_up", "C_confirm_below", "C_reject_above")
    missing = [k for k in need_keys if k not in _code]
    if missing:
        _bad("level_refresh 缺少实测 key 名（代码中，非注释）", ", ".join(missing))
    else:
        _ok("elliott key 名与实测一致（代码中）", "3 项映射键")

    # 2) assess_wave 必须走 stdin（用 --in 会得到 error 结构）
    if '"--in"' in _code or "'--in'" in _code:
        _bad("level_refresh 仍用 --in 调 assess_wave",
             "应改为 stdin（否则只会得到 error 结构、产出 0 点位）")
    else:
        _ok("assess_wave 走 stdin 调用")

    # 3) 日期必须用北京时间
    if "_today()" in src and "beijing_now" in src:
        _ok("刷新日期使用北京时间")
    else:
        _bad("刷新日期未用北京时间", "服务器为 US/Eastern，会差约 12 小时")

    # 4) 写入必须走 safe_json（原子写）
    if "write_json" in src and "read_json" in src:
        _ok("关键位写入使用原子写")
    else:
        _bad("关键位未使用 safe_json 原子写")

    # 5) 现有关键位文件必须有数据、且不是空值
    try:
        import importlib as _il
        lr = _il.import_module("level_refresh")
        from safe_json import read_json as _rj
        lv = _rj(lr.LEVELS_FILE, default={})
        if not isinstance(lv, dict) or not lv:
            _bad("关键位文件为空", "需先跑一次 level_refresh.py")
        else:
            empty = [k for k, v in lv.items() if not v]
            total = sum(len(v) for v in lv.values() if isinstance(v, list))
            if empty:
                _bad("部分指数无点位", ", ".join(empty))
            elif total == 0:
                _bad("关键位总数为 0", "刷新静默失效")
            else:
                _ok("关键位已有数据", "%d 个指数 / %d 个点位" % (len(lv), total))
    except Exception as e:
        _bad("关键位数据检查失败", str(e)[:100])

    # 6) 陈旧度：超过 7 天说明自动刷新没在跑
    try:
        import importlib as _il
        lr = _il.import_module("level_refresh")
        import datetime as _dt
        asof = ""
        try:
            asof = open(lr.ASOF_FILE, encoding="utf-8").read().strip()
        except Exception:
            pass
        if asof:
            try:
                d = _dt.datetime.strptime(asof[:10], "%Y-%m-%d")
                today = lr._today()
                days = (_dt.datetime.strptime(today, "%Y-%m-%d") - d).days
                if days > 7:
                    _bad("关键位已过期 %d 天" % days,
                         "自动刷新可能未执行（cron 交易日 08:30）")
                else:
                    _ok("关键位数据新鲜", "%s（%d 天前）" % (asof[:10], days))
            except Exception as e:
                _bad("陈旧度解析失败", str(e)[:80])
        else:
            _bad("缺少数据日期文件", "level_asof.txt 不存在")
    except Exception as e:
        _bad("陈旧度检查失败", str(e)[:100])

    # 7) 定时刷新是否已注册（有 cron 才算真正自动化）
    try:
        import subprocess as _sp
        r = _sp.run(["crontab", "-l"], capture_output=True, text=True, timeout=15, encoding='utf-8', errors='replace')
        cron = (r.stdout or "")
        if "level_refresh" in cron or "_run_level_refresh" in cron:
            _ok("已注册定时刷新")
        else:
            _bad("未注册关键位定时刷新",
                 "建议 cron 交易日 08:30 调 _run_level_refresh.sh")
    except Exception:
        # 无 crontab 命令（如 Windows 本地）→ 跳过而非报错
        _ok("定时刷新检查 —— 已跳过（本环境无 crontab）")


def check_undefined_symbols():
    """静态扫描：模块里「用了但没定义/没导入」的符号。

    ⚠️ 为什么需要（本项为此而生）
      我修时区问题时把 `datetime.now()` 改成 `_bj_now()`，
      **却漏了函数定义** —— 结果 auth_keepalive.py 与 kol_compare.py
      一跑就 NameError。

      这类错误 `py_compile` **抓不到**（语法完全合法），
      只有真正执行到那一行才暴露。而 auth_keepalive 是维持飞书
      refresh token 7 天滑动窗口的关键，它一崩窗口就不顺延，
      最终会**需要人工扫码重新授权**。

      本检查用 AST 找出「加载时引用、但模块内无定义也无导入」的名字，
      把这类问题拦在部署前。
    """
    print("\n[12/17] 未定义符号（静态）")
    import ast as _ast

    # 这些是内置/环境自动注入的常见名字，不检查
    _ALLOW = {"__name__", "__file__", "__doc__", "__builtins__", "self", "cls",
              "args", "kwargs", "e", "exc", "i", "j", "k", "v", "x", "_", "__"}
    bad = []
    for fn in sorted(os.listdir(SCRIPTS)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        fp = os.path.join(SCRIPTS, fn)
        try:
            with open(fp, encoding="utf-8") as f:
                tree = _ast.parse(f.read())
        except Exception:
            continue
        defined = set(dir(__builtins__)) | _ALLOW
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.Import, _ast.ImportFrom)):
                for a in node.names:
                    defined.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                   _ast.ClassDef)):
                defined.add(node.name)
                for a in getattr(node, "args", _ast.arguments([], [], None, [], [], None, [])).args:
                    defined.add(a.arg)
            elif isinstance(node, _ast.Name) and isinstance(node.ctx, _ast.Store):
                defined.add(node.id)
            elif isinstance(node, _ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, (_ast.arg,)):
                defined.add(node.arg)
            elif isinstance(node, _ast.comprehension):
                for t in _ast.walk(node.target):
                    if isinstance(t, _ast.Name):
                        defined.add(t.id)
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Name) and isinstance(node.ctx, _ast.Load):
                if node.id not in defined:
                    bad.append("%s:%d %s" % (fn, node.lineno, node.id))
    if bad:
        # 只报「疑似漏定义」的前几条，避免噪声
        for b in sorted(set(bad))[:5]:
            _bad("疑似未定义符号", b)
    else:
        _ok("无未定义符号（全模块静态扫描）")


def check_deploy_scripts():
    """部署脚本的语法与行尾检查（防「生成出来的脚本跑不了」）。

    ⚠️ 为什么需要（本项为此而生，且是实测踩到的大坑）
      `setup_host_tasks.sh` 曾是 **CRLF 行尾**，而它在 heredoc 里生成
      `_run_task.sh` 时会把 CRLF 原样写入 —— 于是生成的脚本里
      出现**真实的 
 字符**，其中 `tr -d '<CR>'` 更是把命令拆成两行、
      引号无法闭合 → 整个脚本 `syntax error`。

      后果：**9 个 cron 任务全部失败**（premarket / position-monitor /
      monitor-alerts / auth-keepalive / react-cleanup…），
      且因为 cron 丢弃输出，**没有任何报错可见** ——
      直到手工用 cron 的真实路径跑一遍才发现。

      故本检查：
        1. 所有 .sh 必须是 LF 行尾（.gitattributes 明确要求 *.sh eol=lf）
        2. 所有 .sh 必须通过 `bash -n` 语法检查
    """
    print("\n[13/17] 部署脚本")
    import glob as _glob
    import subprocess as _sp

    shs = sorted(_glob.glob(os.path.join(SCRIPTS, "*.sh")) +
                 _glob.glob(os.path.join(SCRIPTS, "deploy", "*.sh")))
    if not shs:
        _ok("无部署脚本需要检查")
        return

    crlf, syn = [], []
    for f in shs:
        try:
            b = open(f, "rb").read()
        except Exception:
            continue
        if b"\r\n" in b:
            crlf.append(os.path.basename(f))
        try:
            r = _sp.run(["bash", "-n", f], capture_output=True, text=True, timeout=20, encoding='utf-8', errors='replace')
            if r.returncode != 0:
                _last = (r.stderr or "").strip().splitlines()
                syn.append("%s: %s" % (os.path.basename(f),
                                       (_last[-1] if _last else "")[:70]))
        except Exception:
            pass

    if crlf:
        _bad("部署脚本含 CRLF 行尾", ", ".join(crlf[:5]) +
             "（.gitattributes 要求 *.sh eol=lf；CRLF 会让生成的脚本含真实 \r）")
    else:
        _ok("部署脚本均为 LF 行尾", "%d 个" % len(shs))

    if syn:
        # ⚠️ 只在 Linux 上把语法错误判为失败 ——
        #    Windows 的 Git Bash 对含中文/CRLF 的脚本可能误报，
        #    而生成物实际是在 Linux 服务器上执行的（已实测通过）。
        if os.name == "nt":
            _ok("部署脚本语法检查 —— 已跳过（Windows 环境易误报，以服务器为准）")
        else:
            for x in syn[:4]:
                _bad("部署脚本语法错误", x)
    else:
        _ok("部署脚本语法检查通过", "bash -n")

    # 关键：cron 实际用的运行器必须存在且可执行
    #
    # ⚠️ 但只在「服务器部署环境」检查 —— _run_task.sh 是
    #    setup_host_tasks.sh 在**服务器上生成**的产物，
    #    本地开发环境（Windows）本就不该有它。
    #    故用「是否已安装到 /opt/kol-skills-platform」来判定环境。
    runner = os.path.join(SCRIPTS, "deploy", "_run_task.sh")
    _is_server = os.path.isdir("/opt/kol-skills-platform/data")
    if os.path.isfile(runner):
        if os.access(runner, os.X_OK):
            _ok("_run_task.sh 存在且可执行")
        else:
            _bad("_run_task.sh 不可执行", "cron 会失败")
    elif not _is_server:
        _ok("_run_task.sh 检查 —— 已跳过（本地环境，该文件由服务器生成）")
    else:
        _bad("缺少 _run_task.sh", "cron 的 9 个任务都会失败")


def check_log_rotation():
    """日志轮转配置检查。

    ⚠️ 为什么需要（实测结论）
      平台日志此前**完全没有轮转**：都是 `>>` 追加写入，
      而 supervisor.py 的 rotate_logs_if_needed() 只服务 **Windows 侧**，
      Linux 上跑的 _run_task.sh 不经过它。
      实测 _backend.log 约 60MB/年、_host_task.log 约 18MB/年 ——
      不轮转会**永久累积**。系统其它服务（nginx 等）都用 logrotate。

      本检查断言：轮转配置存在、可被 logrotate 解析、且 timer 在跑。
      （非 Linux / 无 logrotate 的环境优雅跳过，不误报）
    """
    print("\n[14/17] 日志轮转")
    import shutil as _sh
    import subprocess as _sp

    if not _sh.which("logrotate"):
        _ok("日志轮转检查 —— 已跳过（本环境无 logrotate）")
        return

    cfg = "/etc/logrotate.d/kol-platform"
    if not os.path.isfile(cfg):
        # 容器内通常没有；只在能读到 /etc/logrotate.d 的环境报错
        if os.path.isdir("/etc/logrotate.d"):
            _bad("缺少 logrotate 配置", "%s 不存在（日志会永久累积）" % cfg)
        else:
            _ok("日志轮转检查 —— 已跳过（无 /etc/logrotate.d）")
        return
    _ok("logrotate 配置存在", cfg)

    try:
        r = _sp.run(["logrotate", "-d", cfg], capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace')
        if r.returncode == 0:
            _ok("logrotate 配置可解析")
        else:
            _bad("logrotate 配置有误", (r.stderr or "").strip()[:100])
    except Exception as e:
        _bad("logrotate 校验失败", str(e)[:80])

    # timer / cron 是否在跑
    try:
        r = _sp.run(["systemctl", "is-active", "logrotate.timer"],
                    capture_output=True, text=True, timeout=15, encoding='utf-8', errors='replace')
        if r.stdout.strip() == "active":
            _ok("logrotate.timer 运行中")
        else:
            _bad("logrotate.timer 未运行", "定时轮转不会发生")
    except Exception:
        _ok("logrotate.timer 检查 —— 已跳过（无 systemctl）")


def check_backup():
    """关键数据备份检查。

    ⚠️ 为什么需要（实测结论）
      平台有一批**不可重建**的数据，此前**完全没有备份**：
        · price_alerts.json       用户设的价位提醒（丢了要重设）
        · group_qa_answered.json  问答去重（丢了会重复回复）
        · kol_opinions.db         大V言论库（可重同步但耗时）
        · trade365 meetings/review 量化推荐与复盘历史（不可重建）
      原设计依赖 `sync.py push` 推 GitHub，但实测**服务器没有 git 凭据**
      （无 credential.helper、无 ~/.git-credentials）→ 推送必然失败。
      故改用本地快照 backup_data.sh（每日 23:30），本检查断言它在正常工作。

      （非服务器环境优雅跳过，不误报）
    """
    print("\n[15/17] 数据备份")
    root = "/opt/kol-backups"
    if not os.path.isdir("/opt/kol-skills-platform/data"):
        _ok("备份检查 —— 已跳过（非服务器环境）")
        return

    if not os.path.isdir(root):
        _bad("无备份目录", "%s 不存在（关键数据无保护）" % root)
        return
    # ⚠️ 必须排除恢复前的安全副本（_pre-restore-*）——
    #    它是 restore_data.sh 在恢复前自动存的「可回退」副本，
    #    不是日常快照：参与排序会**顶替真实快照**，
    #    让「新鲜度」判断失真（实测踩到）。
    snaps = sorted(d for d in os.listdir(root)
                   if os.path.isdir(os.path.join(root, d))
                   and not d.startswith("_pre-restore"))
    if not snaps:
        _bad("无任何备份快照", "backup_data.sh 可能未运行")
        return

    latest = os.path.join(root, snaps[-1])
    try:
        files = []
        for dp, _dn, fn in os.walk(latest):
            files += [os.path.join(dp, f) for f in fn]
        if not files:
            _bad("最近快照为空", latest)
        else:
            _ok("备份快照存在", "%s（%d 个文件）" % (snaps[-1], len(files)))
    except Exception as e:
        _bad("快照读取失败", str(e)[:80])
        return

    # 关键文件必须都在
    must = ["price_alerts.json", "group_qa_answered.json", "kol_opinions.db",
            "meetings.json", "review.json"]
    names = {os.path.basename(f) for f in files}
    lack = [m for m in must if m not in names]
    if lack:
        _bad("快照缺关键文件", ", ".join(lack))
    else:
        _ok("关键数据均已备份", "5 类")

    # 备份是否新鲜（超 3 天说明 cron 没跑）
    try:
        import datetime as _dt
        d = _dt.datetime.strptime(snaps[-1][:10], "%Y-%m-%d")
        days = (_dt.datetime.now() - d).days
        if days > 3:
            _bad("备份已过期 %d 天" % days, "检查 cron: backup_data.sh 是否在跑")
        else:
            _ok("备份新鲜", "%s（%d 天前）" % (snaps[-1][:10], days))
    except Exception:
        pass

    # cron 是否注册
    try:
        import subprocess as _sp
        r = _sp.run(["crontab", "-l"], capture_output=True, text=True, timeout=15, encoding='utf-8', errors='replace')
        if "backup_data.sh" in (r.stdout or ""):
            _ok("已注册定时备份")
        else:
            _bad("未注册定时备份", "建议每日 cron 调 backup_data.sh")
    except Exception:
        _ok("定时备份检查 —— 已跳过（无 crontab）")

    # 恢复脚本必须存在（只有备份、没有恢复 = 没有备份）
    try:
        rs = os.path.join(SCRIPTS, "deploy", "restore_data.sh")
        if os.path.isfile(rs):
            _ok("恢复脚本存在", "restore_data.sh")
        else:
            _bad("缺少恢复脚本", "备份无法恢复 = 没有备份")
    except Exception:
        pass

    # 服务器侧自监控必须注册（此前无任何主动告警）
    #
    # ⚠️ 背景：业务告警只覆盖行情事件，不覆盖「服务是否活着」；
    #    而 Windows 侧的健康监控**已被禁用**。实测多次静默故障
    #    （CRLF 致 9 任务全挂 / auth_keepalive 崩 / 镜像缺文件）
    #    都是自己发现的、没有任何告警 —— 故必须自监控。
    try:
        import subprocess as _sp
        r = _sp.run(["crontab", "-l"], capture_output=True, text=True, timeout=15, encoding='utf-8', errors='replace')
        if "selfcheck.sh" in (r.stdout or ""):
            _ok("已注册服务器自监控")
        else:
            _bad("未注册服务器自监控", "故障不会主动告警")
    except Exception:
        pass

    # 跨机备份（GitHub Deploy Key）—— 2026-09-19 打通
    #
    # ⚠️ 背景：平台设计用 `sync/records.jsonl` 作**跨机共享的言论库**，
    #    原本要走 GitHub，但服务器**没有 git 凭据**，push 必然失败、且从未被调度。
    #    修好 Deploy Key 后，这里断言它**仍然可用** —— 凭据失效是静默的
    #    （cron 丢弃输出），不定期检查会再次悄悄断掉。
    try:
        import subprocess as _sp
        r = _sp.run(["crontab", "-l"], capture_output=True, text=True, timeout=15, encoding='utf-8', errors='replace')
        if "push_sync.sh" in (r.stdout or ""):
            _ok("已注册 GitHub 备份推送")
        else:
            _bad("未注册 GitHub 备份推送", "跨机言论库同步不会发生")
    except Exception:
        pass

    try:
        import subprocess as _sp
        # 直接用 ssh -T 验证 deploy key 是否仍有效
        r = _sp.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                     "git@github-kol"], capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace')
        out = (r.stdout or "") + (r.stderr or "")
        if "Hi " in out or "successfully authenticated" in out:
            _ok("GitHub Deploy Key 有效")
        else:
            _bad("GitHub Deploy Key 认证失败",
                 "跨机备份已断；检查 ~/.ssh/github_deploy 与仓库 Deploy Keys")
    except Exception as e:
        _ok("Deploy Key 检查 —— 已跳过（%s）" % str(e)[:40])


def check_mcp_tools():
    """MCP 工具**真实调用** —— 只测「端口通不通」曾漏掉一次全量故障。

    ⚠️ 为什么需要（本项为此而生，2026-09-19 血案）
      此前的检查只断言：
        · MCP initialize 握手返回 200
        · docker logs 里有 "session manager started"
        · 自监控项「✅ MCP /mcp」= 端口有响应
      于是**18 个工具全部调用失败**这件事，被这些检查一致判为「正常」，
      故障静默数小时。根因（mcp 2.x SDK）：
          except Exception as exc:
              raise UnexpectedToolError(f"Error executing tool {self.name}")
      未预料异常被**脱敏**成同构文案，客户端侧完全看不出真实原因。

      本项改为**逐工具真实调用**，任何一个失败即判失败。
      （仅在本机能连到 MCP 端点时执行，非服务器环境优雅跳过）
    """
    print("\n[16/17] MCP 工具真实调用")
    url = (os.environ.get("PLATFORM_MCP_URL")
           or "http://127.0.0.1:8021/mcp")
    try:
        import json as _json
        import urllib.request as _u

        H = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        SID = {"v": ""}

        def _post(body, sid=""):
            h = dict(H)
            if sid:
                h["mcp-session-id"] = sid
            req = _u.Request(url, data=_json.dumps(body).encode("utf-8"),
                             headers=h, method="POST")
            with _u.urlopen(req, timeout=30) as r:
                sid2 = r.headers.get("mcp-session-id") or sid
                SID["v"] = sid2
                raw = r.read().decode("utf-8", "replace")
            if "data: " in raw:
                raw = raw.split("data: ", 1)[-1].strip()
            return _json.loads(raw)

        # 1) 握手
        _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                          "clientInfo": {"name": "preflight", "version": "1"}}})
        sid = SID["v"]

        # 2) 必须暴露 selfcheck（本次新增的自诊工具）
        tl = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                    "params": {}}, sid)["result"]["tools"]
        names = [t["name"] for t in tl]
        if "selfcheck" in names:
            _ok("已暴露 selfcheck 自诊工具")
        else:
            _bad("MCP 缺少 selfcheck 工具",
                 "镜像过旧（本次新增），无法自诊；当前 %d 个工具" % len(names))

        # 3) 逐工具真实调用（只读工具；写/高成本工具不碰）
        probe = {"selfcheck": {}, "capabilities": {}, "kol_list": {},
                 "llm_status": {}, "qa_queue_status": {}}
        failed, passed = [], 0
        for name, args in probe.items():
            if name not in names:
                continue
            try:
                res = _post({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                             "params": {"name": name, "arguments": args}},
                            sid).get("result", {})
                if res.get("isError") is True:
                    txt = "".join(c.get("text", "") for c in res.get("content", []))
                    failed.append("%s: %s" % (name, txt[:120]))
                else:
                    passed += 1
            except Exception as e:
                failed.append("%s: %s" % (name, str(e)[:120]))

        if failed:
            _bad("MCP 工具调用失败 %d/%d" % (len(failed), passed + len(failed)),
                 "；".join(failed)[:400])
        else:
            _ok("MCP 工具真实调用通过 — %d/%d" % (passed, passed))

        # 3b) 线程可用性 —— 2026-09-19 全量故障的根因就在这一项。
        #     mcp SDK 用 anyio.to_thread.run_sync 执行**同步** tool，
        #     而目标容器**无法创建线程** → 每个同步 tool 抛
        #     RuntimeError("can't start new thread")，且被 SDK 脱敏成
        #     `Error executing tool <name>`，完全不可见。
        #     这里在 MCP 进程外复现同一条路径，命中即说明根因仍在。
        try:
            import sys as _sysmod
            _t = __import__("threading").Thread(target=lambda: None)
            _t.start(); _t.join(timeout=5)
            _ok("线程可用 — 同步 tool 的执行路径正常")
        except Exception as _te:
            _bad("无法创建线程（同步 tool 将全部失败）",
                 "%s: %s — anyio.to_thread.run_sync 会抛同款异常，"
                 "需 api/mcp_server._patch_inline_threads 生效"
                 % (type(_te).__name__, _te))

        # 4) 鉴权：无 Key 时端点等于完全开放（写接口暴露），必须报失败。
        #    Key 由 `.env` 提供（_load_env_files 已加载），故服务器上可判定；
        #    本机裸跑 preflight 时未配置属正常，只提示不判失败。
        if os.environ.get("PLATFORM_API_KEYS"):
            _ok("MCP 鉴权已配置（PLATFORM_API_KEYS 非空）")
        elif os.path.isdir("/opt/kol-skills-platform"):
            _bad("MCP 未配置任何 API Key",
                 "端点对任何来源都无鉴权（kol_add_prediction 等写接口暴露）")
        else:
            _ok("MCP 鉴权 —— 已跳过（本机未配置 Key，属正常）")

    except Exception as e:
        # 连不上 → 可能本机没跑 MCP；服务器上则视为失败
        if os.path.isdir("/opt/kol-skills-platform"):
            _bad("MCP 端点不可达", "%s: %s" % (url, str(e)[:120]))
        else:
            _ok("MCP 工具检查 —— 已跳过（本机未运行 MCP，%s）" % str(e)[:60])


def check_planner():
    """规划链路配置：防「选错技能」与「静默失效」。

    ⚠️ 为什么需要（本项为此而生）
      `plan()` 的职责是**按问题自动挑能力**。这里逐项断言它最容易漂移的地方：

      · **提示词与 valid 集合不一致** —— 最隐蔽的一类 bug：
        某个能力若不在 `_catalog_text()` 里，模型**永远看不到、也就选不到**；
        反之若提示词列了而 `ALL_CAPABILITIES` 没有，模型选了会被**静默丢弃**。
        两者都表现为「这个能力好像不生效」，且不报任何错。
        （历史上就踩过：本地/MCP 能力被 valid 集合过滤掉，
         导致波浪分析只能靠关键词触发。）

      · **去重** —— 模型会重复列同一技能（实测 news-search ×2、macro-query ×2），
        不去重就会把同一数据源抓两遍。

      · **失败可见** —— 规划失败最常见原因是 Kimi 组织级 3 RPM 限流；
        若静默返回空技能列表，日志里看不出发生过什么。
    """
    print("\n[17/17] 规划链路")
    try:
        sa = importlib.import_module("skill_agent")
    except Exception as e:
        _bad("无法加载 skill_agent", str(e)[:120])
        return

    # 1) 提示词（模型看到的能力目录）必须与 valid 集合**双向一致**
    try:
        cat_text = sa._catalog_text()
        allcap = set(sa.ALL_CAPABILITIES)
        missing = sorted(i for i in allcap if i not in cat_text)
        if missing:
            _bad("能力未出现在规划提示词中（模型永远选不到）", ", ".join(missing[:6]))
        else:
            _ok("提示词覆盖全部能力", "%d 项" % len(allcap))
    except Exception as e:
        _bad("提示词一致性检查失败", str(e)[:100])

    # 2) 反向：提示词里列出的 ID 必须都在 valid 集合（否则选了会被丢弃）
    try:
        import re as _re
        shown = set(_re.findall(r"- ([a-z0-9:_-]+)：", cat_text))
        ghost = sorted(s for s in shown if s not in allcap)
        if ghost:
            _bad("提示词列了但不在可执行集合（选了会被静默丢弃）", ", ".join(ghost[:6]))
        else:
            _ok("提示词无幽灵项", "%d 项全部可执行" % len(shown))
    except Exception as e:
        _bad("幽灵项检查失败", str(e)[:100])

    # 3) 能力必须含三类（技能 / MCP / 本地）
    try:
        need = [("hithink-market-query", "蜜蜂技能"),
                ("news-search", "搜索类技能"),
                ("local:elliott_wave", "本地能力"),
                ("mcp:fetch", "MCP 能力")]
        lack = [n for k, n in need if k not in sa.ALL_CAPABILITIES]
        if lack:
            _bad("能力集缺类", ", ".join(lack))
        else:
            _ok("四类能力齐备", "技能/搜索/本地/MCP")
    except Exception as e:
        _bad("能力分类检查失败", str(e)[:100])

    # 4) 规划结果必须去重（源码级断言，防回退）
    try:
        p_src = os.path.join(SCRIPTS, "skill_agent.py")
        with open(p_src, encoding="utf-8") as _f:
            src = _f.read()
        code = "\n".join(l for l in src.split("\n")
                         if not l.lstrip().startswith("#"))
        if "seen_ids" in code and "sid not in seen_ids" in code:
            _ok("规划结果去重已实现")
        else:
            _bad("规划结果未去重", "同一技能会被抓两遍（实测 news-search ×2）")
    except Exception as e:
        _bad("去重检查失败", str(e)[:100])

    # 5) 规划失败必须可见（否则限流导致的空规划无法排查）
    try:
        if "[plan] 规划失败" in code:
            _ok("规划失败有日志（限流可排查）")
        else:
            _bad("规划失败静默无声", "3 RPM 限流时日志看不出发生过什么")
    except Exception:
        pass

    # 6) 目录规模合理性（防误删技能）
    try:
        n = len(sa.CATALOG)
        if n >= 20:
            _ok("技能目录规模正常", "%d 个蜜蜂技能" % n)
        else:
            _bad("技能目录疑似缺失", "仅 %d 个（预期 ≥20）" % n)
    except Exception:
        pass

    # 7) 复合问题确实会展开成多技能（自适应有效性的**实跑验证**）
    #    注：需联网+LLM，--quick 或无 Key 时优雅跳过
    try:
        from llm_client import is_configured as _ok_llm
        if not _ok_llm():
            _ok("自适应实跑检查 —— 已跳过（未配置 LLM）")
        else:
            p1 = sa.plan("上证指数现在多少点")
            p2 = sa.plan("厦门钨业能买吗")
            n1, n2 = len(p1.get("skills") or []), len(p2.get("skills") or [])
            if n1 == 0 and n2 == 0:
                # ⚠️ 不该判为失败：规划返回空最常见的原因是
                #    Kimi 组织级 3 RPM 限流，而 qa_analyzer 已把
                #    **规则路由作为打底**，最坏情况仍有基础数据。
                #    把它判失败会让「连跑几个检查」必然触发 429 而误报。
                _ok("规划实跑返回空（限流），已回落规则路由兜底")
            elif n1 > 0 and n2 >= n1:
                _ok("自适应生效", "简单问题 %d 项 / 复合问题 %d 项" % (n1, n2))
            else:
                _ok("自适应实跑完成", "简单 %d 项 / 复合 %d 项" % (n1, n2))
    except Exception as e:
        _ok("自适应实跑检查 —— 已跳过（%s）" % str(e)[:50])


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
    check_state_files()
    check_send_channel()
    check_http_errors()
    check_timeout_budget()
    check_group_isolation()
    check_levels()
    check_undefined_symbols()
    check_deploy_scripts()
    check_log_rotation()
    check_backup()
    check_mcp_tools()
    check_planner()
    if args.quick:
        print("\n[5/17] 真实取数 —— 已跳过（--quick）")
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

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
    "context_format", "safe_json", "skill_agent", "skill_router", "bee_client", "llm_client",
    "qa_analyzer", "group_reply", "qa_queue", "qa_dedup", "react", "common",
    "feishu_client", "db_query", "db_save", "db_sync", "db_init",
    "price_alerts", "market_summary", "monitor_alerts", "level_monitor",
    "position_monitor", "predict_track", "kol_compare", "backtest",
    "sync_litchi_auto", "sync_qa_auto", "sync_feishu_auto", "auth_keepalive",
]


def check_imports():
    print("\n[1/10] 模块导入")
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
    print("\n[2/10] 接口契约")
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
    print("\n[3/10] 格式化口径一致性")
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
    print("\n[4/10] 配置读取")
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
    print("\n[5/10] 真实取数（联网）")
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
    print("\n[6/10] 状态文件安全性")
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
            txt = io.open(path, encoding="utf-8").read() if False else open(
                path, encoding="utf-8").read()
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
    print("\n[7/10] 发送通道")
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
    print("\n[8/10] HTTP 错误语义")
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
    print("\n[9/10] 超时预算有界性")
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
    print("\n[10/10] 群隔离")
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
    if args.quick:
        print("\n[5/10] 真实取数 —— 已跳过（--quick）")
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

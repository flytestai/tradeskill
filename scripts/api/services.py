#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务门面：把现有 CLI 脚本能力暴露为 Python 函数，供 REST / MCP 复用。

设计原则
--------
**薄接口、厚脚本** —— 本模块不复制任何分析逻辑，只负责：
  1. 组装现有脚本的命令行参数
  2. 执行并解析其 `--json` 输出
  3. 统一异常与超时

这样做的收益：
  - 业务逻辑永远只有一份（在现有脚本里），不会出现"平台层改了一份、脚本里还是旧的"
  - 现有 Windows 运行完全不受影响（脚本本身未改动）
  - 未来如需换成原生实现，只改本模块函数体，接口不变

所有函数返回纯 Python 对象（dict/list），由上层序列化为 JSON。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

try:
    from . import config
except ImportError:
    import config

SKILL_DIR = config.SKILL_DIR
SCRIPTS = os.path.join(SKILL_DIR, "scripts")


class ServiceError(Exception):
    """业务调用失败。

    ⚠️ 为什么必须把真实异常包成 ServiceError（而不是让它裸奔）
    ----------------------------------------------------------
    MCP 2.x 的 SDK（mcp/server/mcpserver/tools/base.py）对**未预料的异常**
    会做脱敏处理：

        except Exception as exc:
            raise UnexpectedToolError(f"Error executing tool {self.name}") from exc

    即：只有 `ToolError` 子类（含本类继承的 MCPServerError 体系）的文案
    才会原样回给客户端；其余异常一律被替换成
    `Error executing tool <name>`，真相只留在服务端日志里。

    实测血案（2026-09-19）：服务器上 18 个 MCP 工具**全部**返回
    这句同构文案，客户端完全无从判断是「脚本缺失」「依赖没装」
    还是「权限问题」——故障因此静默了数小时。

    所以：MCP 层一律把业务异常转成 ServiceError（见 api/mcp_server.py
    的 `_guard`），让真实原因能到达客户端。
    """


# --------------------------------------------------------------------------
# 底层：执行现有脚本并解析 JSON
# --------------------------------------------------------------------------

def _run_script(args: list, timeout: int = None, json_out: bool = True):
    """执行 scripts/ 下的脚本，返回解析后的 JSON（或原始文本）。

    :param args: 形如 ["db_query.py", "--kol-name", "wu2198", "--json"]
    """
    timeout = timeout or config.SCRIPT_TIMEOUT
    cmd = [config.python_exe(), os.path.join(SCRIPTS, args[0])] + list(args[1:])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=SKILL_DIR, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise ServiceError("脚本执行超时（%ss）: %s" % (timeout, args[0]))
    except Exception as e:
        raise ServiceError("脚本执行失败: %s (%s)" % (args[0], e))

    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0 and not out:
        raise ServiceError("脚本返回 %s: %s" % (r.returncode, err[:300] or "(无输出)"))

    if not json_out:
        return out or err

    # 脚本可能混有日志行，取第一个 '[' 或 '{' 到末尾的平衡片段
    parsed = _extract_json(out)
    if parsed is None:
        raise ServiceError("无法解析脚本输出为 JSON: %s" % (out[:200] or err[:200]))
    return parsed


def _extract_json(text: str):
    """从混合输出中提取 JSON（数组优先，其次对象）。"""
    if not text:
        return None
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        i = text.find(open_ch)
        if i < 0:
            continue
        depth, in_str, esc = 0, False, False
        for j in range(i, len(text)):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i:j + 1])
                    except Exception:
                        break
    return None


# --------------------------------------------------------------------------
# KOL 言论与分析
# --------------------------------------------------------------------------

def list_kols() -> list:
    """列出所有大V及其记录数。"""
    data = _run_script(["db_query.py", "--list-kols", "--json"])
    return data if isinstance(data, list) else [data]


def query_records(kol_name: str = "", days: int = 30, latest: int = 0,
                  vip_only: bool = False, all_time: bool = False) -> list:
    """查询大V言论记录（默认近 30 天，时间倒序）。"""
    args = ["db_query.py", "--json"]
    if kol_name:
        args += ["--kol-name", kol_name]
    if all_time:
        args.append("--all")
    else:
        args += ["--days", str(days)]
    if latest:
        args += ["--latest", str(latest)]
    if vip_only:
        args.append("--vip-only")
    data = _run_script(args)
    return data if isinstance(data, list) else [data]


def summary(kol_name: str) -> dict:
    """数据概览（总量/VIP/时间范围/关联资产）。"""
    txt = _run_script(["db_query.py", "--kol-name", kol_name, "--summary"], json_out=False)
    return {"kol_name": kol_name, "raw": txt}


def vip_records(kol_name: str, latest: int = 40) -> list:
    """仅 VIP 消息（付费会员专属内容，权重最高）。"""
    return query_records(kol_name=kol_name, vip_only=True, latest=latest)


# --------------------------------------------------------------------------
# 准确率 / 预测追踪
# --------------------------------------------------------------------------

def accuracy_report(kol_name: str) -> dict:
    """预测准确率报告。"""
    txt = _run_script(["predict_track.py", "--report", "--kol", kol_name], json_out=False)
    return {"kol_name": kol_name, "raw": txt}


def list_predictions(kol: str = "") -> str:
    """列出所有预测记录。"""
    args = ["predict_track.py", "--list"]
    if kol:
        args += ["--kol", kol]
    return _run_script(args, json_out=False)


def add_prediction(kol: str, pred: str, ptype: str = "点位",
                   target: str = "", direction: str = "", date: str = "") -> str:
    """新增一条预测。"""
    args = ["predict_track.py", "--add", "--kol", kol, "--pred", pred, "--type", ptype]
    if target:
        args += ["--target", target]
    if direction:
        args += ["--dir", direction]
    if date:
        args += ["--date", date]
    return _run_script(args, json_out=False)


# --------------------------------------------------------------------------
# 多KOL对比 / 回测 / 点位
# --------------------------------------------------------------------------

def compare_kols(kols: list = None) -> str:
    """多KOL对比。"""
    args = ["kol_compare.py"]
    if kols:
        args += ["--kol"] + list(kols)
    return _run_script(args, json_out=False)


def backtest(strategy: str = "half") -> str:
    """跟单回测（full=满仓跟 / half=半仓跟）。"""
    return _run_script(["backtest.py", "--strategy", strategy], json_out=False)


def levels(index: str = "", price: float = None) -> str:
    """关键点位监控。"""
    if index and price is not None:
        return _run_script(["level_monitor.py", "--index", index, "--price", str(price)],
                           json_out=False)
    return _run_script(["level_monitor.py", "--list"], json_out=False)


# --------------------------------------------------------------------------
# 行情 / 汇总
# --------------------------------------------------------------------------

def market_summary(period: str = "premarket") -> str:
    """行情汇总（premarket / intraday）。

    走 market_summary.py，其内部已改走 bee_client（支持降级）。
    """
    flag = {"premarket": "--premarket", "intraday": "--intraday"}.get(period)
    args = ["market_summary.py"] + ([flag] if flag else [])
    return _run_script(args, json_out=False)


def quote(text: str, channel: str = "") -> dict:
    """行情查询（经 bee_client，可指定通道 http/local）。

    参数名避免用 `query`，以防与 bee_client.query 混淆。
    """
    try:
        from bee_client import query_item
    except Exception as e:
        raise ServiceError("bee_client 不可用: %s" % e)
    item = query_item(text, channel_name=channel or None)
    if item is None:
        raise ServiceError("未取到行情: %s" % text)
    return item


def bee_health() -> dict:
    """蜜蜂通道健康检查（http/local/mcp）。"""
    try:
        from bee_client import health
    except Exception as e:
        raise ServiceError("bee_client 不可用: %s" % e)
    return health()


# --------------------------------------------------------------------------
# 提醒状态
# --------------------------------------------------------------------------

def alert_status() -> dict:
    """当前告警触发状态 + 已配置提醒。"""
    out = {}
    state = os.path.join(SKILL_DIR, "data", "alert_state.txt")
    try:
        with open(state, encoding="utf-8") as f:
            out["alert_state"] = [l.strip() for l in f if l.strip()]
    except Exception:
        out["alert_state"] = []
    try:
        with open(os.path.join(SKILL_DIR, "data", "price_alerts.json"), encoding="utf-8") as f:
            out["price_alerts"] = json.load(f)
    except Exception:
        out["price_alerts"] = []
    return out


# --------------------------------------------------------------------------
# LLM 能力
# --------------------------------------------------------------------------

def llm_status() -> dict:
    """LLM 配置状态（不发起调用）。"""
    try:
        from llm_client import provider, base_url, model, is_configured, max_tokens
    except Exception as e:
        return {"configured": False, "error": "llm_client 不可用: %s" % e}
    return {"configured": is_configured(), "provider": provider(),
            "base_url": base_url(), "model": model(), "max_tokens": max_tokens()}


#: llm_ask 的整体墙钟预算（秒）。
#
# ⚠️ 为什么需要（结构性风险）
#   各阶段超时是**独立**的，叠加起来会突破上层限制：
#       LLM 生成   LLM_TIMEOUT = 120s
#       上下文构建  AI_ENHANCE_BUDGET = 55s
#       ───────────────────────────────
#       理论最坏 175s，再加读文件/序列化开销 → 逼近甚至超过 nginx 的 180s。
#   一旦超过，nginx 返回 504，用户看到「没数据」，而各阶段自己都「没超时」。
#   故设整体上限 125s，并让生成阶段按「剩余时间」动态收紧。
LLM_ASK_BUDGET = int(os.environ.get("QA_ASK_BUDGET", "125"))


def llm_ask(question: str, context: str = "", auto_context: bool = True) -> dict:
    """调用 LLM 回答问题。

    :param auto_context: 为 True（默认）且未显式提供 context 时，
        自动按问题内容取平台数据（行情/关键位/大V言论）注入，
        避免模型因缺少实时数据而拒答或用过时知识回答。

    整体耗时受 LLM_ASK_BUDGET 约束：先取数、再按剩余时间生成，
    保证「取数 + 生成」之和不超过上层超时（否则会 504）。
    """
    import time as _time
    _t0 = _time.time()

    if not question or not question.strip():
        raise ServiceError("缺少 question")

    ctx = context or ""
    if not ctx and auto_context:
        try:
            ctx = _auto_context(question)
        except Exception:
            ctx = ""          # 取数失败不影响主流程

    try:
        from llm_client import analyze_question, LLMError
    except Exception as e:
        raise ServiceError("llm_client 不可用: %s" % e)

    # 生成超时 = 总预算 − 已用；不足 20s 则直接放弃生成（避免必然超时）
    left = int(LLM_ASK_BUDGET - (_time.time() - _t0))
    if left < 20:
        raise ServiceError("取数阶段耗时过长（%.0fs），已无足够时间生成回答"
                           % (_time.time() - _t0))
    try:
        return {"text": analyze_question(question.strip(), ctx, timeout=left),
                "context_used": bool(ctx),
                "context_len": len(ctx)}
    except LLMError as e:
        raise ServiceError(str(e))


def _auto_context(question: str) -> str:
    """按问题关键词自动取平台数据（复用 qa_analyzer 的逻辑，单一实现）。"""
    try:
        sys.path.insert(0, SCRIPTS)
        import qa_analyzer
        return qa_analyzer.build_context(question)
    except Exception as e:
        return ""


def llm_summarize(content: str, instruction: str = "") -> dict:
    """对给定内容做归纳解读。"""
    if not content or not content.strip():
        raise ServiceError("缺少 content")
    try:
        from llm_client import summarize, LLMError
    except Exception as e:
        raise ServiceError("llm_client 不可用: %s" % e)
    try:
        return {"text": summarize(content, instruction)}
    except LLMError as e:
        raise ServiceError(str(e))


def qa_queue_status() -> dict:
    """群问答队列状态（有多少待处理）。"""
    p = os.path.join(SKILL_DIR, "data", "group_qa_queue.json")
    try:
        with open(p, encoding="utf-8") as f:
            items = json.load(f)
        return {"pending": len(items) if isinstance(items, list) else 0,
                "items": (items or [])[:5] if isinstance(items, list) else []}
    except FileNotFoundError:
        return {"pending": 0, "items": []}
    except Exception as e:
        return {"pending": 0, "error": str(e)[:120]}


# --------------------------------------------------------------------------
# 能力清单（供 MCP/文档自动生成）
# --------------------------------------------------------------------------

def capabilities() -> list:
    """返回平台对外能力清单（name / desc / scopes）。"""
    return [
        {"name": "kol_list", "desc": "列出已收录的大V及记录数", "scopes": ["kol:read"]},
        {"name": "kol_records", "desc": "查询大V言论（近N天/全部/VIP）", "scopes": ["kol:read"]},
        {"name": "kol_summary", "desc": "大V数据概览（总量/VIP/最新仓位/关联资产）", "scopes": ["kol:read"]},
        {"name": "kol_accuracy", "desc": "预测准确率报告", "scopes": ["kol:read"]},
        {"name": "kol_predictions", "desc": "列出/新增预测记录", "scopes": ["kol:read", "kol:write"]},
        {"name": "kol_compare", "desc": "多KOL观点对比", "scopes": ["kol:read"]},
        {"name": "kol_backtest", "desc": "跟单回测", "scopes": ["kol:read"]},
        {"name": "levels", "desc": "关键点位监控（列表/距现价）", "scopes": ["kol:read"]},
        {"name": "market_summary", "desc": "盘前/盘中行情汇总", "scopes": ["market:read"]},
        {"name": "quote", "desc": "行情查询（可指定 http/local 通道）", "scopes": ["market:read"]},
        {"name": "bee_health", "desc": "蜜蜂通道健康检查", "scopes": ["system:read"]},
        {"name": "alert_status", "desc": "提醒与告警状态", "scopes": ["kol:read"]},
        {"name": "llm_ask", "desc": "调用 LLM 回答问题（可附平台数据上下文）", "scopes": ["llm:use"]},
        {"name": "llm_summarize", "desc": "对内容做归纳解读", "scopes": ["llm:use"]},
        {"name": "llm_status", "desc": "LLM 配置状态", "scopes": ["system:read"]},
        {"name": "qa_queue_status", "desc": "群问答队列状态", "scopes": ["kol:read"]},
    ]


# --------------------------------------------------------------------------
# 自诊断（供 MCP 启动自检与 /healthz?deep=1 使用）
# --------------------------------------------------------------------------

def selfcheck() -> dict:
    """逐项体检：解释器 / 脚本目录 / 数据库 / 关键脚本可执行性。

    存在的理由：2026-09-19 的 MCP 全量故障里，**没有任何一处**能看出
    「容器里到底缺了什么」。这个函数把「工具能不能跑」拆成可判定的几项，
    并在 MCP 启动时打印，使同类故障下次能一眼定位。
    """
    checks = {}

    # 1) 解释器
    py = config.python_exe()
    checks["python"] = {"exe": py, "exists": bool(py and os.path.exists(py)),
                        "version": sys.version.split()[0]}

    # 2) 关键脚本是否都在
    needed = ["db_query.py", "db_save.py", "level_monitor.py", "market_summary.py"]
    checks["scripts"] = {
        "dir": SCRIPTS,
        "dir_exists": os.path.isdir(SCRIPTS),
        "missing": [n for n in needed if not os.path.exists(os.path.join(SCRIPTS, n))],
        "count": len(os.listdir(SCRIPTS)) if os.path.isdir(SCRIPTS) else 0,
    }

    # 3) 数据库可读（含关键表是否都在）
    #    ⚠️ 表名以 db_init.py 为准：kol_records / predictions / analysis_reports。
    #    此前误写成 kol_opinions（库文件名），导致自检恒定报红 —— 自检本身
    #    也会说谎，所以这里连"表是否存在"一起查。
    #
    #    ⚠️ 必须用 sqlite3 的**只读 URI**打开（见 api/config.db_uri）：
    #    MCP 容器把 data 挂成只读（start.sh 的 :ro，因该容器不需要写数据），
    #    而 SQLite 默认以读写模式打开、需要建 journal 文件，
    #    在只读文件系统上直接抛 "unable to open database file"。
    #    实测（2026-09-19，服务器）：
    #       普通 connect('/app/data/kol_opinions.db')      → 失败
    #       connect('file:...?mode=ro&immutable=1', uri=True) → 748 行 ✅
    dbp = config.db_path()
    # ⚠️ rows 必须在这里初始化：数据库打不开时会走 except 分支，
    #    而下方 checks["database"] 无条件引用 rows —— 漏了就会
    #    UnboundLocalError，把「原来是数据库坏了」变成「自检自己崩了」。
    db_ok, db_err, tables, missing_tables, rows = False, "", [], [], 0
    needed_tables = ["kol_records", "predictions"]
    try:
        import sqlite3
        con = sqlite3.connect(config.db_uri(), uri=True)
        tables = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")]
        missing_tables = [t for t in needed_tables if t not in tables]
        # 记录数取 kol_records（若缺失则为 0，不抛）
        rows = 0
        if "kol_records" in tables:
            rows = con.execute("select count(*) from kol_records").fetchone()[0]
        con.close()
        db_ok = not missing_tables
    except Exception as e:
        db_err = "%s: %s" % (type(e).__name__, e)
    checks["database"] = {"path": dbp, "readable": db_ok, "error": db_err,
                          "tables": tables, "missing_tables": missing_tables,
                          "records": rows}

    # 4) 依赖（importlib.util 不随 importlib 自动加载，须显式导入）
    #
    # ⚠️ 只检查**平台代码真正 import 的**模块，否则自检会变成噪音源。
    #    本轮踩过：曾把 `requests` 列为必需依赖并报 False，但全仓库检索
    #    scripts/api 与平台脚本**没有任何** import requests 的地方
    #    （bee_client 走 urllib，REST 用 flask）—— 纯属误报，
    #    会让"自检未通过"看起来像故障，反而淹没真实问题。
    import importlib.util

    def _has(mod):
        try:
            return importlib.util.find_spec(mod) is not None
        except Exception:
            return False

    checks["deps"] = {
        "mcp": _has("mcp"),          # MCP 服务端（本进程必需）
        "flask": _has("flask"),      # REST 服务端（同镜像）
        "uvicorn": _has("uvicorn"),  # MCP 的 HTTP 传输（run_http 依赖）
    }

    checks["ok"] = bool(
        checks["python"]["exists"] and checks["scripts"]["dir_exists"]
        and not checks["scripts"]["missing"] and db_ok)
    return checks

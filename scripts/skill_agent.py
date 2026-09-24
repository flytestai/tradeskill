#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 自主规划引擎：让模型自己决定调哪些蜜蜂 skill / MCP，再执行并汇总。

核心目标：**与蜜蜂原生调用完全一致**
--------------------------------------
群里提问拿到的数据，应当与用户在蜜蜂里原生提问时**一模一样** ——
同样的端点、同样的请求体、同样的请求头、同样的完整响应。

因此本模块严格遵守各 skill 的 SKILL.md 契约（实测核对）：

  端点（只有两种，此前误判为三种）
    · /skills/v1/query2data           ← 全部 19 个 hithink-*（含 business/management/
                                          event/futures-query —— 之前我把这三个当成
                                          /query，导致取不到数据）
    · /skills/v1/comprehensive/search ← news-search / announcement-search / report-search
                                          （需 channels + app_id）

  请求头（与 SKILL.md 逐字一致）
    Content-Type / X-Claw-Call-Type / X-Claw-Skill-Id / X-Claw-Skill-Version
    / X-Claw-Plugin-Id / X-Claw-Plugin-Version / X-Claw-Trace-Id(64位)

  请求体
    query2data : {query, page, limit, is_cache, expand_index}
    search     : {channels, app_id, query}

  响应：**完整透传**（遵循网关规范条件六：不得二次解析/清洗/重组）。
        本模块仅在「呈现给 LLM 时」做压缩，原始响应结构不被修改。

不做数量限制
------------
之前有 MAX_SKILLS=6 的限制。现按要求**取消**：模型认为需要多少数据源就调多少，
只保留「总时间预算」作为安全阀（避免单轮跑太久影响下一轮）。

MCP 能力
--------
  · mcp:fetch                 → 抓取网页正文（对应 mcp__bee-mcp__fetch）
  · mcp:list_optional_stocks  → 平台自选股（对应 mcp__bee-mcp__list_optional_stocks）

本地能力
--------
  · local:kol_opinions  本地大V言论库（wu2198）
  · local:levels        关键位监控
  · local:quote_local   公开行情源（腾讯，不依赖蜜蜂，可降级）
  · local:platform_api  本平台 REST/MCP 能力（分析报告/准确率/回测/波浪等）

用法
----
    from skill_agent import plan, build_context
    p   = plan("厦门钨业现在能买吗")           # 查看 AI 规划
    ctx = build_context("厦门钨业现在能买吗")   # 规划 → 执行 → 上下文

CLI
---
    python scripts/skill_agent.py --plan "问题"
    python scripts/skill_agent.py "问题"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from common import service_env
except Exception:
    def service_env(k, d=None):
        return os.environ.get(k, d)

#: 统一格式化层（与 skill_router 共用，消除两套口径）
try:
    from context_format import (
        format_response, format_datas, skill_version, simplify_query,
        cfg as _cfg, cfg_int as _cfg_int,
        FIELDS_PER_ROW, ROWS_PER_SKILL, SUMMARY_MAX,
    )
except Exception:                                   # 极端情况下退化为内置实现
    def skill_version(_sid): return "1.0.0"
    def format_response(resp, skill_id="", rows=None, fields=None, budget=None): return ""
    def format_datas(datas, skill_id="", rows=None, fields=None): return ""
    def simplify_query(q, max_len=16): return ""
    def read_text(_p, _d=""):
        try:
            return open(_p, encoding="utf-8").read()
        except Exception:
            return _d
    def write_text(_p, _t):
        try:
            os.makedirs(os.path.dirname(_p), exist_ok=True)
            open(_p, "w", encoding="utf-8").write(_t)
            return True
        except Exception:
            return False
    def _cfg(_k, d=""): return service_env(_k, d)
    def _cfg_int(_k, d):
        try: return int(_cfg(_k, str(d)) or d)
        except Exception: return d
    FIELDS_PER_ROW, ROWS_PER_SKILL, SUMMARY_MAX = 14, 6, 400

# ⚠️ 文本原子读写来自 safe_json（不是 context_format）—— 实测踩坑：
#    上轮把它们挂到 context_format 的 import 上，导致整个导入失败、
#    静默退化为 4 参数的 format_response stub，调用方传 5 参即 TypeError。
#    故拆成独立 try，且退化实现保留完整签名。
try:
    from safe_json import read_text, write_text
except Exception:
    def read_text(_p, _d=""):
        try:
            return open(_p, encoding="utf-8").read()
        except Exception:
            return _d

    def write_text(_p, _t):
        try:
            os.makedirs(os.path.dirname(_p), exist_ok=True)
            with open(_p, "w", encoding="utf-8") as _f:
                _f.write(_t)
            return True
        except Exception:
            return False

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GATEWAY = _cfg("BEE_GATEWAY_URL", "https://bee-ai.integrity.com.cn").rstrip("/")
EP_Q2D = GATEWAY + "/skills/v1/query2data"
EP_SEARCH = GATEWAY + "/skills/v1/comprehensive/search"
#: 本地波浪技能服务（hithink 风格 query2data 契约，见 scripts/elliott_service.py）。
#: 与网关技能同构：skill_id 进 CATALOG、端点/超时/版本均可配置；服务不可用时回退本地脚本。
ELLIOTT_SERVICE_URL = _cfg("ELLIOTT_SERVICE_URL", "http://127.0.0.1:8022").rstrip("/")

#: 单技能超时（秒）。不再区分快慢 —— 之前把 industry/event 判为"慢"是误判，
#: 实际是端点用错导致的失败重试；统一给 45s 足够。
SKILL_TIMEOUT = _cfg_int("SKILL_TIMEOUT", 45)
#: 整轮总预算（秒）。**不再限制技能数量**，只保留这个安全阀，
#: 防止极端问题拖太久影响下一轮 2 分钟轮询。
TOTAL_BUDGET = _cfg_int("CONTEXT_BUDGET", 600)
#: 交给 LLM 的上下文上限（字符）。响应本身完整保留，此处仅是 prompt 长度控制。
MAX_CTX = _cfg_int("CONTEXT_MAX_CHARS", 24000)
#: 每条数据呈现给 LLM 的字段数上限（原始响应不受影响）
FIELDS_PER_ROW = _cfg_int("CTX_FIELDS_PER_ROW", 14)
ROWS_PER_SKILL = _cfg_int("CTX_ROWS_PER_SKILL", 6)

# ---------------------------------------------------------------------------
# 技能目录：**全部蜜蜂 skill**，交给 AI 自行选择。
#   ep   : q2d | search（只有两种端点）
#   chan : search 端点用的 channels
#   use  : 什么时候用（写给模型看）
# ---------------------------------------------------------------------------
CATALOG = [
    # ---- query2data（19 个）----
    ("hithink-market-query", "q2d", None, "个股/ETF/指数的实时价格、涨跌幅、成交量、主力资金流向、技术指标（MACD等）"),
    ("hithink-zhishu-query", "q2d", None, "大盘指数行情（上证/深证/创业板/科创50/科创综指/恒生/纳斯达克等）"),
    ("hithink-finance-query", "q2d", None, "财务指标：营收、净利润、ROE、毛利率、负债率、现金流、PE/PB 估值"),
    ("hithink-industry-query", "q2d", None, "行业估值、行业财务、盈利、行业行情、板块排名"),
    ("hithink-insresearch-query", "q2d", None, "券商研报评级、目标价、业绩预测、ESG评级、信用评级、基金评级、券商金股"),
    ("hithink-macro-query", "q2d", None, "宏观经济：GDP、CPI、PPI、PMI、利率、汇率、社融、货币供应"),
    ("hithink-basicinfo-query", "q2d", None, "标的基础资料：上市日期、发行价、所属行业、公司简介、费率（全品类）"),
    ("hithink-business-query", "q2d", None, "主营业务构成、主要客户、供应商、参控股公司、股权投资、重大合同"),
    ("hithink-management-query", "q2d", None, "股本结构、股权结构、股东户数、前十大股东/流通股东"),
    ("hithink-event-query", "q2d", None, "个股事件：业绩预告、增发、质押、解禁、机构调研、监管函"),
    ("hithink-etf-selector", "q2d", None, "按行情/跟踪指数/规模/风格/费率筛选 ETF"),
    ("hithink-astock-selector", "q2d", None, "按行情/财务/技术形态条件筛选 A 股个股"),
    ("hithink-hkstock-selector", "q2d", None, "按行情/财务条件筛选港股"),
    ("hithink-usstock-selector", "q2d", None, "按行情/财务条件筛选美股"),
    ("hithink-cb-selector", "q2d", None, "筛选可转债：转股溢价率、正股表现、评级、剩余期限"),
    ("hithink-fund-selector", "q2d", None, "筛选公募基金：类型、业绩、基金经理、风险、持仓、资产配置"),
    ("hithink-futures-query", "q2d", None, "期货/期权行情、波动率、产销、会员持仓、会员榜单"),
    ("hithink-futures-selector", "q2d", None, "按行情/波动率/产销/持仓/行权条件筛选期货期权"),
    ("hithink-sector-selector", "q2d", None, "按行业估值/资金流向/涨跌幅/板块类型筛选行业板块"),
    # ---- comprehensive/search（3 个）----
    ("news-search", "search", ["news"], "财经资讯、政策动态、行业与公司新闻、市场舆情、事件解读"),
    ("announcement-search", "search", ["announcement"], "上市公司公告：定期财报、分红派息、回购增持、资产重组"),
    ("report-search", "search", ["report"], "券商研究报告全文检索"),
    # ---- 本地波浪服务（elliott，端点同 query2data，指向 ELLIOTT_SERVICE_URL）----
    ("hithink-elliott-wave", "elliott", None,
     "艾略特波浪分析：指数当前浪级定位（第几浪/A-B-C结构/C1-C2-C3子浪）、"
     "浪型高低点、斐波那契目标位、失效位；适用上证/深证/创业板/科创50/沪深300/恒生/纳斯达克等"),
]

#: MCP 能力（对应蜜蜂 MCP，服务器侧等价实现）
MCP_CATALOG = [
    ("mcp:fetch", "抓取指定网页 URL 的正文（新闻页/公告页/研报页）"),
    ("mcp:list_optional_stocks", "读取平台自选股列表（代码、名称、市场、涨跌幅）"),
]

#: 本地能力（不依赖蜜蜂网关）
LOCAL_CATALOG = [
    ("local:kol_opinions", "本地大V（wu2198）言论库：最新观点、VIP 专属消息、预测准确率"),
    ("local:levels", "关键位监控：指数/个股的支撑压力位、风控线、止损位"),
    ("local:quote_local", "公开行情源（腾讯直连），完全脱离蜜蜂，可作降级与交叉验证"),
    ("local:platform_api", "本平台已有分析能力：KOL 分析报告、准确率统计、多KOL对比、跟单回测"),
    ("local:elliott_wave", "艾略特波浪分析（elliott-index-wave 技能）：指数当前浪级定位、"
                           "浪型高低点、失效位、备选浪型；交易日/周线双周期确认。"
                           "适用上证指数/深证成指/创业板指/科创50/沪深300/恒生/纳斯达克等"),
    ("local:stock_data", "个股全维度数据（免费公开源直连）：实时行情/涨跌停/换手/PE/PB/流通总市值、日周月K线+60分钟K、财务（营收净利ROE现金流负债率商誉）、主力资金流、龙虎榜、融资融券；适用 A 股个股（尤其新股/次新股等网关覆盖不足的标的）"),
]

#: 已纳入编排的指数（elliott 与关键位能力用）
#: 全部可用能力的 ID 并集（技能 + MCP + 本地）—— 规划器可自由选择
ALL_CAPABILITIES = ({c[0] for c in CATALOG}
                    | {m[0] for m in MCP_CATALOG}
                    | {l[0] for l in LOCAL_CATALOG})

ELLIOTT_INDEXES = ("上证指数", "深证成指", "创业板指", "科创50", "科创综指",
                   "沪深300", "中证500", "北证50", "恒生指数", "纳斯达克")


# ---------------------------------------------------------------------------
# 底层 HTTP —— 严格复刻 SKILL.md 的原生调用
# ---------------------------------------------------------------------------

def _headers(skill_id: str) -> dict:
    """与 SKILL.md 逐字一致的请求头（Trace-Id 必须每次新生成的 64 位十六进制）。

    ⚠️ 版本号**逐技能取**（context_format.skill_version），不再统一硬编码 1.0.0 ——
       `report-search` 自声明 2.0.0，之前被发成 1.0.0，与技能契约不符。
    """
    return {
        "Content-Type": "application/json",
        "X-Claw-Call-Type": "normal",
        "X-Claw-Skill-Id": skill_id,
        "X-Claw-Skill-Version": skill_version(skill_id),
        "X-Claw-Plugin-Id": "none",
        "X-Claw-Plugin-Version": "none",
        "X-Claw-Trace-Id": secrets.token_hex(32),   # 64 字符
    }


def _post(url: str, payload: dict, headers: dict, timeout: int):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def call_skill(skill_id: str, query: str, limit: int = 10, retries: int = 1):
    """按原生契约调用一个蜜蜂技能，返回**完整响应**（dict）。

    调用形态与 SKILL.md 示例完全一致：
      query2data : {query, page, limit, is_cache, expand_index}
      search     : {channels, app_id, query}
    """
    ep, chan = "q2d", None
    for sid, e, c, _ in CATALOG:
        if sid == skill_id:
            ep, chan = e, c
            break

    def _one(q: str):
        if ep == "elliott":
            return _post(ELLIOTT_SERVICE_URL + "/skills/v1/query2data",
                         {"query": q, "page": "1", "limit": str(limit),
                          "is_cache": "1", "expand_index": "true"},
                         _headers(skill_id), SKILL_TIMEOUT)
        if ep == "search":
            return _post(EP_SEARCH,
                         {"channels": chan or ["news"], "app_id": "AIME_SKILL",
                          "query": q},
                         _headers(skill_id), SKILL_TIMEOUT)
        return _post(EP_Q2D,
                     {"query": q, "page": "1", "limit": str(limit),
                      "is_cache": "1", "expand_index": "true"},
                     _headers(skill_id), SKILL_TIMEOUT)

    last = None
    for attempt in range(retries + 1):
        try:
            return _one(query)
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(1.5)

    # 网络层彻底失败 → 返回错误标记（交给上层处理）
    if last is not None:
        return {"_error": "%s: %s" % (type(last).__name__, str(last)[:200])}
    return {"_error": "unknown"}


def call_skill_effective(skill_id: str, query: str, limit: int = 10) -> tuple:
    """调用技能；**取不到数据时用简化问句重试一次**。

    返回 (响应, 实际使用的问句)。简化重试的必要性见
    `context_format.simplify_query` 的说明（长句会让网关 NL→SQL 产出空结果）。
    """
    resp = call_skill(skill_id, query, limit=limit)
    if _datas(resp):
        return resp, query

    short = simplify_query(query)
    if not short or short == query:
        return resp, query

    resp2 = call_skill(skill_id, short, limit=limit)
    if _datas(resp2):
        return resp2, short
    return resp, query                       # 简化也没取到 → 保留原结果


def _datas(resp: dict) -> list:
    """从完整响应中取 datas（不改动原响应）。"""
    if not isinstance(resp, dict):
        return []
    v = resp.get("datas")
    if v is None:
        v = resp.get("data")
    if isinstance(v, dict):
        v = v.get("list") or v.get("items") or []
    return v if isinstance(v, list) else []


# ---------------------------------------------------------------------------
# 阶段①：AI 规划（不给数量上限）
# ---------------------------------------------------------------------------

PLANNER_SYSTEM = (
    "你是数据检索规划助手。用户在财经群提问，你要判断**需要调用哪些数据源**才能完整回答。\n"
    "\n"
    "输出：**只输出 JSON**，格式：\n"
    '{"skills":[{"id":"技能ID","query":"检索问句","why":"理由"}],'
    '"need_fetch":["http://..."],"reason":"整体思路"}\n'
    "\n"
    "规则：\n"
    "1. **不受数量限制** —— 只要对回答问题有帮助的技能都列出来，"
    "该多就多、该少就少；宁全勿缺，但不要列无关的；\n"
    "2. `query` 必须写成**可直接检索的自然语言问句**并带具体标的/指标名，"
    "例如「厦门钨业最新市盈率净资产收益率」而不是「财务」；\n"
    "3. 涉及个股时，考虑同时取：行情、财务、研报、事件、主营、股东等（按问题相关性）；\n"
    "4. 涉及板块/行业时，考虑：行业数据 + 板块资金 + 资讯 + 研报；\n"
    "5. 涉及政策/宏观时，考虑：资讯 + 宏观数据；\n"
    "6. 若需要读取某个网页正文，把 URL 放进 need_fetch；\n"
    "7. 若问题与股票/财经**完全无关**（闲聊、电视剧、生活问题），"
    '返回 {"skills":[],"need_fetch":[],"reason":"非财经问题"}；\n'
    "8. 只能使用下面目录里出现的 ID，不要臆造。"
)


def _catalog_text() -> str:
    lines = ["【可用技能 skill（端点：query2data）】"]
    for sid, ep, chan, use in CATALOG:
        if ep == "q2d":
            lines.append("- %s：%s" % (sid, use))
    lines.append("")
    lines.append("【可用技能 skill（端点：comprehensive/search）】")
    for sid, ep, chan, use in CATALOG:
        if ep == "search":
            lines.append("- %s：%s" % (sid, use))
    lines.append("")
    lines.append("【可用 MCP 能力】")
    for mid, use in MCP_CATALOG:
        lines.append("- %s：%s" % (mid, use))
    lines.append("")
    lines.append("【可用本地能力（无需联网）】")
    for lid, use in LOCAL_CATALOG:
        lines.append("- %s：%s" % (lid, use))
    return "\n".join(lines)


#: 规划缓存（同问题 TTL 内复用，降低 LLM 调用 → 抗限流）
#
#  ⚠️ 默认 **0 = 关闭缓存**。原因：缓存会让同一问题在 TTL 内复用同一套技能清单，
#     而蜜蜂原生提问每次都重新决策 —— 缓存期间的回答可能与首次不同，
#     与「和原生提问保持一致」的目标冲突。
#     若确需抗限流，可显式设 PLAN_CACHE_TTL=600 打开（会有回答漂移的副作用）。
_PLAN_CACHE = {}
PLAN_TTL = _cfg_int("PLAN_CACHE_TTL", 0)


def _cache_get(q):
    if PLAN_TTL <= 0:                     # 缓存关闭
        return None
    hit = _PLAN_CACHE.get(q)
    return hit[1] if hit and time.time() - hit[0] < PLAN_TTL else None


def _cache_put(q, p):
    if PLAN_TTL <= 0:                     # 缓存关闭
        return
    _PLAN_CACHE[q] = (time.time(), p)
    if len(_PLAN_CACHE) > 300:
        for k, _ in sorted(_PLAN_CACHE.items(), key=lambda kv: kv[1][0])[:60]:
            _PLAN_CACHE.pop(k, None)


def plan(question: str) -> dict:
    """让 AI 决定调用哪些技能/MCP（不限制数量）。"""
    cached = _cache_get(question)
    if cached:
        return dict(cached, reason=(cached.get("reason", "") + " [缓存]")[:90])

    try:
        from llm_client import chat, is_configured
    except Exception as e:
        return {"skills": [], "need_fetch": [], "reason": "llm_client 不可用: %s" % e}
    if not is_configured():
        return {"skills": [], "need_fetch": [], "reason": "未配置 LLM_API_KEY"}

    prompt = ("%s\n\n用户问题：%s\n\n请判断需要哪些数据源，按 JSON 格式输出。"
              % (_catalog_text(), question))
    try:
        # 规划是结构化输出任务 → 用快模型（实测 k2.6 约 5s，k3 约 21s）
        out = chat(prompt, system=PLANNER_SYSTEM, purpose="plan",
                   timeout=PLAN_TIMEOUT, retries=1)
    except Exception as e:
        # ⚠️ 规划失败要**可见**：最常见原因是 LLM 组织级 3 RPM 限流，
        #    表现为连续多个问题都返回 0 个技能（静默降级成规则路由）。
        try:
            sys.stderr.write("[plan] 规划失败（将回落规则路由）: %s\n" % str(e)[:150])
        except Exception:
            pass
        return {"skills": [], "need_fetch": [], "reason": "规划失败: %s" % str(e)[:120]}

    m = re.search(r"\{[\s\S]*\}", out or "")
    if not m:
        return {"skills": [], "need_fetch": [], "reason": "规划输出非 JSON"}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {"skills": [], "need_fetch": [], "reason": "规划 JSON 解析失败"}

    # ⚠️ valid 必须包含**全部三类能力**（技能 / MCP / 本地）。
    #    此前只含 CATALOG（22 个蜜蜂技能），导致模型提议的 local:* / mcp:* 被
    #    静默丢弃 —— 实测「创业板指处于第几浪」的规划 reason 明确写
    #    「以本地艾略特波浪分析能力为核心」，但返回的技能里没有任何 local:*，
    #    模型想用却用不了。现修正为三类并集。
    valid = ALL_CAPABILITIES
    skills, seen_ids = [], set()
    for s in (d.get("skills") or []):          # ← 不再切片限制数量
        if not isinstance(s, dict):
            continue
        sid = (s.get("id") or "").strip()
        q = (s.get("query") or "").strip()
        # ⚠️ 必须去重：实测模型会把同一个技能列两次（如 news-search ×2、
        #    macro-query ×2），导致同一数据源被抓两遍 —— 既浪费耗时，
        #    又让「这个技能取到几份数据」变得不可预期。同一技能只保留首条。
        if sid in valid and q and sid not in seen_ids:
            seen_ids.add(sid)
            skills.append({"id": sid, "query": q, "why": (s.get("why") or "")[:40]})

    result = {"skills": skills,
              "need_fetch": [u for u in (d.get("need_fetch") or [])
                             if isinstance(u, str) and u.startswith("http")][:3],
              "reason": (d.get("reason") or "")[:90]}
    _cache_put(question, result)
    return result


# ---------------------------------------------------------------------------
# 阶段②：执行
# ---------------------------------------------------------------------------

def _fmt_rows(resp: dict, skill_id: str, budget: int = None) -> str:
    """把响应压成适合 LLM 阅读的文本 —— **统一走 context_format**。

    ⚠️ 这里只影响「呈现给模型的文本」，**原始响应结构不被修改** ——
       遵循网关规范条件六（透明传递）。完整响应可通过 call_skill() 获取。

    与 skill_router 共用同一实现（FIELDS_PER_ROW 一致），
    因此「AI 规划路径」与「规则回退路径」喂给模型的数据口径完全相同。

    :param budget: 该技能可用的字符预算 → 条目多的技能会多显示，
                   不再一律砍到 ROWS_PER_SKILL 条（那会静默丢弃最多 40% 数据）。
    """
    txt = format_response(resp, skill_id, None, FIELDS_PER_ROW, budget)
    if txt:
        return txt
    err = (resp or {}).get("_error")
    return "（无数据%s）" % ("：" + err[:80] if err else "")


def _do_fetch(url: str) -> str:
    """MCP fetch 的服务器侧等价实现（抓正文）。"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"})
        raw = urllib.request.urlopen(req, timeout=25).read()
        for enc in ("utf-8", "gbk"):
            try:
                html = raw.decode(enc); break
            except Exception:
                continue
        else:
            return ""
        txt = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html)
        txt = re.sub(r"<[^>]+>", " ", txt)
        txt = re.sub(r"&nbsp;|&amp;|&quot;|&#\d+;", " ", txt)
        return re.sub(r"\s+", " ", txt).strip()[:2000]
    except Exception:
        return ""


def _subprocess_out(args, timeout=60) -> str:
    import subprocess
    try:
        r = subprocess.run([sys.executable] + args, capture_output=True, text=True,
                           timeout=timeout, cwd=SKILL_DIR,
                           encoding="utf-8", errors="replace")
        return (r.stdout or r.stderr or "").strip()
    except Exception:
        return ""


def _do_local(kind: str, arg: str) -> str:
    if kind == "local:levels":
        return _subprocess_out(["scripts/level_monitor.py", "--list"], 25)[:1500]
    if kind == "local:kol_opinions":
        out = _subprocess_out(["scripts/db_query.py", "--kol-name", "wu2198",
                               "--days", "3", "--latest", "8", "--json"], 30)
        try:
            recs = json.loads(out)
            if isinstance(recs, list) and recs:
                return "\n".join("%s [%s] %s" % (
                    str(r.get("record_date"))[:16],
                    "VIP" if r.get("is_vip") else "公开",
                    (r.get("content") or "")[:90]) for r in recs[:8])
        except Exception:
            pass
        return ""
    if kind == "local:quote_local":
        return _subprocess_out(["scripts/bee_client.py", "--query", arg[:30],
                                "--channel", "local", "--json"], 30)[:800]
    if kind == "local:platform_api":
        # 本平台已有分析能力（REST 本地）
        try:
            out = _subprocess_out(["scripts/db_query.py", "--kol-name", "wu2198",
                                   "--summary"], 30)[:1500]
            return out
        except Exception:
            return ""
    if kind == "local:elliott_wave":
        return _elliott_context(arg)
    if kind == "local:stock_data":
        return _stock_data_context(arg)
    return ""


# ---------------------------------------------------------------------------
# 个股全维度数据（stock_data_service.py，免费公开源直连）
# ---------------------------------------------------------------------------

def _stock_data_context(question: str) -> str:
    """调用本地股票数据服务（免费公开源），返回格式化文本；失败静默返回空。

    覆盖：实时行情/日K/60分钟K/财务/主力资金流/龙虎榜/融资融券，
    尤其能补齐网关对新股/次新股覆盖不足的缺口。
    """
    m = re.search(r"\b(\d{6})\b", question or "")
    if not m:
        return ""
    out = _subprocess_out(["scripts/stock_data_service.py", "--code", m.group(1),
                           "--kind", "all"], 45)
    return out[:3000] if out else ""


# ---------------------------------------------------------------------------
# 艾略特波浪（elliott-index-wave 技能）
#   该技能是纯标准库实现，自带数据源链（eastmoney → tencent → sina → hithink），
#   已实测服务器容器内三个公开源均可访问，故可在服务器侧直接调用。
# ---------------------------------------------------------------------------

def _elliott_skill_dir() -> str:
    """定位 elliott-index-wave 技能目录。

    优先环境变量 ELLIOTT_SKILL_DIR，其次按蜜蜂技能目录约定查找：
        <...>/skills/kol-opinion-analyzer  →  <...>/skills/elliott-index-wave
    """
    import glob
    env = _cfg("ELLIOTT_SKILL_DIR", "")
    if env and os.path.isdir(env):
        return env
    # 与 kol-opinion-analyzer 同级
    sibling = os.path.join(os.path.dirname(SKILL_DIR), "elliott-index-wave")
    if os.path.isdir(sibling):
        return sibling
    # 兜底：在常见技能根目录下搜
    #   优先仓库内 committed 的 skills/（持久化），其次 deploy 时生成的 vendor/（gitignored）
    for root in (os.path.join(SKILL_DIR, "skills"),
                 os.path.expanduser("~/.bee/plugins/.my-plugin/skills"),
                 "/app/skills", "/app",
                 os.path.join(SKILL_DIR, "vendor")):
        for p in glob.glob(os.path.join(root, "**", "elliott-index-wave"), recursive=True):
            if os.path.isdir(os.path.join(p, "scripts")):
                return p
    return ""


def _pick_index(text: str) -> str:
    """从问句里识别指数名（未识别到时返回空串，由调用方决定默认值）。"""
    q = text or ""
    for idx in ELLIOTT_INDEXES:
        if idx in q:
            return idx
    aliases = {"大盘": "上证指数", "创业板": "创业板指",
               "科创板": "科创综指", "深成指": "深证成指", "纳指": "纳斯达克"}
    for alias, canon in aliases.items():
        if alias in q:
            return canon
    return ""


def _extract_wave_section(md: str, limit: int = 4500) -> str:
    """从波浪报告里取上下文：报告较短时整篇返回；超长时按关键词截取关键段。"""
    if not md:
        return ""
    # 多周期报告（v5）约 2~3KB，直接整篇给模型，信息最全
    if len(md) <= limit:
        return md.strip()
    keys = ("当前浪", "浪级", "结论", "主浪", "备选", "失效", "invalidation",
            "支撑", "压力", "置信", "confidence", "wave",
            "子浪", "C浪", "A浪", "B浪", "斐波那契",
            "年线", "月线", "周线", "日线", "反弹", "下杀", "方向", "目标", "关键位")
    lines = md.splitlines()
    picked, seen = [], 0
    for i, ln in enumerate(lines):
        low = ln.lower()
        if any(k.lower() in low for k in keys):
            # 连同该标题后面的几行一起收
            for j in range(i, min(i + 6, len(lines))):
                if lines[j].strip() and lines[j] not in picked:
                    picked.append(lines[j])
            seen += 1
        if seen >= 16 or len("\n".join(picked)) > limit:
            break
    out = "\n".join(picked).strip()
    return out[:limit] if out else md[:limit]


#: elliott 报告缓存有效期（秒）。默认 6 小时 —— 波浪结构按日线/周线判定，
#: 日内反复重算既无必要也浪费（单次约 2 分钟）。0 = 禁用缓存。
ELLIOTT_CACHE_TTL = _cfg_int("ELLIOTT_CACHE_TTL", 21600)


def _elliott_context(question: str) -> str:
    """调用 elliott-index-wave 生成波浪上下文（失败静默返回空，不影响主流程）。

    带磁盘缓存：同一指数的报告在 TTL 内直接复用（波浪按日/周线判定，
    日内重算无意义，且单次耗时约 2 分钟）。
    """
    sd = _elliott_skill_dir()
    if not sd:
        return ""
    gen = os.path.join(sd, "scripts", "generate_report.py")
    if not os.path.isfile(gen):
        return ""

    idx = _pick_index(question) or "上证指数"
    out_md = os.path.join(SKILL_DIR, "data", "_elliott_ctx_%s.md" % idx)
    cache_txt = os.path.join(SKILL_DIR, "data", "_elliott_ctx_%s.txt" % idx)

    # 命中缓存则直接返回（避免 2 分钟重算）
    if ELLIOTT_CACHE_TTL > 0 and os.path.isfile(cache_txt):
        try:
            if time.time() - os.path.getmtime(cache_txt) < ELLIOTT_CACHE_TTL:
                cached = read_text(cache_txt).strip()
                if cached:
                    return cached
        except Exception:
            pass

    try:
        # generate_report.py 跑完整链路（取数 → 预筛 → markdown 报告），耗时较长
        txt = _subprocess_out(
            [gen, "--index", idx, "--out", out_md], timeout=240)
    except Exception:
        txt = ""

    md = ""
    if os.path.isfile(out_md):
        try:
            with open(out_md, encoding="utf-8") as f:
                md = f.read()
        except Exception:
            md = ""
    if not md:
        md = txt or ""
    result = _extract_wave_section(md)

    # 落缓存（只缓存非空结果，避免把一次失败固化 6 小时）
    if result:
        # 原子写：缓存由子进程写、主进程读，非原子写会读到半截内容
        write_text(cache_txt, result)
    return result


def execute(p: dict, question: str, deadline: float, verbose: bool = False,
            suppress_auto: bool = False) -> tuple:
    """执行规划。返回 (上下文文本, 执行明细)。

    :param suppress_auto: 多轮补取时置 True，避免每轮重复触发本地自动补充项。
    """
    parts, detail = [], []
    planned = p.get("skills") or []

    # ⚠️ 必须按归属分流：规划器现在能返回 local:* / mcp:* 能力（见 ALL_CAPABILITIES），
    #    但只有 CATALOG 里的才是**真的蜜蜂技能**。此前不区分，导致
    #    local:elliott_wave 被当成技能 POST 到网关（白跑一次请求、返回空），
    #    而真正的本地能力又在下面的 LOCAL_CATALOG 循环里跑第二遍 —— 重复且低效。
    REMOTE_IDS = {c[0] for c in CATALOG}
    skills = [x for x in planned if x.get("id") in REMOTE_IDS]
    #: 远程技能里**实际取到数据**的 id —— 用于本地能力降级判定：
    #:   波浪类问题若远程 hithink-elliott-wave 被选但取数失败，则仍回退本地脚本。
    got_remote = set()

    for i, s in enumerate(skills):
        if time.time() > deadline:
            detail.append((s["id"], "跳过(超预算)")); continue
        resp = call_skill(s["id"], s["query"])
        txt = _fmt_rows(resp, s["id"])
        n = len(_datas(resp))
        if n:
            got_remote.add(s["id"])
            parts.append("【%s】%s\n%s" % (s["id"], s["query"], txt))
            detail.append((s["id"], "%d 条" % n))
        else:
            detail.append((s["id"], "无数据"))
        if verbose:
            print("   [%d/%d] %-28s %s" % (i + 1, len(skills), s["id"],
                                           detail[-1][1]), file=sys.stderr)

    for url in p.get("need_fetch") or []:
        if time.time() > deadline:
            break
        txt = _do_fetch(url)
        if txt:
            parts.append("【网页抓取·mcp:fetch】%s\n%s" % (url[:70], txt[:1500]))
            detail.append(("mcp:fetch", "成功"))

    # 本地能力：模型选了就执行；另外按问题特征自动补充（成本极低且常有用）
    #   ⚠️ chosen 必须取**全部规划项**（含 local:/mcp:），不能用过滤后的 skills，
    #      否则模型选中的本地能力会被漏掉。
    chosen = {x["id"] for x in planned}
    q = question or ""
    for lid, _ in LOCAL_CATALOG:
        if lid in chosen:
            # 传问题原文：local:elliott_wave 需要它识别指数名
            txt = _do_local(lid, q)
            if txt:
                parts.append("【%s】\n%s" % (lid, txt))
                detail.append((lid, "OK"))

    if not suppress_auto:
        if any(w in q for w in ("点位", "支撑", "压力", "止损", "关键位", "阻力")) \
                and "local:levels" not in chosen:
            txt = _do_local("local:levels", q)
            if txt:
                parts.append("【关键位】\n" + txt); detail.append(("local:levels", "自动补充"))
        if any(w in q for w in ("大V", "wu2198", "老吴", "言论", "观点", "准确率")) \
                and "local:kol_opinions" not in chosen:
            txt = _do_local("local:kol_opinions", q)
            if txt:
                parts.append("【大V观点】\n" + txt); detail.append(("local:kol_opinions", "自动补充"))

        # 波浪：问句明确提到波浪/浪级时自动补（本地 markdown 报告信息最全，
        #   多周期年/月/周/日 + C1-C5 子浪，故即使远程已取数也一并补上）
        if any(w in q for w in ("波浪", "浪型", "第几浪", "几浪", "艾略特", "elliott",
                                "浪级", "主升浪", "调整浪", "C浪", "C几")) \
                and "local:elliott_wave" not in chosen:
            txt = _elliott_context(q)
            if txt:
                parts.append("【艾略特波浪】\n" + txt); detail.append(("local:elliott_wave", "自动补充"))

        # 个股：问句含 6 位代码时自动补全维度数据（免费公开源直连，新股也能取到）
        if re.findall(r"\b\d{6}\b", q) and "local:stock_data" not in chosen:
            txt = _stock_data_context(q)
            if txt:
                parts.append("【个股数据】\n" + txt); detail.append(("local:stock_data", "自动补充"))

    if "mcp:list_optional_stocks" in chosen:
        txt = _subprocess_out(["scripts/db_query.py", "--list-kols"], 25)
        parts.append("【自选/关注标的】\n%s" % (txt or "(空)"))
        detail.append(("mcp:list_optional_stocks", "OK"))

    ctx = "\n\n".join(parts)
    if len(ctx) > MAX_CTX:
        ctx = ctx[:MAX_CTX] + "\n…（上下文已截断，原始响应完整保留）"
    return ctx, detail


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------

#: 多轮补取：最多轮数（与原生「可反复追问数据」对齐，但保留安全阀）
CONTEXT_MAX_ROUNDS = _cfg_int("CONTEXT_MAX_ROUNDS", 3)
#: 多轮补取：每轮最多追加技能数
CONTEXT_EXTRA_PER_ROUND = _cfg_int("CONTEXT_EXTRA_PER_ROUND", 4)
#: 单次「规划」LLM 调用的超时（秒）。
#  ⚠️ 实测踩坑：原为 90 且 retries=2 → 最坏 90×3=270s，单是规划就吃满了
#     TOTAL_BUDGET(600s)，再叠加技能执行就会超出 REST 层 120s 超时，
#     导致整个请求被截断、context_len=0。
#     另实测规划耗时（3 次平均，提示词 1680 字）：
#        kimi-k2.6 → 21.5s      kimi-k3 → 17.7s
#     故 30s 对 k3 有充足余量；即便超时也有规则路由兜底，不会零数据。
#     并且规则路由已作为兜底先行执行（见 qa_analyzer.build_context），
#     故这里超时的代价只是「少一些增强数据」，不会再出现「零数据」。
# ⚠️ 默认值 30 → 50（2026-09-20，为适配推理模型 glm-5.3）
#
#   历史沿革：
#     · 原注释「实测 k2.6 约 5s，k3 约 21s，故 30s 对 k3 有充足余量」
#       是针对 LLM 的测量。
#     · 2026-09-20 切换到阿里百炼 glm-5.3 后重新实测：
#         规划稳态 11.6~14.7s，**冷启动 25.2s**
#       → 30s 余量过小（冷启动占 84%），一旦超时会**静默回落规则路由**
#         （用户看不出问题，但 AI 规划实际失效）。
#     · 故默认提到 50s。
#
#   ★ 上限约束：**必须 < qa_analyzer.AI_ENHANCE_BUDGET**。
#     该预算是「规划 + 多轮补取」的总时长，PLAN_TIMEOUT 只是其中一段。
#     若 PLAN_TIMEOUT ≥ 该预算，外层会先超时、内层白等，反而更糟。
#
#   2026-09-20 二次调整（按用户要求放宽）：
#     QA_AI_BUDGET      55 → 70s
#     PLAN_TIMEOUT      50 → 60s
#     实测端到端：上下文 56.2s + 生成 6.2s = 62.4s
#     逐层核对：nginx 180s / Flask 120s / 预算 70s / 规划 60s —— 全部放得下 ✅
#     （Flask 的 PLATFORM_SCRIPT_TIMEOUT=120s 是真正的天花板，
#       70s 预算离它还有 50s 安全垫）
PLAN_TIMEOUT = _cfg_int("PLAN_TIMEOUT", 60)
#: 多轮补取时交给模型的「已取数据」节选长度（字符）
COLLECTED_BRIEF = _cfg_int("CONTEXT_COLLECTED_BRIEF", 6000)


def _ask_next_queries(question: str, collected: str, used_ids: set) -> list:
    """把已取到的数据交回模型，问它「还缺什么」→ 返回追加技能清单。

    这是对齐「蜜蜂原生提问可多轮追加取数」的关键：原生 Agent 是边推理边取数、
    发现缺口就再查一次；此前服务器实现是单轮 plan 后冻结上下文，
    一旦首轮没覆盖到就只能靠模型记忆作答。本函数补上这一环。
    """
    try:
        from llm_client import chat, is_configured
    except Exception:
        return []
    if not is_configured():
        return []

    used = "、".join(sorted(used_ids)) or "（无）"
    # ⚠️ 必须在这里就截断：此前把截断放在 prompt 内部（[:6000]），
    #    而调用方传入的是完整上下文（实测可达 3 万+字），
    #    白白构造了一个巨大的字符串再由格式化操作符丢掉，浪费内存与时间。
    _brief = (collected or "")[:COLLECTED_BRIEF]
    prompt = (
        "%s\n\n"
        "【本轮用户问题】\n%s\n\n"
        "【已取到的数据（节选）】\n%s\n\n"
        "【已调用过的数据源】\n%s\n\n"
        "请判断：**上面的数据是否已足够完整回答该问题**。\n"
        "· 若已足够 → 只输出 {\"done\":true}\n"
        "· 若还缺关键数据 → 输出 {\"done\":false,\"skills\":["
        "{\"id\":\"技能ID\",\"query\":\"可直接检索的自然语言问句\",\"why\":\"缺什么\"}]}\n"
        "要求：最多 %d 个；只能从上面的目录里选 ID；不要重复已调用过的数据源；"
        "只输出 JSON。"
        % (_catalog_text(), question, _brief, used, CONTEXT_EXTRA_PER_ROUND)
    )
    try:
        out = chat(prompt, system=PLANNER_SYSTEM, purpose="plan",
                   timeout=PLAN_TIMEOUT, retries=0)
    except Exception:
        return []

    m = re.search(r"\{[\s\S]*\}", out or "")
    if not m:
        return []
    try:
        d = json.loads(m.group(0))
    except Exception:
        return []
    if d.get("done"):
        return []

    valid = ALL_CAPABILITIES
    out_skills, _seen2 = [], set()
    for s in (d.get("skills") or [])[:CONTEXT_EXTRA_PER_ROUND]:
        if not isinstance(s, dict):
            continue
        sid = (s.get("id") or "").strip()
        q = (s.get("query") or "").strip()
        # 只收新增的、目录内合法的技能
        if sid in valid and q and sid not in used_ids and sid not in _seen2:
            _seen2.add(sid)
            out_skills.append({"id": sid, "query": q, "why": (s.get("why") or "")[:40]})
    return out_skills


def build_context(question: str, verbose: bool = False,
                  budget_sec: int = None) -> str:
    """AI 自主规划 → 执行 → （多轮）追加补取 → 返回结构化上下文。

    技能数量不限；轮数由 CONTEXT_MAX_ROUNDS 控制（默认 3）。

    :param budget_sec: 本次取数的**硬性墙钟预算**（秒）。默认取 TOTAL_BUDGET。
        ⚠️ 实测踩坑：规划单次 LLM 调用最坏可到 90s（45s×2），叠加多轮补取后
        整条链路可达 166s，而 REST 层只有 120s 超时 —— 请求被截断、返回零数据。
        故必须由调用方按「上层超时」倒推一个更紧的预算传进来；
        预算耗尽即停止追加取数，用已有数据返回（保证「有数据」优先于「数据全」）。
    """
    q = (question or "").strip()
    if not q:
        return ""
    deadline = time.time() + (budget_sec if budget_sec and budget_sec > 0
                              else TOTAL_BUDGET)
    p = plan(q)
    if verbose:
        print("[规划] %s（%d 个技能）" % (p.get("reason"), len(p.get("skills") or [])),
              file=sys.stderr)
        for s in p.get("skills") or []:
            print("   · %-28s ← %s" % (s["id"], s["query"][:60]), file=sys.stderr)

    ctx, detail = execute(p, q, deadline, verbose)

    # ---- 多轮追加补取：把已有数据交回模型，问它还缺什么 ----
    used_ids = {s["id"] for s in (p.get("skills") or [])}
    for rnd in range(1, CONTEXT_MAX_ROUNDS):
        # 预留 1 轮 LLM + 技能的时间，避免「刚好跨过 deadline」导致返回空白
        if time.time() > deadline or (deadline - time.time()) < 30:
            break
        extra = _ask_next_queries(q, ctx, used_ids)
        if not extra:
            if verbose:
                print("[补取] 第%d轮：模型判断已足够" % rnd, file=sys.stderr)
            break
        if verbose:
            print("[补取] 第%d轮：追加 %d 个技能" % (rnd, len(extra)), file=sys.stderr)
            for s in extra:
                print("   + %-28s ← %s" % (s["id"], s["query"][:60]), file=sys.stderr)

        new_ctx, new_detail = execute({"skills": extra}, q, deadline,
                                      verbose, suppress_auto=True)
        if not new_ctx:
            break                       # 追加的都没取到，再问也会重复，直接停
        ctx = ctx + "\n\n" + new_ctx
        detail += new_detail
        used_ids |= {s["id"] for s in extra}

    if verbose:
        print("[执行汇总] 成功 %d / 共 %d"
              % (sum(1 for _, st in detail if st not in ("无数据",) and "跳过" not in st),
                 len(detail)), file=sys.stderr)
    return ctx


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 自主规划引擎（skill/MCP 编排）")
    ap.add_argument("question", nargs="?", help="用户问题")
    ap.add_argument("--plan", action="store_true", help="只看 AI 规划结果")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not args.question:
        ap.print_help(); return 1

    if args.plan:
        t0 = time.time()
        p = plan(args.question)
        print("问题：%s" % args.question)
        print("AI 规划（%.1fs，%d 个技能）：%s"
              % (time.time() - t0, len(p.get("skills") or []), p.get("reason")))
        for s in p.get("skills") or []:
            print("  · %-28s query=%s" % (s["id"], s["query"][:58]))
        for u in p.get("need_fetch") or []:
            print("  · 抓取：%s" % u[:70])
        return 0

    t0 = time.time()
    ctx = build_context(args.question, verbose=not args.json)
    if args.json:
        print(json.dumps({"question": args.question,
                          "elapsed": round(time.time() - t0, 1),
                          "context": ctx}, ensure_ascii=False, indent=1))
    else:
        print("-" * 70)
        print("耗时 %.1fs，上下文 %d 字" % (time.time() - t0, len(ctx)))
        print(ctx or "(无数据)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

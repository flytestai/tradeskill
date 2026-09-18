#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 自主规划引擎：让模型自己决定「调哪些蜜蜂 skill / MCP」，再执行并汇总。

与 skill_router 的区别（为什么重写）
------------------------------------
`skill_router` 用**关键词硬编码**决定调什么：
    if "估值" in q: 调 finance;  if "研报" in q: 调 research ...

问题：提问方式千变万化，关键词永远列不全 ——
    「这股贵不贵」要 finance、「这公司靠不靠谱」要 finance+management、
    「最近有啥消息」要 news、「板块轮动到哪了」要 industry+sector ...
硬编码只能覆盖已想到的说法，**没覆盖到的问句就拿不到数据，AI 只能空谈**。

本模块改为**两阶段 AI 驱动**：
    阶段①（规划）：把「全部可用技能目录」交给模型 → 模型输出要调用的技能清单(JSON)
    阶段②（执行）：按清单真实调用，聚合数据
    阶段③（汇总）：把数据交回模型 → 生成最终回答

即：**根据问题自行分析该调哪些 skill / MCP**，而非查表。

技能目录（三套端点，实测确认）
------------------------------
  · /skills/v1/query2data         —— 全部 hithink-*（行情/财务/行业/研报/宏观/选择器）
  · /skills/v1/query              —— business / management / event
  · /skills/v1/comprehensive/search —— news / announcement / report（需 channels + app_id）

MCP 能力（服务器侧等价实现）
---------------------------
  · fetch（网页抓取）        → 用 urllib 抓取正文（对应 mcp__bee-mcp__fetch）
  · list_optional_stocks（自选股）→ 本地 data/watchlist.json（平台自选）

用法
----
    from skill_agent import plan, build_context

    plan_ = plan("厦门钨业现在能买吗")      # 仅看 AI 规划了什么（调试）
    ctx   = build_context("厦门钨业现在能买吗")   # 完整：规划 → 执行 → 上下文

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

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GATEWAY = os.environ.get("BEE_GATEWAY_URL", "https://bee-ai.integrity.com.cn")
EP_Q2D = GATEWAY + "/skills/v1/query2data"
EP_QUERY = GATEWAY + "/skills/v1/query"
EP_SEARCH = GATEWAY + "/skills/v1/comprehensive/search"

DEFAULT_TIMEOUT = int(os.environ.get("SKILL_TIMEOUT", "30"))
SLOW_TIMEOUT = int(os.environ.get("SKILL_TIMEOUT_SLOW", "45"))
TOTAL_BUDGET = int(os.environ.get("CONTEXT_BUDGET", "100"))
MAX_CTX = int(os.environ.get("CONTEXT_MAX_CHARS", "7000"))
MAX_SKILLS = int(os.environ.get("MAX_SKILLS_PER_QUERY", "6"))

# ---------------------------------------------------------------------------
# 技能目录：交给 AI 让它自己选。
#   ep  : 端点类型  q2d | query | search
#   use : 什么时候用（写给模型看的）
# ---------------------------------------------------------------------------
CATALOG = [
    # ---- query2data 端点 ----
    ("hithink-market-query", "q2d", "个股/ETF/指数的实时价格、涨跌幅、成交量、主力资金、技术指标"),
    ("hithink-zhishu-query", "q2d", "大盘指数行情（上证/深证/创业板/科创50/恒生/纳斯达克）"),
    ("hithink-finance-query", "q2d", "财务报表指标：营收、净利、ROE、毛利率、负债率、PE/PB 估值"),
    ("hithink-industry-query", "q2d", "行业估值、盈利、板块排名、行业行情"),
    ("hithink-insresearch-query", "q2d", "券商研报评级、目标价、业绩预测、ESG/信用评级、金股"),
    ("hithink-macro-query", "q2d", "宏观：GDP、CPI、PPI、PMI、利率、汇率、社融"),
    ("hithink-etf-selector", "q2d", "按条件筛选 ETF（跟踪指数、规模、风格、费率）"),
    ("hithink-astock-selector", "q2d", "按行情/财务/技术形态条件筛选 A 股个股"),
    ("hithink-hkstock-selector", "q2d", "筛选港股"),
    ("hithink-usstock-selector", "q2d", "筛选美股"),
    ("hithink-cb-selector", "q2d", "筛选可转债（转股溢价率、评级、剩余期限）"),
    ("hithink-fund-selector", "q2d", "筛选公募基金（类型、业绩、经理、风险）"),
    ("hithink-futures-selector", "q2d", "筛选期货/期权（波动率、持仓、产销）"),
    ("hithink-sector-selector", "q2d", "按资金流向/估值/涨跌幅筛选行业板块"),
    ("hithink-basicinfo-query", "q2d", "标的基础资料：上市日期、发行价、所属行业、公司简介"),
    # ---- /query 端点（注意：与上面不是同一端点）----
    ("hithink-business-query", "query", "主营业务构成、主要客户、供应商、参控股公司、重大合同"),
    ("hithink-management-query", "query", "股本结构、股权结构、股东户数、前十大股东"),
    ("hithink-event-query", "query", "业绩预告、增发、质押、解禁、机构调研、监管函"),
    # ---- /comprehensive/search 端点 ----
    ("news-search", "search", "财经资讯、政策动态、行业与公司新闻、市场舆情"),
    ("announcement-search", "search", "上市公司公告（定期报告、分红、回购、重组）"),
    ("report-search", "search", "券商研究报告全文检索"),
]

#: MCP 能力（服务器侧等价实现）
MCP_CATALOG = [
    ("mcp:fetch", "抓取指定网页正文（新闻链接、公告页、研报页）"),
    ("mcp:list_optional_stocks", "读取本平台自选股列表（代码/名称/涨跌幅）"),
]

#: 本地能力（不依赖蜜蜂网关）
LOCAL_CATALOG = [
    ("local:kol_opinions", "本地大V（wu2198）言论库：最新观点、VIP 消息、历史准确率"),
    ("local:levels", "本地关键位监控：指数支撑压力位、风控线"),
    ("local:quote_local", "本地行情源（腾讯直连，不依赖蜜蜂，可作降级）"),
]


# ---------------------------------------------------------------------------
# 底层 HTTP
# ---------------------------------------------------------------------------

def _headers(skill_id: str) -> dict:
    import secrets
    return {"Content-Type": "application/json", "X-Claw-Call-Type": "normal",
            "X-Claw-Skill-Id": skill_id, "X-Claw-Skill-Version": "1.0.0",
            "X-Claw-Plugin-Id": "none", "X-Claw-Plugin-Version": "none",
            "X-Claw-Trace-Id": secrets.token_hex(32)}


def _post(url: str, payload: dict, headers: dict, timeout: int):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _call_skill(skill_id: str, ep: str, query: str, limit: int = 5):
    """按端点类型调用技能，返回 datas 列表（失败返回 []）。"""
    try:
        if ep == "q2d":
            d = _post(EP_Q2D, {"query": query, "page": "1", "limit": str(limit),
                               "is_cache": "1", "expand_index": "true"},
                      _headers(skill_id), DEFAULT_TIMEOUT)
            return d.get("datas") or []
        if ep == "query":
            d = _post(EP_QUERY, {"query": query, "page": "1", "limit": str(limit),
                                 "is_cache": "1"},
                      _headers(skill_id), SLOW_TIMEOUT)
            return d.get("datas") or []
        if ep == "search":
            chan = {"news-search": ["news"],
                    "announcement-search": ["announcement"],
                    "report-search": ["report"]}.get(skill_id, ["news"])
            d = _post(EP_SEARCH, {"channels": chan, "app_id": "AIME_SKILL",
                                  "query": query},
                      _headers(skill_id), SLOW_TIMEOUT)
            items = d.get("datas") or d.get("data") or []
            if isinstance(items, dict):
                items = items.get("list") or []
            return items
    except Exception:
        return []
    return []


# ---------------------------------------------------------------------------
# 阶段①：AI 规划 —— 让它自己决定调哪些技能
# ---------------------------------------------------------------------------

PLANNER_SYSTEM = (
    "你是数据检索规划助手。用户会在财经群里提问，你要判断**需要哪些数据源**才能回答好。\n"
    "\n"
    "输出要求：**只输出 JSON**，格式：\n"
    '{"skills":[{"id":"技能ID","query":"检索问句","why":"一句话理由"}],"need_fetch":[],'
    '"reason":"整体思路"}\n'
    "\n"
    "规则：\n"
    "1. 最多选 %d 个技能，**宁缺毋滥**——只选真正能提供所需数据的；\n"
    "2. `query` 要写成**能直接检索的自然语言问句**，并带上具体标的/指标名"
    "（如「厦门钨业最新市盈率净资产收益率」而不是「财务」）；\n"
    "3. 若问题与股票/财经**完全无关**（如闲聊、电视剧、生活问题），"
    '返回 {"skills":[],"need_fetch":[],"reason":"非财经问题"}；\n'
    "4. 若需要抓取网页正文，把 URL 放进 need_fetch（没有就留空数组）；\n"
    "5. 不要选与你判断无关的技能，不要臆造不存在的技能 ID。"
)


def _catalog_text() -> str:
    lines = ["【可调用技能（skill）】"]
    for sid, ep, use in CATALOG:
        lines.append("- %s：%s" % (sid, use))
    lines.append("")
    lines.append("【可调用 MCP 能力】")
    for mid, use in MCP_CATALOG:
        lines.append("- %s：%s" % (mid, use))
    lines.append("")
    lines.append("【可调用本地能力（无需联网）】")
    for lid, use in LOCAL_CATALOG:
        lines.append("- %s：%s" % (lid, use))
    return "\n".join(lines)


#: 规划结果缓存（问题 → (时间戳, 规划)）。
#  为什么需要：Moonshot 有组织级限流（实测 429），规划是纯 LLM 调用。
#  同一问题在 TTL 内复用规划结果，可显著减少调用次数。
#  TTL 默认 600s：群里的提问常有重复/追问，且规划结果与时点无关（只是选数据源）。
_PLAN_CACHE = {}
PLAN_TTL = int(os.environ.get("PLAN_CACHE_TTL", "600"))


def _cache_get(q: str):
    hit = _PLAN_CACHE.get(q)
    if hit and time.time() - hit[0] < PLAN_TTL:
        return hit[1]
    return None


def _cache_put(q: str, p: dict):
    _PLAN_CACHE[q] = (time.time(), p)
    if len(_PLAN_CACHE) > 200:          # 简单容量控制
        oldest = sorted(_PLAN_CACHE.items(), key=lambda kv: kv[1][0])[:50]
        for k, _ in oldest:
            _PLAN_CACHE.pop(k, None)


def plan(question: str) -> dict:
    """让 AI 决定要调用哪些技能/MCP。返回 {skills, need_fetch, reason}。"""
    cached = _cache_get(question)
    if cached:
        return dict(cached, reason=(cached.get("reason", "") + " [缓存]")[:80])

    try:
        from llm_client import chat, is_configured, LLMError
    except Exception as e:
        return {"skills": [], "need_fetch": [], "reason": "llm_client 不可用: %s" % e}
    if not is_configured():
        return {"skills": [], "need_fetch": [], "reason": "未配置 LLM_API_KEY"}

    prompt = (
        "%s\n\n"
        "用户问题：%s\n\n"
        "请判断需要哪些数据源，按 JSON 格式输出。"
        % (_catalog_text(), question)
    )
    try:
        # 规划是结构化输出任务 → 用快模型（实测 kimi-k2.6 约 5s，kimi-k3 需 21s）
        out = chat(prompt, system=PLANNER_SYSTEM % MAX_SKILLS,
                   purpose="plan", timeout=60, retries=1)
    except Exception as e:
        return {"skills": [], "need_fetch": [], "reason": "规划失败: %s" % str(e)[:120]}

    # 解析 JSON（容忍模型包裹 ```json 或多余文字）
    m = re.search(r"\{[\s\S]*\}", out or "")
    if not m:
        return {"skills": [], "need_fetch": [], "reason": "规划输出非 JSON"}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {"skills": [], "need_fetch": [], "reason": "规划 JSON 解析失败"}

    valid_ids = {c[0] for c in CATALOG}
    skills = []
    for s in (d.get("skills") or [])[:MAX_SKILLS]:
        if not isinstance(s, dict):
            continue
        sid = (s.get("id") or "").strip()
        q = (s.get("query") or "").strip()
        if sid in valid_ids and q:
            skills.append({"id": sid, "query": q, "why": (s.get("why") or "")[:40]})
    result = {"skills": skills,
              "need_fetch": [u for u in (d.get("need_fetch") or []) if isinstance(u, str)][:2],
              "reason": (d.get("reason") or "")[:80]}
    _cache_put(question, result)
    return result


# ---------------------------------------------------------------------------
# 阶段②：执行 —— 调技能 / MCP / 本地能力
# ---------------------------------------------------------------------------

_EP = {sid: ep for sid, ep, _ in CATALOG}


def _fmt_rows(datas, skill_id: str, limit: int = 4) -> str:
    """把技能返回的 datas 压成简洁文本（控制上下文长度）。"""
    if not datas:
        return ""
    out = []
    for d in datas[:limit]:
        if not isinstance(d, dict):
            continue
        if skill_id in ("news-search", "announcement-search", "report-search"):
            t = d.get("title") or d.get("标题") or ""
            c = (d.get("content") or d.get("摘要") or "")[:260]
            if t:
                out.append("- %s%s" % (t[:70], ("：" + c) if c else ""))
            continue
        # 通用：取前 7 个非空字段
        items = []
        for k, v in list(d.items())[:9]:
            if v in (None, "", "-"):
                continue
            items.append("%s=%s" % (k, str(v)[:38]))
        if items:
            out.append("- " + "，".join(items))
    return "\n".join(out)


def _do_fetch(url: str) -> str:
    """MCP fetch 的等价实现：抓网页正文。"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"})
        raw = urllib.request.urlopen(req, timeout=20).read()
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
        return re.sub(r"\s+", " ", txt).strip()[:1200]
    except Exception:
        return ""


def _do_local(kind: str, question: str) -> str:
    import subprocess
    def run(args, t=25):
        try:
            r = subprocess.run([sys.executable] + args, capture_output=True, text=True,
                               timeout=t, cwd=SKILL_DIR, encoding="utf-8", errors="replace")
            return (r.stdout or r.stderr or "").strip()
        except Exception:
            return ""
    if kind == "local:levels":
        return run(["scripts/level_monitor.py", "--list"], 20)[:700]
    if kind == "local:kol_opinions":
        out = run(["scripts/db_query.py", "--kol-name", "wu2198", "--days", "3",
                   "--latest", "5", "--json"], 25)
        try:
            recs = json.loads(out)
            if isinstance(recs, list) and recs:
                return "\n".join("%s %s" % (str(r.get("record_date"))[:16],
                                            (r.get("content") or "")[:70]) for r in recs[:5])
        except Exception:
            pass
        return ""
    if kind == "local:quote_local":
        out = run(["scripts/bee_client.py", "--query", question[:20],
                   "--channel", "local", "--json"], 30)
        return out[:400]
    return ""


def execute(plan_: dict, question: str, deadline: float) -> tuple:
    """执行规划。返回 (上下文文本, 执行明细)。"""
    parts, detail = [], []

    for s in plan_.get("skills", []):
        if time.time() > deadline:
            detail.append((s["id"], "跳过(超预算)")); continue
        datas = _call_skill(s["id"], _EP.get(s["id"], "q2d"), s["query"])
        txt = _fmt_rows(datas, s["id"])
        if txt:
            parts.append("【%s】%s\n%s" % (s["id"], s["query"][:40], txt))
            detail.append((s["id"], "%d 条" % len(datas)))
        else:
            detail.append((s["id"], "无数据"))

    for url in plan_.get("need_fetch", [])[:2]:
        if time.time() > deadline:
            break
        txt = _do_fetch(url)
        if txt:
            parts.append("【网页抓取】%s\n%s" % (url[:60], txt[:800]))
            detail.append(("mcp:fetch", "成功"))

    # 本地能力（AI 未选也补关键位/大V，成本极低且常有用）
    q = question or ""
    if any(w in q for w in ("点位", "支撑", "压力", "止损", "关键位")):
        txt = _do_local("local:levels", q)
        if txt:
            parts.append("【关键位】\n" + txt)
            detail.append(("local:levels", "OK"))
    if any(w in q for w in ("大V", "wu2198", "老吴", "言论", "观点")):
        txt = _do_local("local:kol_opinions", q)
        if txt:
            parts.append("【大V观点】\n" + txt)
            detail.append(("local:kol_opinions", "OK"))

    ctx = "\n\n".join(parts)
    if len(ctx) > MAX_CTX:
        ctx = ctx[:MAX_CTX] + "\n…（已截断）"
    return ctx, detail


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------

def build_context(question: str, verbose: bool = False) -> str:
    """AI 自主规划 → 执行 → 返回结构化上下文。"""
    q = (question or "").strip()
    if not q:
        return ""
    deadline = time.time() + TOTAL_BUDGET
    p = plan(q)
    if verbose:
        print("[规划] %s" % p.get("reason"), file=sys.stderr)
        for s in p.get("skills", []):
            print("   · %s ← %s" % (s["id"], s["query"]), file=sys.stderr)
    ctx, detail = execute(p, q, deadline)
    if verbose:
        for name, st in detail:
            print("[执行] %-28s %s" % (name, st), file=sys.stderr)
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
        print("AI 规划（%.1fs）：%s" % (time.time() - t0, p.get("reason")))
        for s in p.get("skills", []):
            print("  · %-28s query=%s" % (s["id"], s["query"][:52]))
        if p.get("need_fetch"):
            print("  · 抓取：%s" % p["need_fetch"])
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

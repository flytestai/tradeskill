#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蜜蜂技能整合层：按问题意图路由到多个技能，聚合出结构化上下文。

设计目标
--------
群问答的回答质量取决于「喂给 AI 什么数据」。原先 `qa_analyzer.build_context`
只覆盖指数/个股/关键位/大V 四类，很多问题拿不到数据，AI 只能泛泛而谈。

本模块把**蜜蜂的全部技能**按意图编排，一次提问可并发（顺序）取多个数据源：

    问题 ──► 意图识别 ──► 技能编排 ──► 聚合上下文 ──► 交 AI 分析 → 回复
               │            │
               │            ├─ hithink-zhishu-query      指数行情
               │            ├─ hithink-market-query      个股行情
               │            ├─ hithink-finance-query     财务/估值
               │            ├─ hithink-industry-query    行业板块
               │            ├─ hithink-insresearch-query 研报评级
               │            ├─ hithink-macro-query       宏观数据
               │            ├─ hithink-etf-selector      ETF 筛选
               │            ├─ hithink-basicinfo-query   标的基础信息
               │            └─ news-search / announcement-search（search 端点）
               └─ 本地：KOL 言论库 / 关键位 / 平台行情（local 通道，不依赖蜜蜂）

关键约束
--------
1. **顺序执行**：目标容器无法创建线程（宿主内核限制），且对端网关有 QPS 限制，
   故不使用并发；每个技能设独立超时，慢的不会拖垮整体。
2. **容错**：任一技能失败只是少一块数据，不影响其余，也不阻断回复。
3. **预算控制**：总耗时与上下文长度都有上限，避免拖到下一轮轮询还在跑。
4. **单一实现**：qa_analyzer 的上下文构建统一走这里，不再各写一份。

用法
----
    from skill_router import build_context, route

    ctx = build_context("厦门钨业现在能买吗")     # 结构化上下文字符串
    hits = route("半导体行业怎么样")              # 仅看路由结果（调试）

CLI
---
    python scripts/skill_router.py "厦门钨业能买吗"
    python scripts/skill_router.py --route "半导体行业"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from bee_client import query as bee_query, SKILL_IDS
except Exception:
    bee_query, SKILL_IDS = None, {}

try:
    from common import service_env
except Exception:
    def service_env(k, d=None):
        return os.environ.get(k, d)

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 单个技能的调用超时（秒）；慢技能（如 industry/event）给多一点
DEFAULT_TIMEOUT = int(os.environ.get("SKILL_TIMEOUT", "25"))
SLOW_TIMEOUT = int(os.environ.get("SKILL_TIMEOUT_SLOW", "40"))
#: 整个上下文构建的总预算（秒）；超时后放弃剩余技能，用已有数据回答
TOTAL_BUDGET = int(os.environ.get("CONTEXT_BUDGET", "600"))
#: 上下文最大字符数（防止 prompt 过长）
#  ⚠️ 与 skill_agent 保持**同一上限**：此前这里是 6000、skill_agent 是 24000，
#     导致「规则回退路径」比「AI 规划路径」少 4 倍上下文，同一问题答案质量漂移。
MAX_CTX = int(os.environ.get("CONTEXT_MAX_CHARS", "24000"))

COMPREHENSIVE_URL = "https://bee-ai.integrity.com.cn/skills/v1/comprehensive/search"

#: 已知标的（用于在无 6 位代码时也识别出个股）
KNOWN_STOCKS = {
    "厦门钨业": "600549", "贵州茅台": "600519", "宁德时代": "300750",
    "中芯国际": "688981", "北方华创": "002371", "浙江龙盛": "600352",
    "亿纬锂能": "300014", "特锐德": "300001", "华友钴业": "603799",
    "寒锐钴业": "300618", "富瀚微": "300613", "东方财富": "300059",
}
INDEXES = ["上证指数", "深证成指", "创业板指", "科创50", "科创综指",
           "沪深300", "中证500", "北证50", "恒生指数", "纳斯达克"]
INDEX_ALIAS = {"大盘": "上证指数", "科创板": "科创综指", "创业板": "创业板指"}

_CODE_RE = re.compile(r"\b(\d{6})\b")


# --------------------------------------------------------------------------
# 底层调用
# --------------------------------------------------------------------------

def _call(skill_id: str, text: str, limit: int = 5, timeout: int = None,
          channel: str = "http"):
    """调用一个蜜蜂技能，返回 datas 列表（失败返回 []）。"""
    if bee_query is None:
        return []
    try:
        d = bee_query(text, skill_id=skill_id, limit=limit, channel_name=channel)
        return (d or {}).get("datas") or []
    except Exception:
        # 慢技能用更长超时重试一次
        if timeout and timeout > DEFAULT_TIMEOUT:
            try:
                d = bee_query(text, skill_id=skill_id, limit=limit, channel_name=channel)
                return (d or {}).get("datas") or []
            except Exception:
                return []
        return []


def _search_news(query: str, limit: int = 5):
    """调用资讯搜索端点（news-search 走 /comprehensive/search 而非 query2data）。"""
    import secrets
    body = json.dumps({"channels": ["news"], "app_id": "AIME_SKILL",
                       "query": query}, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json", "X-Claw-Call-Type": "normal",
         "X-Claw-Skill-Id": "news-search", "X-Claw-Skill-Version": "1.0.0",
         "X-Claw-Plugin-Id": "none", "X-Claw-Plugin-Version": "none",
         "X-Claw-Trace-Id": secrets.token_hex(32)}
    try:
        req = urllib.request.Request(COMPREHENSIVE_URL, data=body, headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=SLOW_TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8"))
        items = d.get("datas") or d.get("data") or []
        if isinstance(items, dict):
            items = items.get("list") or []
        out = []
        for it in items[:limit]:
            if isinstance(it, dict):
                out.append({"title": it.get("title") or it.get("标题") or "",
                            "content": (it.get("content") or it.get("摘要") or "")[:300],
                            "time": it.get("publish_time") or it.get("time") or ""})
        return out
    except Exception:
        return []


# --------------------------------------------------------------------------
# 意图识别与技能编排
# --------------------------------------------------------------------------

def _find_indexes(q: str) -> list:
    found = [i for i in INDEXES if i in q]
    for alias, canon in INDEX_ALIAS.items():
        if alias in q and canon not in found:
            found.append(canon)
    return found[:3]


def _find_stocks(q: str) -> list:
    out = []
    for code in _CODE_RE.findall(q)[:3]:
        out.append((code, code))
    for name, code in KNOWN_STOCKS.items():
        if name in q:
            out.append((name, code))
    # 去重（按代码）
    seen, uniq = set(), []
    for n, c in out:
        if c not in seen:
            seen.add(c); uniq.append((n, c))
    return uniq[:3]


def route(question: str) -> list:
    """解析问题意图 → 返回待调用的技能清单。

    返回 [(label, kind, payload), ...]
      kind = "index" | "stock" | "finance" | "industry" | "research"
             | "macro" | "etf" | "basic" | "news" | "kol" | "level"
    """
    q = (question or "").strip()
    plan = []

    # ---- 本地能力（不依赖蜜蜂，优先）----
    if any(w in q for w in ("点位", "支撑", "压力", "止损", "关键位", "阻力")):
        for idx in (_find_indexes(q) or ["创业板指"]):
            plan.append(("关键位·" + idx, "level", idx))
    if any(w in q for w in ("大V", "wu2198", "老吴", "观点", "言论", "怎么看", "准确率")):
        plan.append(("大V观点", "kol", "wu2198"))

    # ---- 指数行情 ----
    idxs = _find_indexes(q)
    for idx in idxs:
        plan.append(("指数·" + idx, "index", idx))

    # ---- 个股 ----
    stocks = _find_stocks(q)
    # 是否属于「要不要买」这类决策问题 —— 这类问题除了行情/财务，
    # 还必须给**研报评级与盈利预测**（对决策最相关），否则 AI 只能给空框架。
    is_decision = any(w in q for w in
                      ("能买吗", "能不能买", "可以买", "该买", "值得买", "要买",
                       "能建仓", "可以建仓", "值得", "靠谱吗", "怎么样这只",
                       "能不能上车", "上车", "抄底", "追高", "止盈", "止损"))
    for name, code in stocks:
        plan.append(("行情·" + name, "stock", (name, code)))
        if is_decision or any(w in q for w in
                              ("估值", "市盈率", "PE", "PB", "财务", "业绩",
                               "营收", "净利", "ROE", "基本面", "贵不贵")):
            plan.append(("财务·" + name, "finance", (name, code)))
        if is_decision or any(w in q for w in
                              ("研报", "评级", "目标价", "机构", "券商", "预测")):
            plan.append(("研报·" + name, "research", name))
        if any(w in q for w in ("上市", "发行价", "主营", "公司", "做什么")):
            plan.append(("资料·" + name, "basic", name))

    # ---- 行业/板块 ----
    if any(w in q for w in ("行业", "板块", "赛道", "产业链")):
        kw = _extract_topic(q)
        if kw:
            plan.append(("行业·" + kw, "industry", kw))

    # ---- 宏观 ----
    if any(w in q for w in ("宏观", "GDP", "CPI", "PPI", "PMI", "社融",
                            "利率", "降息", "加息", "流动性", "经济")):
        plan.append(("宏观数据", "macro", _extract_topic(q) or "最新经济数据"))

    # ---- ETF ----
    if "ETF" in q.upper() or "etf" in q:
        plan.append(("ETF筛选", "etf", q))

    # ---- 资讯（政策/事件/公司新闻）----
    if any(w in q for w in ("新闻", "政策", "消息", "事件", "利好", "利空",
                            "最新", "怎么回事", "为什么", "投资", "出品", "并购",
                            "重组", "解禁", "减持", "增持")):
        plan.append(("资讯·" + _extract_topic(q)[:12], "news", _extract_topic(q) or q))

    # ---- 兜底：纯泛问且什么都没识别到 → 给指数+资讯 ----
    if not plan:
        plan.append(("资讯", "news", q))
        plan.append(("指数·上证指数", "index", "上证指数"))

    # 去重（按 label）
    seen, uniq = set(), []
    for p in plan:
        if p[0] not in seen:
            seen.add(p[0]); uniq.append(p)
    return uniq[:8]          # 上限 8 个，控制耗时


def _extract_topic(q: str) -> str:
    """从问句里抽出「主题词」（用于行业/资讯检索）。

    ⚠️ 顺序很关键：长词必须先替换，否则短词会先把长词拆碎。
       实测踩坑：先替换「怎么」后，「怎么样」变成「样」，
       导致 "半导体行业最近怎么样" → "半导体行业 样"（残留单字）。
    """
    stop = [
        # ---- 长词优先（必须排在短词前）----
        "怎么样", "为什么", "是什么", "怎么", "如何", "什么",
        "选哪个", "哪家", "哪些", "多少", "应该", "请问", "帮我", "一下",
        # ---- 短词 ----
        "的", "了", "吗", "呢", "能", "买", "卖", "现在", "目前",
        "最近", "最新", "股票", "标的", "企业", "投资", "还有", "以及",
        "行业", "板块", "情况", "走势", "今天", "明天",
        # ---- 事务性/泛义名词（留着会污染检索词，如"新能源板块投资机会"→"机会"）----
        "机会", "空间", "方向", "前景", "分析", "看法", "观点", "建议",
        "表现", "排名", "对比", "区别", "影响", "原因", "逻辑", "价值",
    ]
    t = q
    for s in stop:
        t = t.replace(s, " ")
    # 清理标点与残留的孤立单字（中文单字基本都是语气词残留）
    t = re.sub(r"[，。？！,.?!、：:；;（）()\[\]\s]+", " ", t).strip()
    kept = [w for w in t.split() if len(w) >= 2]
    if not kept:
        return q[:20]      # 全是噪声 → 退回原句，避免检索词为空
    return " ".join(kept)[:24]


# --------------------------------------------------------------------------
# 执行技能 → 拼装上下文
# --------------------------------------------------------------------------

#: 统一格式化层（与 skill_agent 共用，消除两套口径）
try:
    from context_format import format_datas as _fmt_datas, FIELDS_PER_ROW as _FIELDS
except Exception:                                       # 退化：内置最小实现
    _FIELDS = 14

    def _fmt_datas(datas, skill_id="", rows=None, fields=None):
        if not datas:
            return ""
        d = datas[0]
        items = ["%s=%s" % (k, str(v)[:60]) for k, v in list(d.items())[:(fields or 14)]
                 if v not in (None, "", "-")]
        return "，".join(items)


def _one(datas, skill_id, fields=None):
    """单条记录的格式化（去掉多行列表用的 '- ' 前缀，避免行内出现多余符号）。"""
    txt = _fmt_datas(datas, skill_id, rows=1, fields=fields or _FIELDS)
    return txt[2:] if txt.startswith("- ") else txt


def _fmt_index(datas, label):
    """指数行情 —— 走统一格式化，**只呈现响应里真实存在的字段**。

    ⚠️ 旧实现固定输出「最高 -，最低 -」占位符，而 hithink-zhishu-query 的
       原生响应里并没有这两个字段（实测为：指数代码 / 指数简称 /
       最新涨跌幅:前复权 / 收盘价[日期]），属于凭空编造 —— 已移除。
       现在字段名连同日期后缀原样保留，口径可追溯。
    """
    return _one(datas, "hithink-zhishu-query")


def _fmt_stock(datas, label):
    """个股行情 —— 同样只呈现真实字段。"""
    return _one(datas, "hithink-market-query")


def _fmt_generic(datas, label, fields=None):
    """通用格式化：取记录的真实字段（字段数与 skill_agent 保持一致）。"""
    return _one(datas, "", fields)


def _execute(step, deadline):
    """执行单个技能步骤，返回 (label, text)。"""
    label, kind, payload = step
    if time.time() > deadline:
        return None, ""

    sid = SKILL_IDS or {}
    try:
        if kind == "level":
            out = _run_local(["scripts/level_monitor.py", "--list"], 20)
            if payload in out:
                return label, out[:600]
            return None, ""

        if kind == "kol":
            out = _run_local(["scripts/db_query.py", "--kol-name", payload,
                              "--days", "3", "--latest", "5", "--json"], 25)
            recs = json.loads(out) if out.strip().startswith("[") else []
            if recs:
                lines = ["%s %s" % (str(r.get("record_date"))[:16],
                                    (r.get("content") or "")[:70]) for r in recs[:5]]
                return label, payload + " 近3日观点：\n" + "\n".join(lines)
            return None, ""

        if kind == "index":
            d = _call(sid.get("index", "hithink-zhishu-query"), payload + "最新点位", 3)
            return label, _fmt_index(d, payload)

        if kind == "stock":
            name, code = payload
            d = _call(sid.get("market", "hithink-market-query"), "%s最新价" % (name or code), 3)
            return label, _fmt_stock(d, name)

        if kind == "finance":
            name, code = payload
            d = _call(sid.get("finance", "hithink-finance-query"),
                      "%s最新市盈率净资产收益率净利润增长率" % (name or code), 3)
            return label, _fmt_generic(d, "财务·" + name)

        if kind == "research":
            d = _call(sid.get("insresearch", "hithink-insresearch-query"),
                      "%s最新研报评级" % payload, 3)
            return label, _fmt_generic(d, "研报·" + payload)

        if kind == "industry":
            d = _call(sid.get("industry", "hithink-industry-query"),
                      "%s行业最新涨跌幅估值" % payload, 3, timeout=SLOW_TIMEOUT)
            return label, _fmt_generic(d, "行业·" + payload)

        if kind == "macro":
            d = _call(sid.get("macro", "hithink-macro-query"), payload, 3)
            return label, _fmt_generic(d, "宏观")

        if kind == "etf":
            d = _call(sid.get("etf_selector", "hithink-etf-selector"), payload, 5)
            if not d:
                return None, ""
            rows = []
            for it in d[:3]:
                rows.append("%s(%s) 涨跌%s" % (it.get("基金简称") or it.get("ETF简称") or "",
                                               it.get("基金代码") or it.get("ETF代码") or "",
                                               it.get("最新涨跌幅") or ""))
            return label, "ETF：\n" + "\n".join(rows)

        if kind == "basic":
            d = _call(sid.get("basicinfo", "hithink-basicinfo-query"),
                      "%s公司基本信息上市日期主营业务" % payload, 3)
            return label, _fmt_generic(d, "资料·" + payload)

        if kind == "news":
            items = _search_news(payload, 4)
            if not items:
                return None, ""
            lines = []
            for it in items:
                t = (it.get("title") or "")[:60]
                c = (it.get("content") or "")[:110]
                lines.append("- %s%s" % (t, ("：" + c) if c else ""))
            return label, "相关资讯：\n" + "\n".join(lines)

    except Exception:
        return None, ""
    return None, ""


def _run_local(args, timeout=25):
    import subprocess
    try:
        r = subprocess.run([sys.executable] + args, capture_output=True, text=True,
                           timeout=timeout, cwd=SKILL_DIR,
                           encoding="utf-8", errors="replace")
        return (r.stdout or r.stderr or "").strip()
    except Exception:
        return ""


def build_context(question: str, max_chars: int = None) -> str:
    """按问题意图取多源数据，拼成给 AI 的结构化上下文。"""
    q = (question or "").strip()
    if not q:
        return ""

    plan = route(q)
    deadline = time.time() + TOTAL_BUDGET
    parts, used = [], []

    for step in plan:
        label, text = _execute(step, deadline)
        if text:
            parts.append("【%s】%s" % (label, text))
            used.append(label)

    ctx = "\n".join(parts)
    limit = max_chars or MAX_CTX
    if len(ctx) > limit:
        ctx = ctx[:limit] + "\n…（上下文已截断）"

    if not used:
        return ""
    return ctx


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="蜜蜂技能整合层")
    ap.add_argument("question", nargs="?", help="用户问题")
    ap.add_argument("--route", action="store_true", help="只显示路由结果")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not args.question:
        ap.print_help()
        return 1

    if args.route:
        plan = route(args.question)
        print("问题：%s" % args.question)
        print("路由 %d 个技能：" % len(plan))
        for label, kind, payload in plan:
            print("  - %-22s kind=%-10s payload=%s" % (label, kind, payload))
        return 0

    t0 = time.time()
    ctx = build_context(args.question)
    if args.json:
        print(json.dumps({"question": args.question, "elapsed": round(time.time() - t0, 1),
                          "context": ctx}, ensure_ascii=False, indent=1))
    else:
        print("问题：%s" % args.question)
        print("耗时：%.1fs  上下文：%d 字" % (time.time() - t0, len(ctx)))
        print("-" * 70)
        print(ctx or "(无数据)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

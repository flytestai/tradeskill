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

#: 统一格式化层（与 skill_agent 共用，消除两套口径）
try:
    from context_format import (
        format_datas as _fmt_datas, FIELDS_PER_ROW as _FIELDS,
        cfg as _cfg, cfg_int as _cfg_int, skill_version as _skill_version,
        SUMMARY_MAX,
    )
except Exception:                                       # 退化：内置最小实现
    _FIELDS = 14

    def _skill_version(_sid):
        return "1.0.0"

    def _cfg(_k, d=""):
        return service_env(_k, d)

    def _cfg_int(_k, d):
        try:
            return int(_cfg(_k, str(d)) or d)
        except Exception:
            return d

    def _fmt_datas(datas, skill_id="", rows=None, fields=None):
        if not datas:
            return ""
        d = datas[0]
        items = ["%s=%s" % (k, str(v)[:60]) for k, v in list(d.items())[:(fields or 14)]
                 if v not in (None, "", "-")]
        return "，".join(items)

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 单个技能的调用超时（秒）；慢技能（如 industry/event）给多一点
DEFAULT_TIMEOUT = _cfg_int("SKILL_TIMEOUT", 25)
SLOW_TIMEOUT = _cfg_int("SKILL_TIMEOUT_SLOW", 40)
#: 整个上下文构建的总预算（秒）；超时后放弃剩余技能，用已有数据回答
TOTAL_BUDGET = _cfg_int("CONTEXT_BUDGET", 600)
#: 上下文最大字符数（防止 prompt 过长）
#  ⚠️ 与 skill_agent 保持**同一上限**：此前这里是 6000、skill_agent 是 24000，
#     导致「规则回退路径」比「AI 规划路径」少 4 倍上下文，同一问题答案质量漂移。
MAX_CTX = _cfg_int("CONTEXT_MAX_CHARS", 24000)

#: 搜索类端点（可配置，与 skill_agent 的网关保持一致）
COMPREHENSIVE_URL = (_cfg("BEE_GATEWAY_URL", "https://bee-ai.integrity.com.cn")
                     .rstrip("/") + "/skills/v1/comprehensive/search")

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
         "X-Claw-Skill-Id": "news-search",
         "X-Claw-Skill-Version": _skill_version("news-search"),
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
                            "content": (it.get("summary") or it.get("content") or it.get("摘要") or "")[:SUMMARY_MAX],
                            "time": it.get("publish_time") or it.get("time") or ""})
        return out
    except Exception:
        return []


def _search(channel: str, skill_id: str, query: str, limit: int = 5):
    """搜索类技能通用调用（news/announcement/report 共用 /comprehensive/search 端点）。"""
    import secrets
    body = json.dumps({"channels": [channel], "app_id": "AIME_SKILL",
                       "query": query}, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json", "X-Claw-Call-Type": "normal",
         "X-Claw-Skill-Id": skill_id,
         "X-Claw-Skill-Version": _skill_version(skill_id),
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
                            "content": (it.get("summary") or it.get("content") or it.get("摘要") or "")[:SUMMARY_MAX],
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


#: 美股 ticker（全大写 2-6 位词边界）；黑名单排除常见缩写/术语词，
#: 避免「MA 走势」「PE 估值」「RSI 超买」之类被当成股票代码。
US_TICKER_RE = re.compile(r"\b([A-Z]{2,6})\b")
US_TICKER_STOP = {
    "ETF", "ETFS", "MA", "MACD", "RSI", "BOLL", "KDJ", "WR", "PE", "PB",
    "ROE", "ROA", "PEG", "PS", "GDP", "CPI", "PPI", "PMI", "AI", "USD",
    "CNY", "HKD", "IPO", "FOMC", "ESG", "DIF", "DEA", "TTM", "EV", "SH",
    "SZ", "BJ", "AH", "NA", "NG", "RE", "OK", "GO", "BY", "TO", "AND",
    "OR", "IN", "ON", "AT", "IS", "IT", "BE", "DO", "IF", "AS", "OF",
    "UP", "NO", "SO", "MY", "ME", "WE", "HE", "NOT", "ALL", "CAN", "GET",
    "ROE", "EPS", "ROI", "MDD", "VAR", "CAGR", "YOY", "HOLD", "BUY",
}
#: 常见美股中文名 → ticker（问到中文名也能识别；仅收无歧义高频标的）
US_STOCK_NAMES = {
    "甲骨文": "ORCL", "苹果": "AAPL", "特斯拉": "TSLA", "英伟达": "NVDA",
    "微软": "MSFT", "谷歌": "GOOGL", "亚马逊": "AMZN", "脸书": "META",
    "台积电": "TSM", "英特尔": "INTC", "高通": "QCOM", "博通": "AVGO",
    "奈飞": "NFLX", "礼来": "LLY", "摩根大通": "JPM", "可口可乐": "KO",
    "麦当劳": "MCD", "雪佛龙": "CVX", "埃克森美孚": "XOM", "迪士尼": "DIS",
    "耐克": "NKE", "星巴克": "SBUX", "伯克希尔": "BRK.B",
}


def _is_us_code(code: str) -> bool:
    """是否美股代码（非 6 位数字即视为美股：ORCL / BRK.B 等）。"""
    c = (code or "").strip()
    return bool(c) and not c.isdigit()


def _find_stocks(q: str) -> list:
    out = []
    for code in _CODE_RE.findall(q)[:3]:
        out.append((code, code))
    for name, code in KNOWN_STOCKS.items():
        if name in q:
            out.append((name, code))
    # 美股中文名（如「甲骨文」）→ ticker；6 位 A 股优先，不受影响
    for name, ticker in US_STOCK_NAMES.items():
        if name in q:
            out.append((name, ticker))
    # 美股 ticker（全大写，排除缩写词黑名单）
    for m in US_TICKER_RE.findall(q or ""):
        if m not in US_TICKER_STOP:
            out.append((m, m))
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
             | "astock" | "cb" | "fund" | "futures" | "hkstock"
             | "sector" | "usstock" | "announcement" | "report"
             | "event" | "business" | "management"
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
        if _is_us_code(code):
            # ---- 美股标的（2026-10-14 新增）：此前 _find_stocks 只认
            # 6 位 A 股代码与白名单，ORCL/AAPL 等 ticker 不被识别，
            # 行情/财务/技术指标全部缺失。网关实测支持美股（ORCL.N 等），
            # 技术面走腾讯公开接口本地计算（stock_data_service 仅支持 A 股）。
            plan.append(("美股行情·" + name, "stock", (name, code)))
            plan.append(("美股技术·" + name, "ustechnical", (name, code)))
            plan.append(("财务·" + name, "finance", (name, code)))
            plan.append(("资讯·" + name, "news", "%s 美股" % (name or code)))
            if is_decision or any(w in q for w in
                                  ("研报", "评级", "目标价", "机构", "券商", "预测")):
                plan.append(("研报·" + name, "research", name))
            continue
        plan.append(("行情·" + name, "stock", (name, code)))
        plan.append(("个股数据·" + code, "stockdata", code))
        if is_decision or any(w in q for w in
                              ("估值", "市盈率", "PE", "PB", "财务", "业绩",
                               "营收", "净利", "ROE", "基本面", "贵不贵")):
            plan.append(("财务·" + name, "finance", (name, code)))
        if is_decision or any(w in q for w in
                              ("研报", "评级", "目标价", "机构", "券商", "预测")):
            plan.append(("研报·" + name, "research", name))
        if any(w in q for w in ("上市", "发行价", "公司", "做什么")):
            plan.append(("资料·" + name, "basic", name))
        if any(w in q for w in ("业绩预告", "质押", "解禁", "调研", "监管", "增发", "处罚")):
            plan.append(("事件·" + name, "event", (name, code)))
        if any(w in q for w in ("主营", "主营业务", "客户", "供应商", "合同", "参控股", "股权投资")):
            plan.append(("经营·" + name, "business", name))
        if any(w in q for w in ("股东", "股权", "股本", "实控人", "前十大")):
            plan.append(("股权·" + name, "management", name))
        if any(w in q for w in ("公告", "财报", "分红", "回购", "增持", "减持")):
            plan.append(("公告·" + name, "announcement", name))

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

    # ---- 选股/筛选类（A股/港股/美股/可转债/基金/期货/板块）----
    if (any(w in q for w in ("选股", "筛选股", "选A股", "A股选", "龙头股", "白马股",
                             "荐股", "股票筛选"))
            or ("筛选" in q and "股" in q)):
        plan.append(("A股选股", "astock", q))
    if any(w in q for w in ("可转债", "转债", "转股溢价")):
        plan.append(("可转债筛选", "cb", q))
    if any(w in q for w in ("基金", "公募", "基金经理", "定投", "指数基金")):
        plan.append(("基金筛选", "fund", q))
    if any(w in q for w in ("期货", "期权", "商品期货", "股指期货")):
        plan.append(("期货期权", "futures", q))
    if any(w in q for w in ("港股", "港股通")):
        plan.append(("港股筛选", "hkstock", q))
    if any(w in q for w in ("板块筛选", "选板块", "行业筛选", "强势板块")):
        plan.append(("板块筛选", "sector", q))
    if any(w in q for w in ("美股", "选美股")):
        plan.append(("美股筛选", "usstock", q))
    # ---- 公告 / 研报全文（search 端点；个股定向时在个股块里已加）----
    if any(w in q for w in ("公告", "财报", "分红", "回购")) and not stocks:
        plan.append(("公告", "announcement", _extract_topic(q) or q))
    if any(w in q for w in ("研报", "研究报告")) and not stocks:
        plan.append(("研报全文", "report", _extract_topic(q) or q))

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
    # 上限可配置（取消硬编码 8）；这些是**规则路由**的候选，不是模型自主决策
    return uniq[:max(1, _cfg_int("ROUTER_MAX_STEPS", 8))]


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


def _fmt_selector(datas, label, rows=3):
    """选股/筛选类技能：展示前 N 条结果（字段名原样，与 skill_agent 口径一致）。"""
    return _fmt_datas(datas, "", rows=rows, fields=_FIELDS)


# --------------------------------------------------------------------------
# 美股技术面（腾讯公开接口 + 本地计算；网关/本地 stockdata 不覆盖此层）
# --------------------------------------------------------------------------

_US_QUOTE_URL = "https://qt.gtimg.cn/q=us%s"
_US_KLINE_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                 "?param=us%s,day,,,%d,qfq")


def _to_f(x):
    try:
        v = float(x)
        return v if v == v else None   # NaN 防护
    except (TypeError, ValueError):
        return None


def _fetch_us_quote(ticker):
    """腾讯美股实时行情（GBK）→ dict，失败返回 {}。

    字段实测（usORCL）：p[1]名称 p[2]代码 p[3]现价 p[4]昨收 p[5]开
    p[30]时间 p[31]涨跌额 p[32]涨跌幅 p[33]高 p[34]低 p[35]币种
    p[39]PE(TTM) p[46]总市值(亿) p[47]英文全名。
    """
    try:
        req = urllib.request.Request(_US_QUOTE_URL % ticker,
                                     headers={"User-Agent": "Mozilla/5.0"})
        text = urllib.request.urlopen(req, timeout=12).read().decode("gbk", "replace")
        p = text.split('"')[1].split("~")
        if len(p) < 48:
            return {}
        return {
            "name": p[1], "code": p[2], "price": _to_f(p[3]),
            "prev": _to_f(p[4]), "open": _to_f(p[5]), "dt": p[30],
            "chg": _to_f(p[31]), "pct": _to_f(p[32]), "high": _to_f(p[33]),
            "low": _to_f(p[34]), "currency": p[35], "pe": _to_f(p[39]),
            "mcap": _to_f(p[45]), "fullname": p[46] if len(p) > 46 else "",
        }
    except Exception as e:
        print("[WARN] 腾讯美股行情失败(%s): %s" % (ticker, e))
        return {}


def _fetch_us_kline(ticker, days=120):
    """雅虎财经日K（前复权 adjclose）→ [(YYYYMMDD, close), ...]，失败返回 []。

    腾讯美股 fqkline 实测只回 2 条（上市日+最新日），不可用；东财/新浪
    美股K线在海外节点被拒；本服务器为海外节点，雅虎 chart API 可直连
    且免密钥。range=6mo ≈ 126 个交易日，满足 MA60/RSI14/MACD(40+)/BOLL20/
    60日区间所需样本；adjclose 与腾讯实时价口径一致（末条=最新收盘）。
    """
    try:
        req = urllib.request.Request(
            "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=6mo" % ticker,
            headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        data = json.loads(raw or "{}")
        r = (((data.get("chart") or {}).get("result") or [{}])[0]) or {}
        ts = r.get("timestamp") or []
        adj = (((r.get("indicators") or {}).get("adjclose") or [{}])[0]).get("adjclose") or []
        closes = (((r.get("indicators") or {}).get("close") or [{}])[0]).get("close") or []
        series = adj if len(adj) >= len(closes) else closes
        import datetime as _dt
        out = []
        for t, c in zip(ts, series):
            if c is None:
                continue
            day = _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y%m%d")
            out.append((day, float(c)))
        return out
    except Exception as e:
        print("[WARN] 雅虎美股日K失败(%s): %s" % (ticker, e))
        return []


def _fmt_pct(x, suffix="%"):
    return ("%.2f%s" % (x, suffix)) if isinstance(x, (int, float)) else "--"


def _us_technical_snapshot(ticker, name=""):
    """美股技术面快照：腾讯实时行情 + 日K本地计算 MA/RSI/MACD/BOLL/区间。

    指标函数复用 market_summary（rsi14/macd/bollinger，同口径可追溯）；
    数据源标注腾讯前复权K线，AI 引用时可区分口径。
    """
    q = _fetch_us_quote(ticker)
    closes = [c for _, c in _fetch_us_kline(ticker, 120)]
    if not q and not closes:
        return ""
    disp = q.get("name") or (name if (name and name != ticker) else ticker)
    lines = []
    if q:
        lines.append("【美股·%s（%s）实时快照（腾讯行情）】" % (disp, q.get("code") or ticker))
        lines.append("现价 %s（%s %s）｜涨跌 %s（%s）｜昨收 %s｜开 %s｜高 %s｜低 %s" % (
            q.get("price"), q.get("dt"), q.get("currency"),
            q.get("chg"), _fmt_pct(q.get("pct")), q.get("prev"),
            q.get("open"), q.get("high"), q.get("low")))
        extra = []
        if q.get("pe") is not None:
            extra.append("PE(TTM) %.2f" % q["pe"])
        if q.get("mcap") is not None:
            extra.append("总市值 %.1f 亿美元" % q["mcap"])
        if q.get("fullname"):
            extra.append(q["fullname"])
        if extra:
            lines.append("｜".join(extra))
    if len(closes) >= 5:
        try:
            from market_summary import rsi14, macd, bollinger
            ma5 = sum(closes[-5:]) / 5.0
            ma20 = sum(closes[-20:]) / 20.0 if len(closes) >= 20 else None
            ma60 = sum(closes[-60:]) / 60.0 if len(closes) >= 60 else None
            rsi = rsi14(closes)
            dif, dea, hist = macd(closes)
            mid, upper, lower, bpos = bollinger(closes)
            if ma60 is not None and ma20 is not None:
                if ma5 > ma20 > ma60:
                    ma_state = "多头排列"
                elif ma5 < ma20 < ma60:
                    ma_state = "空头排列"
                else:
                    ma_state = "均线缠绕"
            else:
                ma_state = "--"
            mas = ["MA5 %.2f" % ma5]
            if ma20 is not None:
                mas.append("MA20 %.2f" % ma20)
            if ma60 is not None:
                mas.append("MA60 %.2f" % ma60)
            lines.append("【技术面（雅虎日K·前复权 %d 日，本地计算）】%s｜%s" % (
                len(closes), "｜".join(mas), ma_state))
            if rsi is not None:
                lines.append("RSI14 %.1f" % rsi)
            if dif is not None and dea is not None:
                cross = "金叉" if dif > dea else "死叉"
                zone = "零轴上" if dif > 0 else "零轴下"
                lines.append("MACD %s·%s｜DIF %.2f｜DEA %.2f" % (cross, zone, dif, dea))
            if mid is not None:
                lines.append("布林带：上轨 %.2f｜中轨 %.2f｜下轨 %.2f｜位置 %s" % (
                    upper, mid, lower, _fmt_pct(bpos)))
            w60 = closes[-60:]
            hi, lo = max(w60), min(w60)
            pos = (closes[-1] - lo) / (hi - lo) * 100 if hi > lo else None
            lines.append("60日区间：高 %.2f｜低 %.2f｜位置 %s" % (hi, lo, _fmt_pct(pos)))
        except Exception as e:
            print("[WARN] 美股技术指标计算失败(%s): %s" % (ticker, e))
    return "\n".join(lines)

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

        if kind == "ustechnical":
            name, code = payload
            return label, _us_technical_snapshot(code, name)

        if kind == "stockdata":
            out = _run_local(["scripts/stock_data_service.py", "--code", payload,
                              "--kind", "all"], 40)
            return label, out[:3000] if out else ""

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
                t = (it.get("title") or "")[:80]
                c = (it.get("content") or "")[:SUMMARY_MAX]
                lines.append("- %s%s" % (t, ("：" + c) if c else ""))
            return label, "相关资讯：\n" + "\n".join(lines)

        if kind in ("astock", "cb", "fund", "hkstock", "sector", "usstock"):
            _sel_map = {"astock": "astock_selector", "cb": "cb_selector",
                        "fund": "fund_selector", "hkstock": "hkstock_selector",
                        "sector": "sector_selector", "usstock": "usstock_selector"}
            d = _call(sid.get(_sel_map[kind], ""), payload, 5, timeout=SLOW_TIMEOUT)
            return label, _fmt_selector(d, payload)

        if kind == "futures":
            d = _call(sid.get("futures_query", "hithink-futures-query"),
                      payload, 5, timeout=SLOW_TIMEOUT)
            return label, _fmt_selector(d, payload)

        if kind == "event":
            name, code = payload
            d = _call(sid.get("event", "hithink-event-query"),
                      "%s业绩预告解禁质押" % (name or code), 5)
            return label, _fmt_generic(d, "事件·" + name)

        if kind == "business":
            d = _call(sid.get("business", "hithink-business-query"),
                      "%s主营业务构成客户供应商" % payload, 5)
            return label, _fmt_generic(d, "经营·" + payload)

        if kind == "management":
            d = _call(sid.get("management", "hithink-management-query"),
                      "%s股东股权结构实控人" % payload, 5)
            return label, _fmt_generic(d, "股权·" + payload)

        if kind == "announcement":
            items = _search("announcement", "announcement-search", payload, 5)
            if not items:
                return None, ""
            lines = []
            for it in items:
                t = (it.get("title") or "")[:80]
                c = (it.get("content") or "")[:SUMMARY_MAX]
                lines.append("- %s%s" % (t, ("：" + c) if c else ""))
            return label, "相关公告：\n" + "\n".join(lines)

        if kind == "report":
            items = _search("report", "report-search", payload, 5)
            if not items:
                return None, ""
            lines = []
            for it in items:
                t = (it.get("title") or "")[:80]
                c = (it.get("content") or "")[:SUMMARY_MAX]
                lines.append("- %s%s" % (t, ("：" + c) if c else ""))
            return label, "相关研报：\n" + "\n".join(lines)

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

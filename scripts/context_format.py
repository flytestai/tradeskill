#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一技能响应格式化 —— skill_agent / skill_router 共用同一份实现。

为什么单独抽一个模块
--------------------
原先前端有**两套**格式化实现，导致同一问题在「AI 规划路径」与「规则回退路径」
下拿到 4 倍不同的数据量：

    skill_agent._fmt_rows   : 14 字段/行 × 6 行，上下文上限 24000 字
    skill_router._fmt_*     :  6 字段/行 × 1 行，上下文上限  6000 字

两条路径喂给同一个模型的上下文口径不一致，回答质量随之漂移。
本模块把格式化收敛为**唯一实现**，两个调用方都从这里取。

设计原则
--------
1. **只呈现真实存在的字段** —— 绝不输出硬编码占位符。
   此前 `skill_router._fmt_index` 会固定输出「最高 -，最低 -」，
   而 `hithink-zhishu-query` 的原生响应里**根本没有**这两个字段
   （实测字段为：指数代码 / 指数简称 / 最新涨跌幅:前复权 / 收盘价[日期]），
   属于凭空编造数据 —— 现已彻底移除。
2. **保留口径信息** —— 字段名原样保留，包括 `收盘价[20260918]` 这类**带日期**的键。
   日期是判断数据新鲜度的关键依据，不能丢掉；数值仅做长度截断，不改写。
3. **响应结构不受影响** —— 本模块只负责「呈现给模型的文本」，
   原始响应始终保持完整（遵循网关规范条件六：透明传递）。
"""
from __future__ import annotations

#: 搜索类技能（走 /comprehensive/search，响应结构与表格类不同）
SEARCH_SKILLS = ("news-search", "announcement-search", "report-search")

#: 技能 ID → 版本号（**取自各技能 SKILL.md 自声明版本**，非统一硬编码）
#:   · 19 个 hithink-* 与 news-search / announcement-search 均为 1.0.0
#:   · report-search 自声明 2.0.0（此前被统一发成 1.0.0，与契约不符）
#: 实测网关对版本不强制校验（1.0.0 也能返回），但严格对齐原生应逐技能发送。
SKILL_VERSIONS = {
    "report-search": "2.0.0",
}
#: 未在 SKILL_VERSIONS 中登记的技能使用的默认版本
DEFAULT_SKILL_VERSION = "1.0.0"


def skill_version(skill_id: str) -> str:
    """返回该技能应发送的 X-Claw-Skill-Version（逐技能，不再统一硬编码）。"""
    return SKILL_VERSIONS.get(skill_id, DEFAULT_SKILL_VERSION)


#: 简化查询时用于切分的并列/补充连词（长句往往由它们串起多个从句）
_SPLIT_WORDS = ("以及", "及", "与", "和", "、", "并对", "并", "；", ";", "，", ",")


def simplify_query(query: str, max_len: int = 16) -> str:
    """把过长的检索问句简化为「主体 + 核心指标」。

    为什么需要（实测结论）
    ----------------------
    网关对**过长/多重从句**的问句做 NL→SQL 时经常产出空结果
    （响应里带 `model_sql` / `condition` 字段、datas 为空的路径），
    而同一主体换短问句立刻有数据。实测：

        厦门钨业主营业务构成主要产品及上下游情况   → 0 条（带 model_sql）
        厦门钨业主营业务构成                       → 5 条

        钨及稀有金属行业最新估值行业涨跌幅板块排名与资金流向 → 0 条
        稀有金属行业估值                                   → 5 条

    因此「取不到数据时用简化问句重试一次」是必要的，
    与原生 Agent 发现查不到就换个说法再查的行为一致。
    """
    q = (query or "").strip()
    if len(q) <= max_len:
        return ""
    # 1) 先按并列连词切，取第一段（通常是「主体+核心指标」）
    head = q
    for w in _SPLIT_WORDS:
        if w in head:
            cand = head.split(w)[0].strip()
            if len(cand) >= 4:
                head = cand
                break
    if 4 <= len(head) <= max_len:
        return head
    # 2) 仍然过长 → 直接截断（中文金融问句前 16 字基本覆盖主体与指标）
    if len(head) > max_len:
        return head[:max_len]
    return ""

#: 每条记录呈现的最大字段数（原 skill_agent 为 14，skill_router 为 6，现统一）
FIELDS_PER_ROW = 14
#: 每个技能呈现的最大记录数
ROWS_PER_SKILL = 6
#: 单个字段值的最大字符数（超出截断，不改写）
VALUE_MAX = 60
#: 搜索类「摘要」字段的最大字符数
SUMMARY_MAX = 400


def is_search_skill(skill_id: str) -> bool:
    return skill_id in SEARCH_SKILLS


def _first(d: dict, *keys):
    """按顺序取第一个非空值（缺失返回 None，**不返回占位符**）。"""
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip() not in ("", "-"):
            return v
    return None


def format_record(d: dict, skill_id: str = "", fields: int = None) -> str:
    """把**单条**记录格式化为一行文本。

    - 表格类：`字段=值，字段=值…`（字段名原样，含日期后缀）
    - 搜索类：`[日期] 标题：摘要`
    """
    if not isinstance(d, dict):
        return ""
    fields = fields or FIELDS_PER_ROW

    if is_search_skill(skill_id):
        title = _first(d, "title", "标题")
        if not title:
            return ""
        tm = _first(d, "publish_time", "publish_date", "time")
        body = _first(d, "summary", "content", "摘要")
        prefix = "[%s] " % str(tm)[:10] if tm else ""
        suffix = "：" + str(body)[:SUMMARY_MAX] if body else ""
        return "%s%s%s" % (prefix, str(title)[:80], suffix)

    items = []
    for k, v in list(d.items())[:fields]:
        if v is None:
            continue
        s = str(v).strip()
        if s in ("", "-"):
            continue
        items.append("%s=%s" % (k, s[:VALUE_MAX]))
    return "，".join(items)


def format_datas(datas, skill_id: str = "", rows: int = None,
                 fields: int = None) -> str:
    """把 datas 列表格式化为多行文本。空数据返回空串（**不输出占位文字**）。"""
    if not datas:
        return ""
    rows = rows or ROWS_PER_SKILL
    out = []
    for d in datas[:rows]:
        line = format_record(d, skill_id, fields)
        if line:
            out.append("- " + line)
    return "\n".join(out)


def format_response(resp, skill_id: str = "", rows: int = None,
                    fields: int = None) -> str:
    """从**完整响应**中取 datas 并格式化（不修改原响应）。"""
    if not isinstance(resp, dict):
        return ""
    v = resp.get("datas")
    if v is None:
        v = resp.get("data")
    if isinstance(v, dict):
        v = v.get("list") or v.get("items") or []
    if not isinstance(v, list):
        return ""
    return format_datas(v, skill_id, rows, fields)

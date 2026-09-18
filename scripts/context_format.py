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

import os

# ---------------------------------------------------------------------------
# 配置读取：环境变量优先，回退 data/local_config.env
# ---------------------------------------------------------------------------
# ⚠️ 为什么统一走 service_env 而不是 os.environ.get
#    平台有两种部署形态：
#      · systemd / docker --env-file  → 配置在**环境变量**里
#      · 直接跑脚本（宿主机 cron）    → 配置在 **data/local_config.env** 文件里
#    此前 skill_agent / skill_router 用 os.environ.get 直读，
#    在「只用配置文件」的场景下**读不到任何值**，全部回退默认值 ——
#    实测：把 PLAN_CACHE_TTL=999 写进 local_config.env，skill_agent 仍读到 0。
#    而 llm_client / bee_client 走 service_env 能正确读到。
#    本模块统一为 cfg()/cfg_int()，消除这一个模块级别的行为不一致。

try:
    from common import service_env as _service_env
except Exception:                              # 允许独立运行
    def _service_env(k, d=None):
        return os.environ.get(k, d)


def cfg(key: str, default: str = "") -> str:
    """读配置：环境变量优先，回退 data/local_config.env（去空白）。"""
    v = _service_env(key, None)
    if v is None or str(v).strip() == "":
        v = os.environ.get(key, default)
    return str(v).strip() if v is not None else default


def cfg_int(key: str, default: int) -> int:
    """读整数配置（非法值回退 default）。"""
    try:
        return int(str(cfg(key, str(default))).strip() or default)
    except (TypeError, ValueError):
        return default


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
#: 每个技能呈现的记录数上限（默认值，实际会按剩余预算自适应放大）
ROWS_PER_SKILL = 6
#: 单个技能呈现记录数的**硬上限**（自适应放大的天花板，防单个技能吃光预算）
ROWS_HARD_MAX = 20
#: 单个字段值的最大字符数（超出截断，不改写）
VALUE_MAX = 60
#: 搜索类「摘要」字段的最大字符数
SUMMARY_MAX = 400

# ⚠️ 关于「截断」的透明性
#    原生响应常返回 10 条，而此前固定只呈现 6 条 → **静默丢弃 40%**，模型完全
#    不知道还有更多内容。这与「与原生一致」的目标冲突，且会让模型基于不完整
#    信息下结论。现在改为：
#      1) 默认行数按「剩余上下文预算」自适应放大（ROWS_HARD_MAX 封顶）
#      2) 一旦发生截断，**显式标注**「共 N 条，已展示 M 条」，让模型知其边界
#    原始响应结构始终不变，此处只影响呈现文本。


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
                 fields: int = None, budget: int = None) -> str:
    """把 datas 列表格式化为多行文本。空数据返回空串（**不输出占位文字**）。

    :param rows:   最多呈现几条。None → 按 budget 自适应（上限 ROWS_HARD_MAX）
    :param budget: 该技能可用的字符预算。给定则据此决定行数，
                   让条目多的技能多显示一些、而不是一律砍到 6 条。
    :return: 发生截断时，末尾会附「（共 N 条，已展示 M 条）」标注。
    """
    if not datas:
        return ""
    total = len(datas)

    if rows is None:
        if budget and budget > 0:
            # 先按默认行数量一遍，估算单行成本，再反推能放几行
            probe = [format_record(d, skill_id, fields) for d in datas[:ROWS_PER_SKILL]]
            probe = [p for p in probe if p]
            per_row = (sum(len(p) for p in probe) / len(probe)) if probe else 200
            rows = int(budget / max(per_row + 3, 1))
            rows = max(ROWS_PER_SKILL, min(rows, ROWS_HARD_MAX))
        else:
            rows = ROWS_PER_SKILL

    out = []
    for d in datas[:rows]:
        line = format_record(d, skill_id, fields)
        if line:
            out.append("- " + line)
    txt = "\n".join(out)

    # 透明标注截断，避免模型误以为这就是全部
    if total > len(out):
        txt += "\n  …（该数据源共 %d 条，此处展示前 %d 条）" % (total, len(out))
    return txt


def format_response(resp, skill_id: str = "", rows: int = None,
                    fields: int = None, budget: int = None) -> str:
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
    return format_datas(v, skill_id, rows, fields, budget)

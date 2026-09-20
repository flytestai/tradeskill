#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KOL 平台薄 wrapper：自然语言 → REST 调用 → 紧凑输出。

**不重算任何业务逻辑** —— 只做意图识别 + 调后端 + 压缩呈现。

用法:
    python query.py "wu2198 最新观点"
    python query.py "wu2198 VIP 消息"
    python query.py "wu2198 准确率"
    python query.py "对比所有大V"
    python query.py "创业板指点位 3368"
    python query.py "上证指数行情"
    python query.py "半仓跟单回测"

环境变量:
    KOL_PLATFORM_URL   后端地址，默认 http://127.0.0.1:8000
    KOL_PLATFORM_KEY   API Key（见服务端 /etc/kol-platform/env）
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("KOL_PLATFORM_URL", "http://127.0.0.1:8000").rstrip("/")
KEY = os.environ.get("KOL_PLATFORM_KEY", "").strip()
TIMEOUT = int(os.environ.get("KOL_PLATFORM_TIMEOUT", "120"))


def api(path: str, method: str = "GET", body: dict = None, params: dict = None):
    """调用后端 REST 接口，返回 data 字段。"""
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body).encode("utf-8") if body else None
    headers = {"Content-Type": "application/json"}
    if KEY:
        headers["X-API-Key"] = KEY
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise SystemExit("[ERROR] HTTP %s: %s" % (e.code, detail))
    except Exception as e:
        raise SystemExit("[ERROR] 无法连接平台 %s: %s" % (BASE, e))
    if not payload.get("ok"):
        err = payload.get("error") or {}
        raise SystemExit("[ERROR] %s" % err.get("message", "未知错误"))
    return payload.get("data")


# --------------------------------------------------------------------------
# 意图识别（关键字 → 接口）
# --------------------------------------------------------------------------

KOL_RE = re.compile(r"\b(wu2198|[A-Za-z][\w\-]{2,20})\b")


def _find_kol(text: str) -> str:
    """从问句里抽取大V名称；缺省用 wu2198（当前库中唯一收录）。"""
    m = KOL_RE.search(text)
    if m and m.group(1).lower() not in ("vip", "api", "http"):
        return m.group(1)
    # 中文名兜底：去掉指令词后剩下的连续中文
    t = re.sub(r"(最近|最新|近|观点|言论|消息|准确率|概览|统计|的|查|看|一下|\d+|天|条|VIP|vip)", "", text)
    t = re.sub(r"[\s，,。.]", "", t)
    return t if t and len(t) <= 10 else "wu2198"


def _days(text: str, default: int = 30) -> int:
    m = re.search(r"(?:最近|近|过去)\s*(\d+)\s*(?:天|日)", text)
    return int(m.group(1)) if m else default


def dispatch(text: str):
    """把自然语言映射到接口调用（返回 data）。"""
    t = text.strip()

    # --- 多KOL对比（必须在"列出大V"之前判断，否则"对比所有大V"会被误匹配）---
    if re.search(r"对比|比较", t):
        return api("/api/v1/kol/compare")

    # --- 系统：列出大V ---
    if re.search(r"(有|查|看)(哪些|什么)?大\s*V|大\s*V\s*(列表|清单)|收录了", t):
        return api("/api/v1/kol/list")

    # --- 准确率 ---
    if re.search(r"准确率|命中率|胜率|预测统计", t):
        return api("/api/v1/kol/accuracy", params={"kol_name": _find_kol(t)})

    # --- 预测列表 ---
    if re.search(r"预测(列表|记录)", t):
        return api("/api/v1/kol/predictions", params={"kol": _find_kol(t)})

    # --- 概览 ---
    if re.search(r"概览|总览|统计|多少条|数据量", t):
        return api("/api/v1/kol/summary", params={"kol_name": _find_kol(t)})

    # --- 回测 ---
    if re.search(r"回测|跟单", t):
        strat = "full" if re.search(r"满仓", t) else "half"
        return api("/api/v1/kol/backtest", params={"strategy": strat})

    # --- 关键点位 ---
    m = re.search(r"(创业板指?|上证指数?|科创50|科创综指|深证成指)", t)
    if m and re.search(r"点位|关键位|支撑|压力|到多少", t):
        idx = {"创业板": "创业板指", "创业板指": "创业板指",
               "上证": "上证指数", "上证指数": "上证指数",
               "科创50": "科创50", "科创综指": "科创综指",
               "深证成指": "深证成指"}.get(m.group(1), m.group(1))
        pm = re.search(r"(\d{3,5}(?:\.\d+)?)", t)
        params = {"index": idx}
        if pm:
            params["price"] = pm.group(1)
        return api("/api/v1/levels", params=params)

    # --- 全部点位 ---
    if re.search(r"点位|关键位", t):
        return api("/api/v1/levels")

    # --- 盘前/盘中播报 ---
    if re.search(r"播报|汇总|盘前|盘中", t):
        period = "intraday" if re.search(r"盘中", t) else "premarket"
        return api("/api/v1/market/summary", params={"period": period})

    # --- 提醒状态 ---
    if re.search(r"提醒|告警", t):
        return api("/api/v1/system/alerts")

    # --- 行情 ---
    if m or re.search(r"行情|点位|价格|现价", t):
        target = m.group(1) if m else t
        return api("/api/v1/market/quote", params={"query": target})

    # --- 默认：查言论 ---
    return api("/api/v1/kol/records", params={
        "kol_name": _find_kol(t),
        "days": _days(t),
        "vip_only": "1" if re.search(r"VIP|vip|真爱粉", t) else "",
        "latest": 20,
    })


# --------------------------------------------------------------------------
# 紧凑呈现（禁止原样贴 JSON）
# --------------------------------------------------------------------------

def render(data) -> str:
    if data is None:
        return "(无数据)"
    if isinstance(data, str):
        return data.strip()

    if isinstance(data, list):
        if not data:
            return "(无记录)"
        # 大V列表
        if data and isinstance(data[0], dict) and "cnt" in data[0]:
            return "\n".join(
                "  %s：%s 条（%s ~ %s）" % (d.get("kol_name"), d.get("cnt"),
                                          str(d.get("first_date"))[:10],
                                          str(d.get("last_date"))[:10]) for d in data)
        # 言论列表
        if data and isinstance(data[0], dict) and "content" in data[0]:
            lines = []
            for d in data:
                tag = "🔒VIP" if d.get("is_vip") else "公开 "
                c = (d.get("content") or "").replace("\n", " ")[:90]
                lines.append("  %s | %s | %s" % (str(d.get("record_date"))[:16], tag, c))
            return "\n".join(lines)
        # 能力清单等
        if data and isinstance(data[0], dict) and "name" in data[0]:
            return "\n".join("  • %-18s %s" % (d.get("name"), d.get("desc", "")) for d in data)
        return json.dumps(data, ensure_ascii=False, indent=2)[:2000]

    if isinstance(data, dict):
        # 概览/准确率等返回 {raw: "..."} 的，直接给文本
        if "raw" in data and isinstance(data["raw"], str):
            return data["raw"].strip()
        # 行情（兼容蜜蜂 http 通道与 local 通道的不同字段名）
        if any(k in data for k in ("最新价", "名称", "指数简称")):
            price = data.get("最新价") or next(
                (v for k, v in data.items() if k.startswith("收盘价")), "")
            hi = data.get("最高") or data.get("最高价") or "—"
            lo = data.get("最低") or data.get("最低价") or "—"
            name = data.get("名称") or data.get("指数简称") or data.get("股票简称") or ""
            code = data.get("代码") or data.get("指数代码") or data.get("股票代码") or ""
            chg = (data.get("涨跌幅") or data.get("最新涨跌幅:前复权")
                   or data.get("最新涨跌幅") or "")
            return "  %s（%s）  最新 %s  涨跌 %s%%  高 %s  低 %s" % (
                name, code, price, chg, hi, lo)
        if "alert_state" in data or "price_alerts" in data:
            n1 = len(data.get("alert_state") or [])
            n2 = len(data.get("price_alerts") or [])
            return "  告警状态 %d 条，价格提醒 %d 条" % (n1, n2)
        return json.dumps(data, ensure_ascii=False, indent=2)[:2000]

    return str(data)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    text = " ".join(sys.argv[1:])
    data = dispatch(text)
    out = render(data)
    print(out)
    if not re.search(r"免责声明", out):
        print("\n⚠️ 本内容由 AI 生成，仅供参考，不构成投资建议。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

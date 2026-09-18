#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蜜蜂能力统一适配器（http / mcp / local 三通道可切换）。

设计目标
--------
把散落在 market_summary / monitor_alerts / price_alerts 的蜜蜂查询调用
收敛为单一入口，便于：
  1) Linux 部署时切换数据源（不依赖蜜蜂主体进程）
  2) 统一重试、超时、降级、缓存
  3) 未来接入 MCP 作为智能通道

通道说明
--------
  http  : 直接 POST 蜜蜂技能网关（header 鉴权、无 token）——默认，零改动可用
  mcp   : 通过 MCP 协议调用蜜蜂能力（stdio/SSE）——用于智能面（分析/问答）
  local : 直连公开行情源（腾讯/东财）——完全不依赖蜜蜂，作为降级与兜底

配置（环境变量优先，其次 data/local_config.env）
------------------------------------------------
  BEE_CHANNEL        http | mcp | local         默认 http
  BEE_GATEWAY_URL    默认 https://bee-ai.integrity.com.cn/skills/v1/query2data
  BEE_MCP_ENDPOINT   MCP 服务地址（channel=mcp 时必填）
                     形如 stdio:/path/to/bee-mcp 或 https://host/mcp
  BEE_TIMEOUT        秒，默认 30
  BEE_FALLBACK_LOCAL 置 1 时，http 失败自动降级 local

用法
----
    from bee_client import query
    data = query("创业板指最新点位", skill_id="hithink-zhishu-query")
    datas = (data or {}).get("datas") or []

    # 智能面
    from bee_client import call_mcp
    ans = call_mcp("kol_analyze", {"question": "厦门钨业还能建仓吗"})

CLI
---
    python scripts/bee_client.py --query "上证指数最新点位"
    python scripts/bee_client.py --health          # 探测各通道可用性
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from common import service_env
except Exception:  # 允许独立运行
    def service_env(k, d=None):
        return os.environ.get(k, d)

DEFAULT_GATEWAY = "https://bee-ai.integrity.com.cn/skills/v1/query2data"

#: 已知技能 ID（供调用方引用，避免拼写错误）
SKILL_IDS = {
    "market": "hithink-market-query",
    "index": "hithink-zhishu-query",
    "finance": "hithink-finance-query",
    "industry": "hithink-industry-query",
    "insresearch": "hithink-insresearch-query",
    "macro": "hithink-macro-query",
    "etf_selector": "hithink-etf-selector",
    "basicinfo": "hithink-basicinfo-query",
    "event": "hithink-event-query",
    "business": "hithink-business-query",
    "management": "hithink-management-query",
    "announcement": "announcement-search",
}


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def _cfg(key: str, default: str = "") -> str:
    return (os.environ.get(key) or service_env(key, "") or default).strip()


def channel() -> str:
    return (_cfg("BEE_CHANNEL", "http") or "http").lower()


def gateway_url() -> str:
    return _cfg("BEE_GATEWAY_URL", DEFAULT_GATEWAY) or DEFAULT_GATEWAY


def mcp_endpoint() -> str:
    return _cfg("BEE_MCP_ENDPOINT", "")


def timeout_s() -> int:
    try:
        return int(_cfg("BEE_TIMEOUT", "30") or "30")
    except ValueError:
        return 30


# --------------------------------------------------------------------------
# 通道 1：HTTP（默认，向后兼容）
# --------------------------------------------------------------------------

def _headers(skill_id: str) -> dict:
    """蜜蜂技能网关请求头（header 鉴权，无 token）。"""
    return {
        "Content-Type": "application/json",
        "X-Claw-Call-Type": "normal",
        "X-Claw-Skill-Id": skill_id,
        "X-Claw-Skill-Version": "1.0.0",
        "X-Claw-Plugin-Id": "none",
        "X-Claw-Plugin-Version": "none",
        "X-Claw-Trace-Id": secrets.token_hex(32),
    }


def _query_http(query: str, skill_id: str, limit: int = 10, retries: int = 2):
    """直接 POST 蜜蜂技能网关。保持与改造前完全相同的请求形态。"""
    body = json.dumps({
        "query": query, "page": "1", "limit": str(limit),
        "is_cache": "1", "expand_index": "true",
    }).encode("utf-8")

    last_err = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(gateway_url(), data=body,
                                     headers=_headers(skill_id), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s()) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s" % e.code
            # 4xx 不重试（参数/权限问题，重试无意义）
            if 400 <= e.code < 500:
                break
        except Exception as e:
            last_err = str(e)[:160]
        if attempt < retries:
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError("蜜蜂 HTTP 通道失败: %s" % last_err)


# --------------------------------------------------------------------------
# 通道 2：MCP（智能面：分析 / 问答）
# --------------------------------------------------------------------------

def call_mcp(tool: str, arguments: dict, timeout: int | None = None):
    """通过 MCP 调用蜜蜂能力。

    endpoint 形态：
      stdio:/path/to/bee-mcp        —— 本地 stdio 子进程
      https://host/mcp              —— streamable-http / SSE 远程
    """
    ep = mcp_endpoint()
    if not ep:
        raise RuntimeError("BEE_MCP_ENDPOINT 未配置，无法使用 mcp 通道")

    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as e:
        raise RuntimeError("缺少 mcp 依赖：pip install mcp (%s)" % e)

    import asyncio

    async def _run():
        if ep.startswith("stdio:"):
            parts = ep.split(":", 1)[1].strip().split()
            params = StdioServerParameters(command=parts[0], args=parts[1:])
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    return await s.call_tool(tool, arguments)
        # 远程 streamable-http（优先）或 SSE
        try:
            from mcp.client.streamable_http import streamablehttp_client as _cli
        except ImportError:
            from mcp.client.sse import sse_client as _cli
        async with _cli(ep) as conn:
            r, w = conn[0], conn[1]
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await s.call_tool(tool, arguments)

    return asyncio.run(asyncio.wait_for(_run(), timeout or timeout_s()))


def list_mcp_tools(timeout: int | None = None):
    """列出 MCP 端点暴露的所有 tools（用于能力发现）。"""
    ep = mcp_endpoint()
    if not ep:
        raise RuntimeError("BEE_MCP_ENDPOINT 未配置")

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import asyncio

    async def _run():
        if ep.startswith("stdio:"):
            parts = ep.split(":", 1)[1].strip().split()
            params = StdioServerParameters(command=parts[0], args=parts[1:])
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    return await s.list_tools()
        try:
            from mcp.client.streamable_http import streamablehttp_client as _cli
        except ImportError:
            from mcp.client.sse import sse_client as _cli
        async with _cli(ep) as conn:
            async with ClientSession(conn[0], conn[1]) as s:
                await s.initialize()
                return await s.list_tools()

    return asyncio.run(asyncio.wait_for(_run(), timeout or timeout_s()))


# --------------------------------------------------------------------------
# 通道 3：本地行情源（完全不依赖蜜蜂）
# --------------------------------------------------------------------------

#: 常用指数的腾讯代码映射（local 通道用）
_TENCENT_CODES = {
    "上证指数": "sh000001", "深证成指": "sz399001", "创业板指": "sz399006",
    "科创50": "sh000688", "科创综指": "sh000680", "科创100": "sh000698",
    "沪深300": "sh000300", "中证500": "sh000905", "北证50": "bj899050",
}
_TENCENT_INDEX_ALIAS = {
    "上证": "上证指数", "深成指": "深证成指", "创业板": "创业板指",
    "科创板": "科创综指", "科创板指数": "科创综指",
}


def _tencent_quote(code: str) -> dict | None:
    """腾讯行情（免密钥）。返回 {name, price, change_pct, ...}。"""
    url = "http://qt.gtimg.cn/q=" + code
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "http://gu.qq.com"})
    raw = urllib.request.urlopen(req, timeout=timeout_s()).read().decode("gbk")
    line = raw.strip().split("\n")[0]
    if "=" not in line:
        return None
    p = line.split('"')[1].split("~")
    if len(p) < 35:
        return None
    return {
        "代码": code, "名称": p[1], "最新价": p[3],
        "涨跌幅": p[32], "最高": p[33], "最低": p[34],
        "成交量": p[36] if len(p) > 36 else "",
    }


def _query_local(query: str, skill_id: str, limit: int = 10):
    """直连公开行情源，输出**与蜜蜂网关同构**的 {"datas": [...]} 结构。

    这样上层的解析代码无需区分通道。
    目前覆盖：指数实时点位（腾讯源）。其他类型抛 NotImplementedError，
    由调用方决定是否降级。
    """
    if skill_id not in (SKILL_IDS["index"], SKILL_IDS["market"]):
        raise NotImplementedError("local 通道暂不支持 skill=%s" % skill_id)

    # 从问句中识别指数
    target = None
    for alias, canon in _TENCENT_INDEX_ALIAS.items():
        if alias in query:
            target = canon
            break
    if not target:
        for canon in _TENCENT_CODES:
            if canon in query:
                target = canon
                break
    if not target:
        raise NotImplementedError("local 通道未能从问句识别标的：%s" % query)

    q = _tencent_quote(_TENCENT_CODES[target])
    if not q:
        raise RuntimeError("local 通道取价失败：%s" % target)
    return {"datas": [q], "_source": "local/tencent", "_target": target}


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

def query(text: str, skill_id: str = None, limit: int = 10,
          channel_name: str = None, allow_fallback: bool = None):
    """统一查询入口：按通道路由；http 失败时可选降级 local。

    ⚠️ 第一个参数名用 `text`（而非 `query`）—— 与模块函数 `query` 同名会遮蔽它。

    :param text:        自然语言问句
    :param skill_id:    技能 ID（可用 SKILL_IDS 常量），默认 market
    :param channel_name: 覆盖默认通道
    :param allow_fallback: 覆盖 BEE_FALLBACK_LOCAL
    """
    sid = skill_id or SKILL_IDS["market"]
    ch = (channel_name or channel()).lower()
    if allow_fallback is None:
        allow_fallback = _cfg("BEE_FALLBACK_LOCAL", "0") == "1"

    if ch == "local":
        return _query_local(text, sid, limit)

    if ch == "mcp":
        return call_mcp(sid, {"query": text, "limit": limit})

    # http（默认）
    try:
        return _query_http(text, sid, limit)
    except Exception:
        if allow_fallback:
            return _query_local(text, sid, limit)
        raise


def query_item(text: str, skill_id: str = None, limit: int = 10, **kw):
    """便捷方法：返回 datas 的首项（dict），无结果返回 None。

    与改造前 market_summary.query_item 行为一致，便于平滑替换。

    ⚠️ 参数名不可用 `query` —— 会遮蔽本模块的 `query()` 函数，
       导致 "str object is not callable" 且被 except 静默吞掉。
    """
    try:
        data = query(text, skill_id=skill_id, limit=limit, **kw)
    except Exception:
        return None
    datas = (data or {}).get("datas") or []
    return datas[0] if datas else None


# --------------------------------------------------------------------------
# 健康检查
# --------------------------------------------------------------------------

def health() -> dict:
    """探测各通道可用性，返回结构化结果（用于部署自检 / 监控）。"""
    out = {"channel": channel(), "gateway": gateway_url(), "checks": {}}

    # http
    t0 = time.time()
    try:
        _query_http("上证指数最新点位", SKILL_IDS["index"], limit=1, retries=0)
        out["checks"]["http"] = {"ok": True, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        out["checks"]["http"] = {"ok": False, "error": str(e)[:200]}

    # local
    t0 = time.time()
    try:
        r = _query_local("上证指数", SKILL_IDS["index"])
        out["checks"]["local"] = {"ok": True, "ms": int((time.time() - t0) * 1000),
                                  "price": (r.get("datas") or [{}])[0].get("最新价")}
    except Exception as e:
        out["checks"]["local"] = {"ok": False, "error": str(e)[:200]}

    # mcp（仅在配置了 endpoint 时探测）
    if mcp_endpoint():
        t0 = time.time()
        try:
            tools = list_mcp_tools()
            out["checks"]["mcp"] = {"ok": True, "ms": int((time.time() - t0) * 1000),
                                    "tools": [t.name for t in (tools.tools or [])][:20]}
        except Exception as e:
            out["checks"]["mcp"] = {"ok": False, "error": str(e)[:200]}
    else:
        out["checks"]["mcp"] = {"ok": False, "error": "未配置 BEE_MCP_ENDPOINT"}

    out["ok"] = any(c.get("ok") for c in out["checks"].values())
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="蜜蜂能力统一适配器")
    ap.add_argument("--query", help="自然语言查询")
    ap.add_argument("--skill-id", default=SKILL_IDS["market"], help="技能 ID")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--channel", choices=["http", "mcp", "local"], help="覆盖默认通道")
    ap.add_argument("--health", action="store_true", help="探测各通道可用性")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    if args.health:
        h = health()
        print(json.dumps(h, ensure_ascii=False, indent=2))
        return 0 if h["ok"] else 1

    if not args.query:
        ap.print_help()
        return 1

    try:
        data = query(args.query, skill_id=args.skill_id,
                     limit=args.limit, channel_name=args.channel)
    except Exception as e:
        print("[ERROR] %s" % e, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        for d in (data or {}).get("datas") or []:
            print(json.dumps(d, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

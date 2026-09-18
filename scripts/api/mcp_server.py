#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 服务端：把平台能力以 MCP tools 暴露给任意支持 MCP 的 Agent。

支持客户端
----------
- **WorkBuddy**：connectors 原生支持 `streamable-http`（见 ~/.workbuddy/connectors/*/mcp.json）
- **蜜蜂 Bee**：`mcp_manage` 支持 stdio / streamable-http / sse
- **Claude Code / Cursor / 其他**：任何兼容 MCP 的客户端

启动
----
    pip install "mcp[cli]"

    # stdio（本地 Agent 直接拉起，最简）
    python -m api.mcp_server --stdio

    # streamable-http（跨机器 / 多 Agent 共享）
    python -m api.mcp_server --http --port 8001

接入示例
--------
蜜蜂：
    mcp_manage(action="upsert", name="kol-platform",
               transport="streamable-http", url="http://<server>:8001/mcp")

WorkBuddy：
    在 connectors 的 mcp.json 中增加
    {"mcpServers": {"kol-platform": {"type": "streamableHttp",
      "url": "http://<server>:8001/mcp", "timeout": 30000}}}

设计
----
MCP tool 与 REST 共用 `services.py` 门面 —— **业务逻辑只有一份**。
本文件只负责：定义 tool schema、调用门面、把异常转成 MCP 错误。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# MCP SDK 版本兼容
#   mcp 1.x : mcp.server.fastmcp.FastMCP
#   mcp 2.x : mcp.server.mcpserver.MCPServer（FastMCP 已改名）
# 两者 `tool()` 装饰器与 `run(transport=...)` 用法一致，故统一为 MCPServer 别名。
# ---------------------------------------------------------------------------
ServerClass = None
_MCP_IMPORT_ERR = ""

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as ServerClass  # type: ignore
except ImportError:
    try:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as ServerClass  # type: ignore
    except Exception as e:
        _MCP_IMPORT_ERR = str(e)

try:
    from . import config, services
except ImportError:
    import config
    import services


def build_server():
    """构造 MCP Server 实例并注册全部 tools（兼容 mcp 1.x / 2.x）。"""
    if ServerClass is None:
        raise RuntimeError(
            "未安装可用的 mcp SDK。请执行：pip install \"mcp\"\n"
            "  错误详情: %s\n"
            "（REST 接口不受影响，可继续使用 python -m api.rest_app）" % (_MCP_IMPORT_ERR or "未知"))

    mcp = ServerClass("kol-skills-platform")

    # ---------------- KOL 能力 ----------------

    @mcp.tool()
    def kol_list() -> list:
        """列出已收录的财经大V及其言论记录数。"""
        return services.list_kols()

    @mcp.tool()
    def kol_records(kol_name: str = "", days: int = 30, latest: int = 0,
                    vip_only: bool = False, all_time: bool = False) -> list:
        """查询大V言论记录（时间倒序）。

        Args:
            kol_name: 大V名称，如 wu2198；留空为全部
            days: 查询最近 N 天，默认 30
            latest: 只取最新 N 条，0 表示不限
            vip_only: 仅返回付费会员专属消息（VIP，权重最高）
            all_time: 忽略 days，返回全部历史
        """
        return services.query_records(kol_name=kol_name, days=days, latest=latest,
                                      vip_only=vip_only, all_time=all_time)

    @mcp.tool()
    def kol_summary(kol_name: str) -> dict:
        """大V数据概览：总量/VIP 条数/时间范围/最新仓位/关联资产。"""
        return services.summary(kol_name)

    @mcp.tool()
    def kol_accuracy(kol_name: str) -> dict:
        """预测准确率报告：命中/偏差/错误统计与逐条明细。"""
        return services.accuracy_report(kol_name)

    @mcp.tool()
    def kol_predictions(kol: str = "") -> str:
        """列出预测记录（可筛选大V）。"""
        return services.list_predictions(kol)

    @mcp.tool()
    def kol_add_prediction(kol: str, pred: str, type: str = "点位",
                           target: str = "", dir: str = "", date: str = "") -> str:
        """登记一条新的预测，便于后续验证准确率。

        Args:
            kol: 大V名称
            pred: 预测内容描述
            type: 类型（点位/方向/板块）
            target: 目标点位或标的
            dir: 方向（看涨到/看跌到）
            date: 预测日期 YYYY-MM-DD
        """
        return services.add_prediction(kol, pred, type, target, dir, date)

    @mcp.tool()
    def kol_compare(kols: list = None) -> str:
        """多KOL观点对比（不传则对比全部）。"""
        return services.compare_kols(kols)

    @mcp.tool()
    def kol_backtest(strategy: str = "half") -> str:
        """跟单回测。

        Args:
            strategy: full=满仓跟（6米=100%）/ half=半仓跟（6米=50%）
        """
        return services.backtest(strategy)

    # ---------------- 行情能力 ----------------

    @mcp.tool()
    def levels(index: str = "", price: float = None) -> str:
        """关键点位监控。

        Args:
            index: 指数名称，如 创业板指/上证指数
            price: 当前价（提供则计算各关键位距离）
        """
        return services.levels(index, price)

    @mcp.tool()
    def market_summary(period: str = "premarket") -> str:
        """行情汇总播报。

        Args:
            period: premarket=盘前 / intraday=盘中
        """
        return services.market_summary(period)

    @mcp.tool()
    def quote(text: str, channel: str = "") -> dict:
        """查询标的行情。

        Args:
            text: 标的问句，如 "上证指数最新点位"
            channel: 数据通道 http=蜜蜂网关 / local=公开行情源（不依赖蜜蜂）
        """
        return services.quote(text, channel)

    # ---------------- 系统能力 ----------------

    @mcp.tool()
    def bee_health() -> dict:
        """蜜蜂通道健康检查（http/local/mcp 三通道可用性）。"""
        return services.bee_health()

    @mcp.tool()
    def alert_status() -> dict:
        """当前告警触发状态与已配置的价格提醒。"""
        return services.alert_status()

    @mcp.tool()
    def capabilities() -> list:
        """列出本平台对外提供的全部能力。"""
        return services.capabilities()

    return mcp


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="KOL Skills Platform MCP 服务端")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--stdio", action="store_true", help="stdio 传输（本地 Agent 拉起）")
    mode.add_argument("--http", action="store_true", help="streamable-http 传输（跨机/多 Agent）")
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--port", type=int, default=config.MCP_PORT)
    args = ap.parse_args()

    try:
        mcp = build_server()
    except RuntimeError as e:
        print("[mcp] %s" % e, file=sys.stderr)
        return 1

    if args.http:
        # FastMCP 的 streamable-http 传输
        try:
            mcp.settings.host = args.host
            mcp.settings.port = args.port
            print("[mcp] streamable-http 监听 http://%s:%s/mcp" % (args.host, args.port))
            mcp.run(transport="streamable-http")
        except TypeError:
            # 兼容旧版 SDK 参数名
            print("[mcp] streamable-http 监听 http://%s:%s" % (args.host, args.port))
            mcp.run(transport="sse")
    else:
        print("[mcp] stdio 传输就绪", file=sys.stderr)
        mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

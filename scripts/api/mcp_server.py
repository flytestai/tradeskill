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

import functools
import json
import os
import sys
import traceback

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
    from . import auth, config, services
except ImportError:
    import auth
    import config
    import services


# ---------------------------------------------------------------------------
# 异常翻译：保证「真实失败原因」一定能到达客户端
#
# 背景（实测血案 2026-09-19）
# --------------------------
# MCP 2.x SDK 对**未预料异常**统一脱敏：
#     except Exception as exc:
#         raise UnexpectedToolError(f"Error executing tool {self.name}") from exc
# 客户端只能看到一句 `Error executing tool <name>`，真实堆栈只进服务端日志。
# 结果：服务器上 18 个工具全挂，客户端侧却完全看不出「缺脚本 / 缺依赖 /
# 权限不足 / 数据库打不开」中的哪一种 —— 故障静默数小时。
#
# 对策：所有 tool 一律包一层 `_guard`，把任何异常转成 `ToolError`。
# `ToolError` 是 SDK 认定的「可预期失败」，其文案会**原样**回给客户端。
# 同时把完整堆栈打到 stderr，服务端日志同样保留。
# ---------------------------------------------------------------------------
try:
    from mcp.server.mcpserver.exceptions import ToolError as _ToolError  # mcp 2.x
except ImportError:  # pragma: no cover - mcp 1.x
    try:
        from mcp.server.fastmcp.exceptions import ToolError as _ToolError  # type: ignore
    except ImportError:
        class _ToolError(Exception):
            """mcp SDK 不可用时的兜底：普通异常，文案仍可见。"""


def _guard(fn):
    """把 tool 内的任何异常翻成「客户端可见」的失败原因。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _ToolError:
            raise
        except services.ServiceError as e:
            # 业务失败：用户可读的原因，直接回给客户端
            print("[mcp][tool-error] %s: %s" % (fn.__name__, e), file=sys.stderr)
            raise _ToolError("执行失败：%s" % e)
        except Exception as e:
            # 兜底：把类型与摘要带上，绝不吞成通用文案
            tb = traceback.format_exc()
            print("[mcp][tool-crash] %s\n%s" % (fn.__name__, tb), file=sys.stderr)
            raise _ToolError("内部错误 %s: %s" % (type(e).__name__, str(e)[:300] or "(无信息)"))
    return wrapper


def _threads_available() -> tuple:
    """探测本进程能否创建线程。返回 (可用, 错误说明)。

    为什么必须在启动时探测（2026-09-19 根因，已在本地确证）
    ------------------------------------------------------
    目标宿主的容器**无法创建线程**（项目早已记录：README 写「该宿主容器
    无法创建线程，已自适应」、Dockerfile 写「实测抛 can't start new thread」）。
    REST 因此被降级为 `threaded=False` 绕开了这个限制。

    但 MCP 侧踩了同一个坑，且**静默**：mcp SDK 对同步 tool 的执行方式是

        # mcp/server/mcpserver/utilities/func_metadata.py
        return await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))

    即「同步函数 → 丢到工作线程跑」。容器建不了线程 → 抛 RuntimeError →
    被 SDK 的裸 `except Exception` 吞成 `Error executing tool <name>`。

    这**精确解释**了观测到的全部现象：
      · 18 个同步 tool 全军覆没、返回同构文案
      · `initialize` / `tools/list`（异步、不走线程）正常
      · 同容器 REST 正常（已降级 threaded=False）

    本地复现（把 anyio.to_thread.run_sync 换成抛异常）：
        ❌ UnexpectedToolError: Error executing tool capabilities
           真实原因(__cause__): RuntimeError: can't start new thread
    与服务器 15 个工具的报错**逐字一致**。
    """
    import threading
    try:
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join(timeout=5)
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def _patch_inline_threads() -> bool:
    """无可用线程时，让 anyio 的 to_thread 就地执行（同步）。返回是否打了补丁。

    取舍：这会把阻塞式工作放在事件循环线程上，MCP 请求因此**串行化**。
    但这是「能用」与「整个端点不可用」之间的选择，且该容器本就无法并发；
    MCP 属低频端点，串行化的代价远小于全量失效。
    """
    try:
        import anyio.to_thread as _tt
    except Exception as e:
        print("[mcp][threads] 无法导入 anyio.to_thread: %s" % e, file=sys.stderr)
        return False

    if getattr(_tt, "_kol_inline_patched", False):
        return True

    async def _run_sync_inline(func, *args, **kwargs):
        # anyio 的签名是 run_sync(func, *args, abandon_on_cancel=..., limiter=...)
        kwargs.pop("abandon_on_cancel", None)
        kwargs.pop("limiter", None)
        kwargs.pop("cancellable", None)
        return func(*args, **kwargs)

    _tt.run_sync = _run_sync_inline
    _tt._kol_inline_patched = True
    return True


class RequireAuthMiddleware:
    """MCP 端点鉴权（ASGI 中间件）：堵住「公网裸奔」，但不误伤内网调用。

    问题（2026-09-19 实测）
    ----------------------
        无 Key / 伪造 X-API-Key / 伪造 Bearer → 全部 HTTP 200
    而同容器 REST（/api/v1/kol/list）无 Key 返回 401 —— 同一个服务两套标准。
    /healthz 还自述 `auth_enabled: true`，与实际行为矛盾。

    设计取舍
    --------
    现有客户端（蜜蜂网关等）在此端点上**不发凭据**，直接强制鉴权会连它们
    一起打死 —— 2026-09-19 刚发生过一次同类「修一个坏一个」的事故。
    故按来源分级：

        · 回环 / 私有网段（Nginx 反代来自 127.0.0.1）→ 放行，仅记录
        · 其他来源（公网直连）                        → 必须带有效 Key，否则 401

    公网经 Nginx 反代进来属于前者，因此**要真正在公网生效，还需在 Nginx
    层把凭据透传并叠加 IP 限制**（见 scripts/deploy/nginx-skill.conf）。
    本中间件至少保证：**任何能直连到本端口的外部来源都不再裸奔**。
    如需对所有来源强制鉴权（Nginx 属回环也会被拦）：设
    PLATFORM_MCP_AUTH_STRICT=1。
    """

    def __init__(self, app, strict: bool = False):
        self.app = app
        self.strict = strict

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        if not config.API_KEYS:
            # 未配置任何 Key → 无处可校验，保持原状（并在启动日志中告警）
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        client = (scope.get("client") or ["", 0])[0]
        # X-Forwarded-For 首段才是真实客户端（Nginx 反代场景）
        fwd = (headers.get("x-forwarded-for") or "").split(",")[0].strip()
        real_ip = fwd or client

        from_local = auth.is_local_client(real_ip)
        if from_local and not self.strict:
            # 内网/回环：放行（Nginx 反代即属此类）
            has_key = bool(auth.extract_key(headers))
            if not has_key:
                print("[mcp][auth] 放行内网来源 %s（未带凭据，非严格模式）" % real_ip,
                      file=sys.stderr)
            return await self.app(scope, receive, send)

        # 公网来源（或严格模式）→ 必须带有效 Key
        try:
            auth.resolve_key(auth.extract_key(headers))
        except auth.AuthError as e:
            print("[mcp][auth] 拒绝 %s：%s" % (real_ip, e), file=sys.stderr)
            body = json.dumps({"ok": False, "error": {
                "type": "auth", "message": str(e),
                "hint": "MCP 端点需要 X-API-Key 或 Authorization: Bearer <key>"}},
                ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode()),
                                    (b"www-authenticate", b'Bearer realm="kol-platform"')]})
            await send({"type": "http.response.body", "body": body})
            return

        return await self.app(scope, receive, send)


def run_http(mcp, host: str, port: int) -> int:
    """以 streamable-http 启动，并挂上鉴权中间件。

    优先用 `streamable_http_app()` + uvicorn.run 自行启动：
    SDK 的 `run(transport=...)` 不暴露中间件注入点，无法加鉴权。
    若该 API 在当前 SDK 版本不可用，则回退到 `run()`（鉴权缺失，
    但明确打印告警 —— 绝不静默降级）。
    """
    strict = (os.environ.get("PLATFORM_MCP_AUTH_STRICT") or "").strip() in ("1", "true", "yes")
    path = "/mcp"

    if hasattr(mcp, "streamable_http_app"):
        try:
            import uvicorn
            # 注意：host 需传给 streamable_http_app ——
            # 传 127.0.0.1 时会自动开启 DNS rebinding 保护并要求 Host 匹配；
            # 对外服务必须传真实绑定地址，否则反代会被 421 拒绝。
            app = mcp.streamable_http_app(host=host)
            app.add_middleware(RequireAuthMiddleware, strict=strict)
            if not config.API_KEYS:
                print("[mcp][auth] ⚠️ 未配置 PLATFORM_API_KEYS —— "
                      "MCP 端点无任何鉴权，请仅在可信网络暴露", file=sys.stderr)
            else:
                print("[mcp][auth] 已启用分级鉴权（严格模式=%s，共 %d 个 Key）"
                      % (strict, len(config.API_KEYS)), file=sys.stderr)
            print("[mcp] streamable-http 监听 http://%s:%s%s" % (host, port, path))
            uvicorn.run(app, host=host, port=port, log_level="info")
            return 0
        except Exception as e:
            print("[mcp][auth] ⚠️ 无法注入鉴权中间件（%s: %s），回退 SDK run() —— "
                  "**该端点将无鉴权**" % (type(e).__name__, e), file=sys.stderr)

    # 回退：原 SDK 启动方式
    print("[mcp] 回退 SDK run(transport=streamable-http)")
    try:
        mcp.run(transport="streamable-http", host=host, port=port)
    except TypeError as e:
        print("[mcp] run(kwargs) 不被支持(%s)，回退 settings 方式" % e)
        try:
            mcp.settings.host = host
            mcp.settings.port = port
        except Exception:
            pass
        mcp.run(transport="streamable-http")
    return 0


def build_server():
    """构造 MCP Server 实例并注册全部 tools（兼容 mcp 1.x / 2.x）。"""
    if ServerClass is None:
        raise RuntimeError(
            "未安装可用的 mcp SDK。请执行：pip install \"mcp\"\n"
            "  错误详情: %s\n"
            "（REST 接口不受影响，可继续使用 python -m api.rest_app）" % (_MCP_IMPORT_ERR or "未知"))

    # ★ 线程可用性处理（必须在注册/运行 tool 之前完成）
    #   目标容器无法创建线程，而 SDK 把同步 tool 丢到 anyio 工作线程执行
    #   → 原本会让**每一个同步 tool 全部失败且原因被脱敏**（详见 _threads_available）。
    ok, why = _threads_available()
    if ok:
        print("[mcp][threads] 线程可用 —— 同步 tool 走 anyio 工作线程")
        if os.environ.get("PLATFORM_MCP_FORCE_INLINE") in ("1", "true", "yes"):
            _patch_inline_threads()
            print("[mcp][threads] 已按 PLATFORM_MCP_FORCE_INLINE 强制就地执行")
    else:
        patched = _patch_inline_threads()
        print("[mcp][threads] ⚠️ 本进程**无法创建线程**（%s）—— 已%s" % (
            why, "改为就地执行（请求将串行处理）" if patched else "尝试打补丁但失败"))
        if not patched:
            print("[mcp][threads] 🔴 所有同步 tool 将不可用！", file=sys.stderr)

    mcp = ServerClass("kol-skills-platform")

    # ---------------- KOL 能力 ----------------

    @mcp.tool()
    @_guard
    def kol_list() -> list:
        """列出已收录的财经大V及其言论记录数。"""
        return services.list_kols()

    @mcp.tool()
    @_guard
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
    @_guard
    def kol_summary(kol_name: str) -> dict:
        """大V数据概览：总量/VIP 条数/时间范围/最新仓位/关联资产。"""
        return services.summary(kol_name)

    @mcp.tool()
    @_guard
    def kol_accuracy(kol_name: str) -> dict:
        """预测准确率报告：命中/偏差/错误统计与逐条明细。"""
        return services.accuracy_report(kol_name)

    @mcp.tool()
    @_guard
    def kol_predictions(kol: str = "") -> str:
        """列出预测记录（可筛选大V）。"""
        return services.list_predictions(kol)

    @mcp.tool()
    @_guard
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
    @_guard
    def kol_compare(kols: list = None) -> str:
        """多KOL观点对比（不传则对比全部）。"""
        return services.compare_kols(kols)

    @mcp.tool()
    @_guard
    def kol_backtest(strategy: str = "half") -> str:
        """跟单回测。

        Args:
            strategy: full=满仓跟（6米=100%）/ half=半仓跟（6米=50%）
        """
        return services.backtest(strategy)

    # ---------------- 行情能力 ----------------

    @mcp.tool()
    @_guard
    def levels(index: str = "", price: float = None) -> str:
        """关键点位监控。

        Args:
            index: 指数名称，如 创业板指/上证指数
            price: 当前价（提供则计算各关键位距离）
        """
        return services.levels(index, price)

    @mcp.tool()
    @_guard
    def market_summary(period: str = "premarket") -> str:
        """行情汇总播报。

        Args:
            period: premarket=盘前 / intraday=盘中
        """
        return services.market_summary(period)

    @mcp.tool()
    @_guard
    def quote(text: str, channel: str = "") -> dict:
        """查询标的行情。

        Args:
            text: 标的问句，如 "上证指数最新点位"
            channel: 数据通道 http=蜜蜂网关 / local=公开行情源（不依赖蜜蜂）
        """
        return services.quote(text, channel)

    # ---------------- 系统能力 ----------------

    @mcp.tool()
    @_guard
    def bee_health() -> dict:
        """蜜蜂通道健康检查（http/local/mcp 三通道可用性）。"""
        return services.bee_health()

    @mcp.tool()
    @_guard
    def alert_status() -> dict:
        """当前告警触发状态与已配置的价格提醒。"""
        return services.alert_status()

    # ---------------- LLM（Kimi）能力 ----------------

    @mcp.tool()
    @_guard
    def llm_ask(question: str, context: str = "") -> dict:
        """调用 Kimi 大模型回答问题。

        适用于需要自然语言推理的场景（如「这个位置还能建仓吗」）。
        context 可放入平台数据（行情/言论/关键位），让回答更贴合实际。

        Args:
            question: 用户问题
            context:  可选的数据上下文（平台查询结果）
        """
        return services.llm_ask(question, context)

    @mcp.tool()
    @_guard
    def llm_summarize(content: str, instruction: str = "") -> dict:
        """对给定内容做归纳解读（如把分析报告浓缩成要点）。

        Args:
            content:     待归纳的文本
            instruction: 自定义指令（留空用默认：3-5 条要点 + 风险提示）
        """
        return services.llm_summarize(content, instruction)

    @mcp.tool()
    @_guard
    def llm_status() -> dict:
        """查看 LLM（Kimi）配置状态（provider/model/是否已配置）。"""
        return services.llm_status()

    @mcp.tool()
    @_guard
    def qa_queue_status() -> dict:
        """查看群问答待处理队列（有多少条 @机器人 的问题排队）。"""
        return services.qa_queue_status()

    @mcp.tool()
    @_guard
    def capabilities() -> str:
        """列出本平台对外提供的全部能力。

        返回 JSON 文本而非 list：MCP 2.x 对 list 返回值会包成结构化内容，
        以文本返回可保证任何客户端（含只读 content[0].text 的）都能正确解析。
        """
        import json as _json
        return _json.dumps(services.capabilities(), ensure_ascii=False, indent=2)

    @mcp.tool()
    @_guard
    def selfcheck() -> dict:
        """平台自检：解释器 / 脚本目录 / 数据库 / 依赖是否就绪。

        排查「MCP 工具集体失败」的**第一站**：
          · 本工具也失败  → 问题在容器本身（镜像/挂载/权限）
          · 本工具正常而其他失败 → 对比各项结果即可定位到具体缺口
        """
        return services.selfcheck()

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

    # 启动自检：把「容器里到底缺什么」在启动时就说清楚。
    # 实测血案：此前 MCP 全量故障时，日志里只有一句 `Error executing tool`，
    # 无法判断是缺脚本、缺依赖还是权限问题 —— 这里提前暴露。
    try:
        _sc = services.selfcheck()
        print("[mcp] 自检 python=%s(%s) scripts=%s(db=%s) db=%s deps=%s ok=%s"
              % (_sc["python"]["version"], _sc["python"]["exists"],
                 _sc["scripts"]["count"], _sc["scripts"]["missing"] or "齐备",
                 _sc["database"]["readable"], _sc["deps"], _sc["ok"]))
        if not _sc["ok"]:
            print("[mcp] ⚠️ 自检未通过：%s" % json.dumps(_sc, ensure_ascii=False), file=sys.stderr)
    except Exception as e:
        print("[mcp] 自检执行失败: %s: %s" % (type(e).__name__, e), file=sys.stderr)

    if args.http:
        # streamable-http 传输（含鉴权中间件），见 run_http()。
        # 历史注意：host/port 早先必须作为 run() 的 kwargs 传入 ——
        #   mcp 2.x (MCPServer) 的 Settings 已无 host/port 字段，
        #   赋值会抛 ValueError("no field 'host'")。
        return run_http(mcp, args.host, args.port)
    else:
        print("[mcp] stdio 传输就绪", file=sys.stderr)
        mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

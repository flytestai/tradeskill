---
name: kol-platform
description: >
  查询财经大V（KOL）言论库与分析结果。支持：大V言论检索（含 VIP 专属）、
  预测准确率统计、多KOL观点对比、跟单回测、关键点位监控、行情查询与汇总播报。
  所有能力由远端 KOL Skills Platform 提供（REST + MCP）。
  Use when users 询问某个大V的最新观点、准确率、仓位变化，
  或需要 KOL 言论/ETF 建议/点位提醒相关的数据查询。
---

# KOL 平台控制台（薄 skill）

## 黄金规则（必须遵守）

1. **只调后端，不重算**：KOL Skills Platform 是唯一数据源与计算源。
   本 skill 只做「自然语言 → 命令 → 调接口 → 紧凑回复」。
2. **禁止**在 skill 内重写分析逻辑、直接读 SQLite、或自行拉行情接口。
3. **禁止**把后端返回的完整 JSON 原样贴给用户 —— 必须压缩成简洁结论。

## 连接配置

```bash
export KOL_PLATFORM_URL="http://127.0.0.1:8000"   # 或远端 https://your-host
export KOL_PLATFORM_KEY="<API Key>"
```

## 快速执行

```bash
# 列出已收录的大V
python scripts/query.py "有哪些大V"

# 查最新观点（默认近 30 天，时间倒序）
python scripts/query.py "wu2198 最新观点"
python scripts/query.py "wu2198 最近3天"

# 仅看 VIP 消息（付费会员专属，权重最高；公开消息从不透露仓位）
python scripts/query.py "wu2198 VIP 消息"

# 准确率统计
python scripts/query.py "wu2198 准确率"

# 数据概览（总量/VIP/最新仓位/关联资产）
python scripts/query.py "wu2198 概览"

# 多KOL对比 / 跟单回测
python scripts/query.py "对比所有大V"
python scripts/query.py "半仓跟单回测 wu2198"

# 关键点位
python scripts/query.py "创业板指点位 3368"

# 行情
python scripts/query.py "上证指数行情"
python scripts/query.py "盘前播报"
```

## 能力清单（对应后端 REST 接口）

| 命令意图 | 后端接口 |
|---|---|
| 列出大V | `GET /api/v1/kol/list` |
| 查询言论 | `GET /api/v1/kol/records?kol_name=&days=&latest=&vip_only=` |
| 数据概览 | `GET /api/v1/kol/summary?kol_name=` |
| 准确率 | `GET /api/v1/kol/accuracy?kol_name=` |
| 预测列表/新增 | `GET/POST /api/v1/kol/predictions` |
| 多KOL对比 | `GET /api/v1/kol/compare?kol=a&kol=b` |
| 跟单回测 | `GET /api/v1/kol/backtest?strategy=full\|half` |
| 关键点位 | `GET /api/v1/levels?index=&price=` |
| 行情汇总 | `GET /api/v1/market/summary?period=premarket\|intraday` |
| 行情查询 | `GET /api/v1/market/quote?query=&channel=http\|local` |
| 提醒状态 | `GET /api/v1/system/alerts` |
| 健康检查 | `GET /healthz`（无需鉴权） |

完整字段说明见 `references/api.md`。

## 通过 MCP 调用（推荐，跨 Agent 通用）

若 Agent 支持 MCP，直接连接平台 MCP 端点，无需本 skill：

```
MCP 端点: http://<host>:8001/mcp      传输: streamable-http
```

暴露 14 个 tools：`kol_list` / `kol_records` / `kol_summary` / `kol_accuracy` /
`kol_predictions` / `kol_add_prediction` / `kol_compare` / `kol_backtest` /
`levels` / `market_summary` / `quote` / `bee_health` / `alert_status` / `capabilities`

蜜蜂：
```
mcp_manage(action="upsert", name="kol-platform",
           transport="streamable-http", url="http://<host>:8001/mcp")
```

WorkBuddy：在 `~/.workbuddy/connectors/*/mcp.json` 的 `mcpServers` 增加
```json
{"kol-platform": {"type": "streamableHttp",
                  "url": "http://<host>:8001/mcp", "timeout": 30000}}
```

## 回复规范

- 紧凑输出：每条言论一行（时间 + 层级 + 摘要）
- 涉及 VIP 消息标注 `🔒 VIP`
- 涉及仓位/操作标注 `🔒 仅 VIP`
- **必须**在结论末尾追加：`⚠️ 本内容由 AI 生成，仅供参考，不构成投资建议。`

## 资源

- `scripts/query.py` —— 薄 wrapper：自然语言 → REST 调用（不重算）
- `references/api.md` —— 接口字段与响应结构

# kol-platform · 财经大V + 行情数据平台（跨平台接入）

一个远程 **MCP 服务器**，把「财经大V言论、观点准确率、指数/个股行情、盘前播报/收盘复盘、
价格提醒、关键位、持仓监控」等能力，以 **19 个 MCP 工具**的形式开放给任意 Agent 调用。

已支持接入：**Bee / Claude Code / Cursor / Codex / WorkBuddy / Hermes**。

> ⚠️ 先厘清一个常见困惑：**「为什么装了 skill 却看不到？」**
>
> `kol-platform` 是 **MCP 服务器（工具型）**，不是 `SKILL.md` 指令型技能。它出现在各平台的
> **MCP 连接/工具**里，而不是「技能/Skill」列表里。只要 MCP 配置连上了，工具就对 Agent 可见。
> 本仓库根目录的 `SKILL.md`（`kol-opinion-analyzer`）是**服务器端**分析机器人的技能说明书，与客户接入无关。

---

## 一、接入三要素（各平台通用）

| 参数 | 值 |
|------|----|
| 服务器名 | `kol-platform` |
| 传输协议 | `streamable-http` |
| 端点 URL | `https://skill.flytest.com.cn/mcp` |
| 鉴权头 | `X-API-Key: <你的API Key>`（或 `Authorization: Bearer <key>`） |

> 服务器做**分级鉴权**：内网/回环放行；公网必须带有效 Key。跨机器接入时 **Key 是必须的**。

---

## 二、一键安装（无需下载 zip，所有平台统一命令）

发下面这一条命令给客户即可——**不需要指定平台**，脚本会自动检测本机已安装的平台
（WorkBuddy / Hermes / Bee / Codex / Claude Code）并全部接入。把 `kol-xxx` 换成客户的密钥。

```bash
# 统一命令：只传密钥，自动装到本机所有可用平台
bash <(curl -fsSL https://raw.githubusercontent.com/flytestai/tradeskill/main/install.sh) kol-xxx
```

也可以先下载脚本再运行：

```bash
curl -fsSL https://raw.githubusercontent.com/flytestai/tradeskill/main/install.sh -o install.sh
bash install.sh kol-xxx
```

安装时脚本会自动：
- **WorkBuddy / Hermes / Bee**：直接写入各自配置目录；
- **Codex / Claude Code**：检测到 `codex` / `claude` 命令就自动注册，检测不到则跳过并提示手动配置。

---

## 三、手动配置（可选）

不想用脚本的，把 `config/` 下对应平台的 `mcp.json` 内容放进该平台的配置位置即可：

| 平台 | 配置位置 | 用哪个文件 |
|------|----------|-----------|
| Bee | `~/.bee-pc-agent/mcp.json`（Windows 为 `C:\Users\<用户>\.bee-pc-agent\mcp.json`） | `config/bee.mcp.json` |
| Claude Code / Cursor | 项目根 `.mcp.json` 或全局 `~/.claude.json` | `config/claude-code.mcp.json` |
| Codex | `~/.codex/config.toml` | `config/codex.config.toml`（走 Bearer 认证，需另设 `export KOL_API_KEY=<key>`） |
| WorkBuddy | `~/.workbuddy/connectors/kol-platform/mcp.json` | `config/workbuddy.mcp.json` |
| Hermes | `~/.hermes/config.yaml` | `config/hermes.config.yaml` |

> 记得把配置里的 `<你的API Key>` 换成真实密钥。

---

## 四、密钥怎么来（多租户多密钥）

管理员在服务器上签发独立密钥（**明文只显示一次**）：

```bash
cd /opt/kol-skills-platform
python3 scripts/api_keys.py issue --label 张三 --tenant default --scopes '*'
```

- 列出 / 吊销 / 校验：
  ```bash
  python3 scripts/api_keys.py list
  python3 scripts/api_keys.py revoke --label 张三
  python3 scripts/api_keys.py check <key>
  ```
- 密钥只存 **SHA-256 哈希**，新增/吊销**免重启**，最迟 30 秒生效。
- 建议**一人/一平台一把独立密钥**，便于审计与单独吊销。
- ⚠️ **密钥绝不明文写入仓库**：管理员密钥只存于服务器 `.env`（已 gitignore），本仓库不包含任何真实密钥。

---

## 五、19 个工具一览

| 分类 | 工具 | 说明 |
|------|------|------|
| 大V言论 | `kol_list` | 列出已收录的大V及记录数 |
| | `kol_records` | 查询大V言论（近N天/全部/VIP） |
| | `kol_summary` | 大V数据概览（总量/VIP/最新仓位/关联资产） |
| | `kol_accuracy` | 预测准确率报告（命中/偏差/错误明细） |
| | `kol_predictions` | 列出预测记录 |
| | `kol_add_prediction` | 新增一条预测记录 |
| | `kol_compare` | 多KOL观点对比 |
| | `kol_backtest` | 跟单回测 |
| 行情市场 | `quote` | 行情查询（可指定 http/local 通道） |
| | `levels` | 关键点位监控（列表/距现价） |
| | `market_summary` | 盘前/盘中行情汇总 |
| 平台状态 | `bee_health` | 蜜蜂通道健康检查 |
| | `alert_status` | 提醒与告警状态 |
| | `qa_queue_status` | 群问答队列状态 |
| | `capabilities` | 列出平台全部能力 |
| | `selfcheck` | 平台自检 |
| LLM 辅助 | `llm_ask` | 调用 Kimi 回答问题（可附平台数据上下文） |
| | `llm_summarize` | 对内容做归纳解读 |
| | `llm_status` | LLM 配置状态 |

各工具的参数 schema 在对应平台连接成功后会直接展示，无需额外文档。

---

## 六、验证是否接通

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST https://skill.flytest.com.cn/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'X-API-Key: <你的API Key>' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}'
```

- `200`：密钥有效、已接通
- `401`：Key 缺失/错误/已吊销
- 超时/拒绝：网络或 Nginx 未通

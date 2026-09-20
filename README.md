# kol-platform · 财经大V + 行情数据平台（跨平台接入）

一个远程 **MCP 服务器**，把「财经大V言论、观点准确率、指数/个股行情、盘前播报/收盘复盘、
价格提醒、关键位、持仓监控」等能力，以 **19 个 MCP 工具**的形式开放给任意 Agent 调用。

已支持接入：**Bee / Claude Code / Cursor / Codex / WorkBuddy / Hermes**。

> ⚠️ 先厘清一个常见困惑：**「为什么装了 skill 却看不到？」**
>
> `kol-platform` 是 **MCP 服务器（工具型）**，不是 `SKILL.md` 指令型技能。它出现在各平台的
> **MCP 连接/工具**里，而不是「技能/Skill」列表里。只要 MCP 配置连上了，19 个工具就对 Agent 可见。
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

## 二、一键安装（无需下载 zip）

发一条命令给客户即可。`<platform>` 取 `workbuddy` / `codex` / `hermes` / `claude-code` / `bee`。

```bash
# 直接从 GitHub 拉取并运行（把 kol-xxx 换成客户自己的 Key）
bash <(curl -fsSL https://raw.githubusercontent.com/flytestai/tradeskill/main/install.sh) <platform> <API_KEY>
```

### 各平台示例

```bash
# WorkBuddy
bash <(curl -fsSL https://raw.githubusercontent.com/flytestai/tradeskill/main/install.sh) workbuddy kol-xxxx

# Codex（OpenAI Codex CLI）
bash <(curl -fsSL https://raw.githubusercontent.com/flytestai/tradeskill/main/install.sh) codex kol-xxxx

# Hermes
bash <(curl -fsSL https://raw.githubusercontent.com/flytestai/tradeskill/main/install.sh) hermes kol-xxxx

# Claude Code / Cursor
bash <(curl -fsSL https://raw.githubusercontent.com/flytestai/tradeskill/main/install.sh) claude-code kol-xxxx

# Bee 蜜蜂
bash <(curl -fsSL https://raw.githubusercontent.com/flytestai/tradeskill/main/install.sh) bee kol-xxxx
```

也可以先下载脚本再运行：

```bash
curl -fsSL https://raw.githubusercontent.com/flytestai/tradeskill/main/install.sh -o install.sh
bash install.sh workbuddy kol-xxxx
```

---

## 三、手动配置（可选）

不想用脚本的，把 `config/` 下对应平台的 `mcp.json` 内容放进该平台的配置位置即可：

| 平台 | 配置位置 | 用哪个文件 |
|------|----------|-----------|
| Bee | `~/.bee-pc-agent/mcp.json`（Windows 为 `C:\Users\<用户>\.bee-pc-agent\mcp.json`） | `config/bee.mcp.json` |
| Claude Code / Cursor | 项目根 `.mcp.json` 或全局 `~/.claude.json` | `config/claude-code.mcp.json` |
| Codex | `~/.codex/config.toml` | `config/codex.mcp.json` |
| WorkBuddy | `~/.workbuddy/connectors/kol-platform/mcp.json` | `config/workbuddy.mcp.json` |
| Hermes | `~/.hermes/config.yaml` | `config/hermes.mcp.json` |

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

当前管理员密钥（环境变量）：
`***REMOVED***`

---

## 五、19 个工具一览

| 分类 | 工具 |
|------|------|
| 大V言论 | `kol_list` `kol_records` `kol_summary` `kol_accuracy` `kol_predictions` `kol_add_prediction` `kol_compare` `kol_backtest` |
| 行情市场 | `quote` `levels` `market_summary` |
| 平台状态 | `bee_health` `alert_status` `qa_queue_status` `capabilities` `selfcheck` |
| LLM 辅助 | `llm_ask` `llm_summarize` `llm_status` |

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

---

## 七、服务端信息（给管理员）

| 项 | 值 |
|----|----|
| 服务器 | ***REMOVED***（Ubuntu 22.04，操作用户 ubuntu） |
| 代码目录 | `/opt/kol-skills-platform` |
| REST | `kol-platform.service` → 127.0.0.1:8020 |
| MCP | `kol-platform-mcp.service` → 127.0.0.1:8021 |
| 公网入口 | `https://skill.flytest.com.cn/mcp`（Nginx → 8021） |
| 鉴权模式 | `enforce`（公网必须带 Key） |

重启服务：`sudo systemctl restart kol-platform kol-platform-mcp`

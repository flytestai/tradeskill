# Linux 部署与跨 Agent 接入指南

> 版本：2026-09-18　｜　适用范围：P0–P5（平台兼容 + REST + MCP + 薄壳 + 部署）

---

## 一、改造概览

```
                  ┌──────────────────────────────────────┐
   Agent 层        │ 蜜蜂 Bee │ WorkBuddy │ Claude Code │ ...
                  └────┬──────────┬───────────┬──────────┘
                       │          │           │
              ┌────────┴──────────┴───────────┴────────┐
   接口层      │  REST  /api/v1/*      MCP  /mcp       │
              │  (Flask :8000)      (streamable-http :8001)
              └────────────────┬───────────────────────┘
                               │ 共用 services.py 门面
              ┌────────────────┴───────────────────────┐
   业务层      │  scripts/*.py（原样保留，未改逻辑）      │
              │  bee_client 适配器：http / mcp / local  │
              └────────────────────────────────────────┘
```

**核心原则**：薄接口、厚脚本 —— 平台层不复制任何业务逻辑，
只做「组装命令行参数 → 执行 → 解析 JSON → 协议适配」。

---

## 二、新增/修改文件清单

### 新增

| 文件 | 作用 |
|---|---|
| `scripts/bee_client.py` | 蜜蜂能力统一适配器（http / mcp / local 三通道） |
| `scripts/api/__init__.py` | 平台包说明 |
| `scripts/api/config.py` | 配置加载（环境变量优先，回退 local_config.env） |
| `scripts/api/auth.py` | API Key 鉴权（支持多租户与 scope） |
| `scripts/api/services.py` | 业务门面（调用现有脚本） |
| `scripts/api/rest_app.py` | Flask REST 接口（13 个端点） |
| `scripts/api/mcp_server.py` | MCP 服务端（14 个 tools，兼容 mcp 1.x/2.x） |
| `scripts/deploy/systemd/*.service` | systemd 单元（REST + MCP） |
| `scripts/deploy/install.sh` | 一键部署脚本 |
| `scripts/deploy/thin-skill/` | 薄 skill（SKILL.md + query.py） |

### 修改（均为向后兼容）

| 文件 | 改动 |
|---|---|
| `scripts/common.py` | **追加**平台抽象层（`IS_WINDOWS`/`detach_flags`/`find_lark_cli`/`service_env`…） |
| `scripts/price_alerts.py` | `query_price` 改走 `bee_client`（签名与返回值不变） |
| `scripts/monitor_alerts.py` | `query_index` 同上 |
| `scripts/market_summary.py` | `query_item` 同上 |
| `scripts/notify_feishu.sh` | 跨平台 `resolve_lark()` |
| `scripts/notify_group.sh` | 同上 |

> **Windows 行为完全不变** —— 所有常量用 `getattr(..., 0)`，Linux 上自动为 0。

---

## 三、快速部署

```bash
# 1) 拉取代码（或拷贝到服务器）
sudo git clone https://github.com/flytestai/tradeskill.git /opt/kol-opinion-analyzer

# 2) 一键部署
cd /opt/kol-opinion-analyzer
sudo bash scripts/deploy/install.sh

# 3) 飞书授权（首次）
lark-cli auth login --no-wait --json     # 取验证链接，在浏览器完成
lark-cli auth login --device-code <code>

# 4) 初始化数据库（或从云存档恢复）
python3 scripts/db_init.py
python3 scripts/sync.py pull

# 5) 验证
systemctl status kol-platform kol-platform-mcp
curl -H "X-API-Key: $(grep ^PLATFORM_API_KEYS= /etc/kol-platform/env | cut -d= -f2 | cut -d, -f1)" \
     http://127.0.0.1:8000/healthz
```

---

## 四、配置说明（`/etc/kol-platform/env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PLATFORM_API_KEYS` | 自动生成 | 逗号分隔；完整格式 `key:tenant:scope1\|scope2` |
| `PLATFORM_HOST` / `PORT` | `127.0.0.1` / `8000` | REST 监听 |
| `PLATFORM_MCP_PORT` | `8001` | MCP 监听 |
| `BEE_CHANNEL` | `http` | 数据通道：`http`(蜜蜂网关) / `local`(公开行情源) / `mcp` |
| `BEE_FALLBACK_LOCAL` | `0` | 蜜蜂不可达时自动降级到公开源（**剥离蜜蜂时置 1**） |
| `BEE_GATEWAY_URL` | 蜜蜂官方 | 网关地址 |
| `BEE_MCP_ENDPOINT` | 空 | 蜜蜂的 MCP 端点（用 mcp 通道时必填） |
| `LARK_CLI` | 自动查找 | lark-cli 路径 |

---

## 五、接入各 Agent

### 方式 A：MCP（推荐，跨 Agent 通用）

**蜜蜂**
```
mcp_manage(action="upsert", name="kol-platform",
           transport="streamable-http", url="http://<host>:8001/mcp")
```

**WorkBuddy** —— 在 `~/.workbuddy/connectors/*/mcp.json` 的 `mcpServers` 增加：
```json
{
  "kol-platform": {
    "type": "streamableHttp",
    "url": "http://<host>:8001/mcp",
    "timeout": 30000
  }
}
```

**Claude Code / Cursor** ——
```bash
claude mcp add --transport http kol-platform http://<host>:8001/mcp
```

### 方式 B：薄 skill（兼容不支持 MCP 的 Agent）

把 `scripts/deploy/thin-skill/` 整个目录拷贝到目标 Agent 的 skills 目录：

```bash
# Bee
cp -r scripts/deploy/thin-skill ~/.bee/plugins/.my-plugin/skills/kol-platform

# WorkBuddy
cp -r scripts/deploy/thin-skill ~/.workbuddy/skills/kol-platform
```

设置环境变量后即可用：
```bash
export KOL_PLATFORM_URL="http://127.0.0.1:8000"
export KOL_PLATFORM_KEY="<API Key>"
python scripts/query.py "wu2198 最新观点"
```

> 两个 Agent 的 `SKILL.md` 格式完全一致，**同一份目录可直接复用**。

### 方式 C：REST（脚本 / cron / CI）

```bash
K=$(grep ^PLATFORM_API_KEYS= /etc/kol-platform/env | cut -d= -f2 | cut -d, -f1)
curl -H "X-API-Key: $K" "http://127.0.0.1:8000/api/v1/kol/records?kol_name=wu2198&days=3"
```

---

## 六、接口一览

### REST（`api/rest_app.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查（含蜜蜂通道探测，无需鉴权） |
| GET | `/api/v1/capabilities` | 能力清单 |
| GET | `/api/v1/kol/list` | 列出大V |
| GET | `/api/v1/kol/records` | 查询言论（`kol_name`/`days`/`latest`/`vip_only`/`all`） |
| GET | `/api/v1/kol/summary` | 数据概览 |
| GET | `/api/v1/kol/accuracy` | 准确率报告 |
| GET/POST | `/api/v1/kol/predictions` | 预测列表 / 新增 |
| GET | `/api/v1/kol/compare` | 多KOL对比 |
| GET | `/api/v1/kol/backtest` | 跟单回测 |
| GET | `/api/v1/levels` | 关键点位 |
| GET | `/api/v1/market/summary` | 行情汇总 |
| GET | `/api/v1/market/quote` | 行情查询（`channel=http|local`） |
| GET | `/api/v1/system/alerts` | 提醒状态 |

### MCP tools（`api/mcp_server.py`，14 个）

`kol_list` / `kol_records` / `kol_summary` / `kol_accuracy` / `kol_predictions` /
`kol_add_prediction` / `kol_compare` / `kol_backtest` / `levels` /
`market_summary` / `quote` / `bee_health` / `alert_status` / `capabilities`

---

## 七、剥离蜜蜂运行（BEE_CHANNEL=local）

若要让数据面完全不依赖蜜蜂：

```bash
# 方式 1：全局走本地源
echo "BEE_CHANNEL=local" >> /etc/kol-platform/env

# 方式 2（推荐）：默认走蜜蜂，不可达时自动降级
echo "BEE_FALLBACK_LOCAL=1" >> /etc/kol-platform/env
```

**已验证**：蜜蜂网关不可达时，`local` 通道能取到**完全一致的价格**
（上证 3911.87，来自腾讯行情源）。

> ⚠️ `local` 通道目前覆盖**指数实时点位**；个股/财务/研报类查询仍走蜜蜂。
> 如需完全脱离，需按标的补充 `bee_client._query_local` 的解析器。

---

## 八、故障排查

| 现象 | 排查 |
|---|---|
| 服务起不来 | `journalctl -u kol-platform -n 50` |
| 401 未授权 | 检查 `X-API-Key` 与 `/etc/kol-platform/env` 是否一致 |
| 502 服务错误 | 平台在调脚本时失败，看 `journalctl` 里的脚本 stderr |
| MCP 连接失败 | 确认已 `pip install mcp`；`systemctl status kol-platform-mcp` |
| 行情取不到 | `curl .../healthz` 看 bee.checks；必要时设 `BEE_FALLBACK_LOCAL=1` |
| 飞书发不出 | 检查 `lark-cli auth status` 与 `LARK_CLI` 环境变量 |

### 关键设计说明：包名冲突

平台包命名为 **`api/`** 而非 `platform/` —— 因为 `platform` 与 Python 标准库同名，
放在 `scripts/` 下会**遮蔽标准库**，导致 `common.py` 的 `platform.system()` 崩溃。
这是本次改造中发现并规避的一个严重坑，后续扩展请勿改回。

---

## 九、安全建议

1. **不要直接暴露 8000/8001** —— 仅绑 `127.0.0.1`，用 Nginx/Caddy 反代 + TLS
2. **生产必须配置 `PLATFORM_API_KEYS`** —— 留空表示不校验（仅适用本机调试）
3. **配置权限 600** —— `/etc/kol-platform/env` 含 API Key 与 chat_id
4. **跨机访问务必 HTTPS** —— WorkBuddy 的 connector 支持 `mcpOAuth`，可对接 OAuth Bearer
5. **引入服务外部的健康监控** —— 避免"服务挂了、告警也发不出"的自举陷阱

---

## 十、回滚

平台层**零侵入**，回滚只需停用服务，现有 Windows 运行完全不受影响：

```bash
sudo systemctl disable --now kol-platform kol-platform-mcp
```

若需回滚代码改动：
```bash
git log --oneline | head            # 找到本次提交
git revert <commit>                 # 或 git checkout <旧版本> -- scripts/
```

平台层与现有系统**可长期并行**：各自独立 SQLite、各自飞书授权，
通过 GitHub `sync/records.jsonl` 共享言论库。


---

## 十一、线上部署信息（2026-09-18）

| 项目 | 值 |
|---|---|
| **REST** | `https://skill.flytest.com.cn` |
| **MCP** | `https://skill.flytest.com.cn/mcp` |
| 已下线旧域 | `etf.flytest.com.cn`（2026-09-18 彻底下线，return 444）|
| 服务器 | 203.0.113.20（Debian 10 / Docker 18.09） |
| 部署目录 | `/opt/kol-skills-platform` |
| 容器 | `kolplatform-rest`（8020）/ `kolplatform-mcp`（8021），仅绑 127.0.0.1 |
| nginx | `/etc/nginx/sites-available/skill-platform`（独立文件） |
| 证书 | Let's Encrypt `skill.flytest.com.cn`，certbot.timer 自动续期 |
| 上游模式 | REST **单线程**（该宿主容器无法创建线程，已自适应） |

### 该环境的特殊适配（详见 Dockerfile 注释）

1. 宿主 Python 3.7 过低 → 全容器化（3.11）
2. Docker 18.09 下 apt 不可用 → 移除 apt，时区用 pip `tzdata`
3. pip 进度条创建线程失败 → `PIP_PROGRESS_BAR=off`
4. 容器无法创建线程 → REST 自动降级 `threaded=False`
5. `api` 包在 scripts/ 下 → 直接执行脚本路径，不用 `-m`

### 运维命令

```bash
ssh root@203.0.113.20 && cd /opt/kol-skills-platform
docker ps --filter name=kolplatform
docker logs -f kolplatform-rest
bash scripts/deploy/start.sh --restart
certbot certificates && certbot renew --dry-run
tail -f /var/log/nginx/skill-platform.error.log
```

### 已下线域名的处理（rejected-domains）

nginx 中若某域名不匹配任何 `server_name`，请求会落到该端口的 **default server**
（实测：etf 下线后 http 落到 cliproxy 的 /management.html、https 落到 flytest 主站），
会**意外暴露无关服务**。

因此用 `sites-available/rejected-domains` 显式 `return 444`（关闭连接不返回内容），
对客户端表现为「该域名无服务」。

新增已下线域名：在该文件的 `server_name` 中追加即可。

```bash
# 回滚 etf 上线（恢复跳转而非拒绝）
cat /root/kol-platform-backup-*/nginx/skill-platform | grep -A20 "旧域跳转"
```

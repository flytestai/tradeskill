# 外部健康监控

> 部署日期：2026-09-18　｜　状态：**✅ 运行中（每 5 分钟探测）**

---

## 一、为什么监控必须跑在服务器之外

这是架构设计中反复强调的「**自举告警陷阱**」：

```
❌ 错误做法：监控放在平台服务器上
   平台进程崩溃 → 监控进程（若同宿主）也可能受影响
                → 即使监控活着，它的告警依赖 lark-cli（也在同一台机器）
                → 结果：服务挂了，却收不到任何告警

✅ 正确做法：监控独立于被监控对象
   你的电脑 ──HTTPS探测──▶ skill.flytest.com.cn
        │                        │
        └──飞书bot告警──▶ 你      └── 挂了完全不影响左边
```

**本方案的监控跑在你的 Windows 电脑上**，通过公网 HTTPS 探测远程平台，
再经飞书 bot 私信告警。两条链路完全独立。

---

## 二、检测项（4 项）

| # | 检测项 | 能发现的问题 |
|---|---|---|
| 1 | REST `/healthz` | 服务进程存活、nginx 反代正常、TLS 正常 |
| 2 | REST `/api/v1/kol/list`（带 Key） | **业务链路**：鉴权、数据库可读、脚本执行 |
| 3 | MCP `/mcp` initialize 握手 | **MCP 通道**单独存活（MCP 挂了 REST 可能仍正常） |
| 4 | TLS 证书剩余天数 | 证书 <14 天预警（**自动续期失败**的早期信号） |

> 第 2 项比 `/healthz` 更有价值 —— `/healthz` 是轻量的（设计上不碰数据库），
> 而业务接口能验出「进程活着但数据库挂了」这类假活。

---

## 三、告警策略（防轰炸）

```
连续失败 1 次  → 记录，不告警（等待确认）
连续失败 2 次  → 记录，不告警
连续失败 3 次  → 🚨 发送故障告警
仍在故障中     → 跳过（不重复告警）
恢复正常       → ✅ 发送「服务已恢复」通知
```

**为什么这样设计**：跨境网络本身有抖动（实测约 19:54 出现过一次瞬时失败），
若首次失败就告警会产生大量误报。阈值默认 3 次（可通过
`MONITOR_FAIL_THRESHOLD` 调整）。

**恢复通知同样重要** —— 否则你不知道服务何时自己好了。

### 实测记录（真实发生）

```
19:54:44 ⚠️ 异常: ✅ /healthz | ❌ 业务链路 | ✅ MCP | ✅ 证书
19:54:44   连续失败 1/3 次，暂不告警（等待确认）    ← 防误报生效 ✅
19:59:04 健康检查: ✅ 全部正常                      ← 已自愈
```

---

## 四、告警通道

| 通道 | 状态 | 说明 |
|---|---|---|
| **飞书 bot 私信** | ✅ 已启用 | 经 `lark-cli --as bot`，token 由 app_id/app_secret 自动获取，**无 7 天限制** |
| 飞书群 Webhook | ⏸ 可选 | 设 `MONITOR_WEBHOOK=<群机器人地址>` 即启用，作为第二通道 |

> 💡 服务器上**没有装 lark-cli** 也没关系 —— 本脚本本就不该跑在服务器上。

---

## 五、部署与运维

### 当前部署形态

| 项目 | 值 |
|---|---|
| 运行位置 | Windows（你的电脑） |
| 计划任务名 | `kol-platform-health-monitor` |
| 频率 | **每 5 分钟** |
| 监控地址 | `https://skill.flytest.com.cn` |
| 日志 | `data/_health_monitor.log` |
| 状态 | `data/_health_monitor_state.json` |
| 配置 | `data/monitor.env`（含 API Key，已 gitignore） |

### 常用命令

```powershell
# 查看任务
Get-ScheduledTask -TaskName 'kol-platform-health-monitor' | Select TaskName,State
Get-ScheduledTaskInfo -TaskName 'kol-platform-health-monitor' | Select LastRunTime,LastTaskResult,NextRunTime

# 立即执行一次
Start-ScheduledTask -TaskName 'kol-platform-health-monitor'

# 查看日志（最近 20 行）
Get-Content data\_health_monitor.log -Tail 20

# 卸装
powershell -ExecutionPolicy Bypass -File scripts/deploy/install_monitor_windows.ps1 -Uninstall
```

### 手动运行（调试）

```bash
cd <skill-dir>

# 单次探测（带输出）
python scripts/deploy/health_monitor.py --once

# JSON 输出（供上游采集）
python scripts/deploy/health_monitor.py --once --json

# 测试告警通道（会真实发一条飞书消息）
python scripts/deploy/health_monitor.py --test-alert

# 常驻模式（不想用计划任务时）
python scripts/deploy/health_monitor.py --loop 300
```

退出码：`0`=健康 / `1`=异常 / `2`=配置错误

---

## 六、配置项

全部通过环境变量覆盖（可写入 `data/monitor.env`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MONITOR_URL` | `https://skill.flytest.com.cn` | 探测目标 |
| `MONITOR_API_KEY` | 从 `local_config.env` 读 | 业务链路检测所需的平台 Key |
| `MONITOR_TIMEOUT` | `25` | 单次请求超时（秒） |
| `MONITOR_FAIL_THRESHOLD` | `3` | 连续失败几次后告警 |
| `MONITOR_CERT_WARN_DAYS` | `14` | 证书剩余天数预警阈值 |
| `MONITOR_WEBHOOK` | 空 | 飞书群机器人地址（可选第二通道） |
| `USER_OPEN_ID` | 从 `local_config.env` 读 | 飞书私信接收人 |

---

## 七、⚠️ 已知限制与改进方向

| 限制 | 说明 | 改进 |
|---|---|---|
| **需登录桌面** | 计划任务是 Interactive 类型，需用户会话 | 改用 NSSM 注册为 Windows 服务（开机即启） |
| **依赖本机开机** | 电脑关机期间不监控 | 同时部署一个云端监控（如 Uptime Kuma / 阿里云拨测） |
| **单点监控** | 只有一条探测链路 | 加第二探测点（不同网络/地域）可区分"服务挂了"vs"我的网络问题" |

### 推荐补充：云端拨测（可选）

若需要 7×24 覆盖（不依赖你的电脑开机），可用：
- **阿里云云监控**（域名拨测，免费额度）
- **Uptime Kuma**（自建，部署在另一台 VPS）
- **Healthchecks.io**（免费，支持飞书 webhook）

配置方法：把探测 URL 设为 `https://skill.flytest.com.cn/healthz`，
告警 webhook 指向飞书群机器人。

---

## 八、故障排查手册

收到告警后，按此顺序排查：

```bash
ssh root@168.138.54.127
cd /opt/kol-skills-platform

# 1) 容器是否在
docker ps --filter name=kolplatform

# 2) 不在 → 重启
bash scripts/deploy/start.sh --restart

# 3) 在但不响应 → 看日志
docker logs --tail 50 kolplatform-rest
docker logs --tail 50 kolplatform-mcp

# 4) nginx 是否正常
nginx -t
tail -20 /var/log/nginx/skill-platform.error.log

# 5) 证书问题
certbot certificates
certbot renew --force-renewal && nginx -s reload

# 6) 资源是否耗尽
free -m && df -h /
docker stats --no-stream
```

### 常见告警对应的原因

| 告警项 | 可能原因 |
|---|---|
| REST `/healthz` ❌ | 容器停止 / nginx 配置错误 / 服务器宕机 |
| REST 业务链路 ❌（其他 ✅） | 数据库文件损坏 / API Key 变更 |
| MCP ❌（其他 ✅） | MCP 容器停止（REST 与 MCP 是独立容器） |
| TLS 证书 ❌ | 自动续期失败（检查 `certbot.timer` 与 80 端口连通性） |
| 全部 ❌ | 服务器宕机 / DNS 变更 / 本地网络问题 |

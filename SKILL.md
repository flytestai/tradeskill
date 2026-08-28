---
name: kol-opinion-analyzer
description: >
  接收财经大V（KOL）的言论记录，持久化保存到 SQLite 数据库（按大V区分），
  结合行情数据、基本面、情绪面、量化指标等多维数据验证观点合理性，
  生成 HTML 格式分析报告并给出全市场 ETF 买入/卖出/持有建议。
  Use when users provide KOL/influencer market commentary, want to save and analyze it,
  or request ETF trading recommendations based on multi-dimensional cross-validation.
---

# 大V观点分析与 ETF 操作建议

## 核心原则：时间优先 + VIP 权重最高（CRITICAL）

**所有观点必须按日期+时间存储，分析时严格按时间倒序（最新优先）。**

- 每条言论记录精确到分钟（格式：`YYYY-MM-DD HH:MM`）
- 查询时默认 `ORDER BY record_date DESC`（最新在前）
- 观点验证优先使用最新言论，历史言论作为背景参考
- 大V可能在同一天内多次修正观点（如调整仓位、改变目标位），最新表态权重最高
- 报告中标注观点演化轨迹（例如：8/4看多→8/5兑现部分→最新持仓变化）

### VIP 消息分层权重规则（CRITICAL）

**标记 `【仅TA的真爱粉可见】` 的言论属于 VIP 付费会员专属内容。但不应一刀切 ×3，而是按分析目的分层加权。**

#### 设计原则

经 wu2198 93 条言论回测验证：
- **点位/方向预判**：公开消息与 VIP 消息精准度无差异（大盘目标位、创业板抄底均来自公开消息且全中）
- **操作/仓位信号**：公开消息 **从不透露**，全部只在 VIP
- **板块暗牌**：VIP 反复暗示的方向（如生物制品），公开消息 **从未提及**

因此分层权重方案：

```
                       公开消息           VIP 消息
                       ─────────         ────────────
① 点位/方向验证        权重 ×1    ←等价→   权重 ×1
   （公开预判同样精准，不应降权）

② 板块推荐方向          权重 ×1    ←差额→   权重 ×2
   （VIP 暗示更真实，但公开方向判断也不差）

③ ETF 操作建议          权重 ×1    ←差额→   权重 ×3
   （以 VIP 为锚，VIP 透露的真实持仓/意图权重最高）

④ 操作/仓位信号          无效（不适用）  ←独占→  仅 VIP
   （公开消息从不透露仓位变化，此维度只读 VIP）

⑤ 公开 vs VIP 冲突      无条件服从 VIP
   （VIP = 大V 真实想法，公开可能存在引导）
```

#### 分层权重矩阵

| 分析维度 | 公开消息权重 | VIP 消息权重 | 裁决规则 |
|---------|:---------:|:---------:|------|
| 点位验证（如"大盘看 3956"） | ×1 | ×1 | 同等对待，实测精准度无差异 |
| 方向判断（如"B 反趋势不变"） | ×1 | ×1 | 同等对待，方向性判断公开也准 |
| 板块推荐（如"创新药涨停潮"） | ×1 | ×2 | VIP 暗示的暗牌板块优先 |
| ETF 买入/卖出建议 | ×1 | ×3 | 以 VIP 真实持仓意图为锚 |
| 仓位变化（如"兑现 2 米"） | ❌ 不适用 | 🔒 仅 VIP | 公开从不透露 |
| 加仓/减仓触发条件 | ❌ 不适用 | 🔒 仅 VIP | 如"收复 3590 才加仓" |
| 公开 vs VIP 矛盾 | — | — | **VIP 无条件胜出** |

#### 冲突裁决规则

当同一主题公开消息与 VIP 消息方向矛盾时：

- **VIP 无条件胜出**。例如：公开说"看好科技反弹"，VIP 说"兑现 2 米科技"，以 VIP 为准 → 实际应解读为减仓科技。
- 报告中必须**显式标注冲突**："⚠️ 公开消息与 VIP 消息存在分歧，已按 VIP 裁决。"

#### VIP 消息识别

```
标记：content 包含 "【仅TA的真爱粉可见】"
```

```sql
SELECT * FROM kol_records WHERE kol_name='大V名称' AND content LIKE '%仅TA的真爱粉可见%' ORDER BY record_date DESC;
```

#### HTML 报告中的 VIP 标注规范

- 涉及 VIP 消息的观点 → 标注 🔒 VIP 标签
- 涉及冲突裁决 → 标注 ⚠️ VIP 裁决 标签
- 仓位/操作信号 → 标注 🔒 仅 VIP 标签
- VIP 消息时间线 → 独立板块，与公开消息分列

## 概述

本技能实现「大V言论采集 → 持久化 → 多维数据验证 → HTML 报告 → ETF 建议」的完整闭环。

## 数据库

所有记录持久化到 SQLite 数据库。首次使用前自动初始化。

- 数据库位置：`<skill-dir>/data/kol_opinions.db`
- 初始化脚本：`scripts/db_init.py`
- 保存脚本：`scripts/db_save.py`
- 查询脚本：`scripts/db_query.py`
- 批量导入：`scripts/db_batch.py`
- 准确率追踪：`scripts/predict_track.py`
- 关键点位监控：`scripts/level_monitor.py`
- 多KOL对比：`scripts/kol_compare.py`
- 跟单回测：`scripts/backtest.py`

> **数据库已启用 WAL 模式 + busy_timeout**，避免多进程读写锁死。

### 初始化数据库（首次或需要重建时）

```bash
python <skill-dir>/scripts/db_init.py
```

### 保存大V言论（单条）

```bash
# record-date 支持精确到分钟：YYYY-MM-DD HH:MM
# position-size 和 position-action 用于追踪仓位变化
python <skill-dir>/scripts/db_save.py \
  --kol-name "大V名称" \
  --platform "微博/雪球/公众号/抖音/其他" \
  --content "言论完整内容" \
  --related-assets "沪深300ETF,创业板ETF" \
  --record-date "2026-08-05 14:30" \
  --position-size 6 \
  --position-action "加仓" \
  --position-note "看多芯片"
```

### 批量导入大V言论（推荐，增量写入不锁库）

```bash
# 从 JSON 批量导入
python <skill-dir>/scripts/db_batch.py --json records.json

# 从文本导入（Tab分隔）
python <skill-dir>/scripts/db_batch.py --text records.txt

# 预览（不执行）
python <skill-dir>/scripts/db_batch.py --dry-run --json records.json
```

JSON 格式：
```json
[
  {"kol_name":"wu2198","platform":"微博","content":"...","related_assets":"...",
   "record_date":"2026-08-14 10:00","position_size":2,"position_action":"持有","position_note":"..."}
]
```

### 预测准确率追踪

```bash
# 添加一条预测
python <skill-dir>/scripts/predict_track.py --add --kol "wu2198" \
  --pred "B反目标3756" --type "点位" --target "3756" --dir "看涨到" --date "2026-08-13"

# 验证预测（自动判定命中/偏差/错误，按误差≤0.5%命中、≤2%偏差）
python <skill-dir>/scripts/predict_track.py --verify --id 1 --actual "3681.80" --date "2026-08-14"

# 查看准确率报告
python <skill-dir>/scripts/predict_track.py --report --kol "wu2198"

# 列出所有预测
python <skill-dir>/scripts/predict_track.py --list
```

### 关键点位监控

```bash
# 监控当前价距各关键位的距离（输入当前价）
python <skill-dir>/scripts/level_monitor.py --index 创业板指 --price 3590
python <skill-dir>/scripts/level_monitor.py --index 上证指数 --price 3918

# 列出所有监控点位
python <skill-dir>/scripts/level_monitor.py --list

# 设置/删除关键位
python <skill-dir>/scripts/level_monitor.py --set 创业板指 --level 3540 --type 风控线 --note "破=B反失败"
python <skill-dir>/scripts/level_monitor.py --delete 创业板指 --level 3540
```

### 多KOL对比

```bash
# 对比所有大V
python <skill-dir>/scripts/kol_compare.py

# 对比指定大V
python <skill-dir>/scripts/kol_compare.py --kol wu2198 李大霄
```

### 跟单回测

```bash
# 满仓跟（6米=100%）
python <skill-dir>/scripts/backtest.py --strategy full

# 半仓跟（6米=50%）
python <skill-dir>/scripts/backtest.py --strategy half

# 自定义价格/仓位序列
python <skill-dir>/scripts/backtest.py --prices prices.csv --positions positions.csv
```

**仓位字段说明：**

| 字段 | 类型 | 示例 | 说明 |
|------|------|------|------|
| `--position-size` | int | 6 | 大V的"米"数或仓位百分比 |
| `--position-action` | str | 加仓 | 加仓/减仓/兑现/持有/建仓/清仓 |
| `--position-note` | str | 看多芯片 | 补充说明（目标板块等） |

### 查询记录

默认查询**最近 30 天**，最新优先。

```bash
# 默认：最近30天
python <skill-dir>/scripts/db_query.py --kol-name "某某大V"

# 最近7天
python <skill-dir>/scripts/db_query.py --kol-name "某某大V" --days 7

# 全部历史（不限时间）
python <skill-dir>/scripts/db_query.py --kol-name "某某大V" --all

# 最近30天 + 限20条
python <skill-dir>/scripts/db_query.py --kol-name "某某大V" --latest 20

# 只看仓位变化
python <skill-dir>/scripts/db_query.py --kol-name "某某大V" --with-position

# 列出所有大V
python <skill-dir>/scripts/db_query.py --list-kols

# 自定义日期范围
python <skill-dir>/scripts/db_query.py --kol-name "某某大V" --from "2026-07-01" --to "2026-08-05"

# JSON 输出（供程序解析）
python <skill-dir>/scripts/db_query.py --kol-name "某某大V" --json
```

## 云存档（GitHub 多设备同步）

SQLite 是二进制文件，不适合 git diff/merge。采用 **JSON 文本中转** 方案实现跨设备同步。

### 架构

```
设备 A                          GitHub                        设备 B
───────                        ───────                       ───────
SQLite DB                      sync/                         SQLite DB
   │                          kol_records.json                  ▲
   │ db_export.py                ▲                              │
   └────────→ JSON ──→ git push ─┘                              │
                                 │                              │
                                 └───── git pull ──→ JSON ──→ db_import.py
```

### 推送（当前设备 → GitHub）

```bash
# 1. 导出数据库 → JSON
python <skill-dir>/scripts/db_export.py

# 2. 提交并推送
cd <skill-dir>
git add sync/kol_records.json sync/sync_meta.json
git commit -m "sync: $(date '+%Y-%m-%d %H:%M') — $(python scripts/db_query.py --list-kols 2>/dev/null | wc -l) KOLs"
git push
```

### 拉取（其他设备 ← GitHub）

```bash
# 1. 拉取最新 JSON
cd <skill-dir>
git pull

# 2. 导入 JSON → 数据库（幂等，已存在跳过）
python <skill-dir>/scripts/db_import.py
```

### 首次在新设备上使用

```bash
git clone <your-github-repo-url>
cd kol-opinion-analyzer
python scripts/db_init.py              # 创建空数据库
python scripts/db_import.py            # 从 JSON 恢复全部数据
```

### 自动化（分析前自动同步）

**每次分析前自动执行 `sync.py pull`，确保数据最新；每次保存发言自动执行 `sync.py push`。**

#### 分析前自动拉取（Step 0）

分析工作流中的 Step 0 自动执行：

```bash
# 从 GitHub 拉取最新 JSON → 导入数据库（幂等）
python <skill-dir>/scripts/sync.py pull
```

#### 保存时自动推送

保存大V发言时加 `--auto-sync` 参数，保存后自动导出+推送：

```bash
python <skill-dir>/scripts/db_save.py \
  --kol-name "wu2198" --platform "微博" \
  --content "..." --record-date "2026-08-11 14:30" \
  --auto-sync
```

**建议在 skill 中始终使用 `--auto-sync`，让每次保存 = 自动推送到 GitHub。**

### 手动同步

```bash
# 手动推送（当前设备 → GitHub，带自动重试5次）
bash <skill-dir>/scripts/push.sh

# 手动拉取（GitHub → 当前设备）
python <skill-dir>/scripts/sync.py pull

# 查看同步状态
python <skill-dir>/scripts/sync.py status
```

### 实时自动同步（默认开启）

**本 skill 已实现"保存自动推、查询自动拉"的准实时同步：**

| 操作 | 自动行为 | 关闭方式 |
|------|---------|---------|
| 保存发言 `db_save.py` | 自动导出+推送到 GitHub | `--no-auto-sync` |
| 查询数据 `db_query.py` | 自动 git pull + 导入新记录 | `--no-sync` |

**同步链路（多设备）：**

```
设备A：保存发言 → 自动push → GitHub
                              ↓
设备B：查询数据 → 自动pull+导入 → 看到最新发言
```

**其他设备安装本 skill 后，每次查询 `db_query.py` 都会自动同步 GitHub 上的最新数据，无需手动操作。**

### 注意事项

- **JSON 文件是真相源**：数据库是本地缓存，每次从 JSON 重建
- **导入是幂等的**：`db_import.py` 按 `(kol_name, record_date, content)` 去重，重复运行安全
- **推送前必须先拉取**：`git pull --rebase && git push`，避免冲突
- **.gitignore 已配置**：`data/*.db` 不会上传，只有 `sync/*.json` 进入 git
- **冲突处理**：JSON 是文本文件，git 冲突时手动合并即可

## 飞书群消息自动同步

`scripts/sync_feishu_auto.py` 通过 lark-cli（OAuth 用户授权）从飞书群增量拉取 wu2198 发言：

- **盘中时间**：交易日 9:00-11:30 / 13:00-15:00（**9:00-9:30 也算盘中**），另在盘后 16:00 兜底一次，其余时间自动跳过
- **增量拉取**：只记住「最后一次拉取的群消息时间」（水位，存于 `sync/feishu_sync_state.json`，随 GitHub 同步，多设备共享一致水位），仅拉取该时间之后的新消息
- **去重**：按 97% 文本相似度去重，测试消息自动跳过，已导入消息不重复导入
- **入库 + 推送**：增量写入 `kol_opinions.db`，有新增时自动导出 JSON 并推送到 GitHub

```bash
python scripts/sync_feishu_auto.py                # 正常同步（带盘中/交易日守卫）
python scripts/sync_feishu_auto.py --force        # 忽略守卫强制同步
python scripts/sync_feishu_auto.py --dry-run      # 只预览
python scripts/sync_feishu_auto.py --reset-watermark  # 重置增量水位（下次全量）
```

## 飞书机器人提醒

> **约定：本 skill 以后设置的所有提醒，统一通过飞书机器人（`notify_feishu.sh`）发私聊，不在会话内刷屏。**

`scripts/notify_feishu.sh` 通过飞书机器人（应用 `cli_a92579c6ddf9dcb5`，机器人身份）给用户发私聊提醒：

```bash
bash scripts/notify_feishu.sh "提醒内容"
```

**触发条件**（关键位突破/跌破或转折观点）：
- 上证放量突破 **3996** → 转多；跌破 **3741/3767** → C杀启动
- 创业板跌破 **3359** → 加速去 3300；回踩 **3300** 企稳 → 短线机会
- wu2198 发表 B反/C杀 转折性观点

> 注：lark-cli 偶发"发送成功后进程不退出"，脚本已改为后台发送（`nohup ... &`），消息发出即返回。

## 分析工作流

### Step 0: 自动同步（每次分析前必执行）

**第一步永远是先同步 JSON → 数据库，确保数据最新：**

```bash
python <skill-dir>/scripts/db_sync.py
```

- 自动检测 `scripts/*_data.json` 中的新记录
- 只插入数据库中不存在的记录（按 kol_name + record_date 去重，幂等操作）
- 已存在的记录跳过，不会重复导入
- `--dry-run` 可预览不执行

**每次用户新增 JSON 数据后，分析时自动运行此步骤，无需手动操作。**

### Step 1: 接收并保存

收到用户提供的大V言论后，先调用 `db_save.py` 持久化保存。如果数据库尚未初始化，先执行 `db_init.py`。

### Step 2: 加载并提取观点（时间优先 + 仓位追踪 + VIP 权重）

先查询最近一个月内的记录：
```bash
# 默认30天，返回全部
python <skill-dir>/scripts/db_query.py --kol-name "大V名称" --json

# 同时查询仓位变化
python <skill-dir>/scripts/db_query.py --kol-name "大V名称" --with-position --json
```

#### Step 2a: 分离 VIP 与公开消息（CRITICAL）

从查询结果中按内容是否包含 `【仅TA的真爱粉可见】` 分为两组：

- **🔒 VIP 组**：付费会员专属内容，大V最真实想法
- **📢 公开组**：面向大众，可能含引导成分

#### Step 2b: 分层加权提取（按 5 维度分别处理）

| 维度 | 处理方式 |
|------|---------|
| 点位/方向验证 | VIP 和公开**同等对待**（×1 / ×1），实测精准度无差异 |
| 板块推荐方向 | VIP ×2，公开 ×1，VIP 暗示的暗牌板块优先 |
| ETF 买入/卖出建议 | VIP ×3，公开 ×1，以 VIP 真实持仓意图为锚 |
| 仓位/操作信号 | **仅读 VIP**，公开从不透露 |
| 冲突裁决 | **VIP 无条件胜出** |

#### Step 2c: VIP 专属信号提取

1. **VIP 仓位/操作信号**：兑现、加仓、挂单、换仓等真实操作意图（仅 VIP 有）
2. **VIP 板块暗示**：反复提及的板块方向（出现 3 次以上 = 强信号，权重 ×3）
3. **VIP 加仓触发条件**：如"收复 3590 才加仓"（仅 VIP 有）
4. **VIP 观点演化轨迹**：追踪 VIP 消息中的立场变化

#### Step 2d: 公开消息补充

- **最新核心观点**（最近 1-3 天的表态）
- **点位目标跟踪**（如大盘 3856→3886→3956，公开消息同样精准）
- **观点演化轨迹**（对比早期言论，标注立场变化）
- 关联的 ETF 标的（宽基、行业、主题、跨境等）

#### Step 2e: VIP vs 公开对比与冲突裁决

- 同一主题 VIP 和公开消息并存时，按分层权重处理（非一刀切）
- 识别是否存在"公开看多、VIP 减仓"等两面信号
- **冲突时 VIP 无条件胜出**，报告中显式标注 ⚠️ VIP 裁决
- 历史言论作为背景参考

### Step 3: 多维度数据查询

针对每个关联标的和观点，并行查询以下维度：

| 维度 | 调用的 Skill/Tool | 查询内容 |
|------|------------------|---------|
| 行情数据 | `hithink-market-query` | 最新价格、涨跌幅、成交量、主力资金流向、技术指标 |
| 指数行情 | `hithink-zhishu-query` | 上证指数、沪深300、创业板指、科创50等关键指数 |
| 基本面 | `hithink-finance-query` | PE/PB 估值分位、ROE、营收增速、净利润增速 |
| 行业数据 | `hithink-industry-query` | 行业估值、资金流向、板块排名 |
| 情绪面 | `news-search` | 近期相关舆情热度、政策动态、重大事件 |
| 机构观点 | `hithink-insresearch-query` | 研报评级、业绩预测、券商金股 |
| ETF 筛选 | `hithink-etf-selector` | 按行情、规模、类型筛选匹配的 ETF |
| 宏观数据 | `hithink-macro-query` | GDP、CPI、PMI、社融等宏观背景（必要时） |

**重要**：使用 Skill 工具调用以上技能，尽量减少嵌套层级。

### Step 4: 交叉验证与分析（时间优先）

将查询到的多维数据与大V的观点进行交叉对比。**分析优先级：最新观点 > 前一日观点 > 更早观点。**

1. **观点演化验证**（核心新增）：
   - 追踪大V在时间轴上的观点变化：入场点位→持仓变化→目标调整→兑现动作
   - 对比行情实际走势，验证每一步判断的准确性
   - 示例：wu2198 8/3「3160反击」→ 8/4「3486B反明朗」→ 8/5「兑现2米，看3686」

2. **观点支持度**：
   - ✅ 数据支撑：多项指标与观点方向一致
   - ⚠️ 部分支撑：部分指标支持，部分矛盾
   - ❌ 数据矛盾：多数指标与观点方向相反
   - ❓ 无法验证：缺乏足够的量化数据

3. **时效性评估**：观点对应的市场阶段是否已过？最新表态是否修正了早期观点？

4. **风险提示**：观点中是否存在被忽略的重大风险？

### Step 5: ETF 操作建议

综合以上分析，给出具体 ETF 操作建议：

- **推荐买入**：观点被数据支撑、且市场处于有利位置的 ETF
- **建议卖出/减仓**：观点与数据矛盾、或估值过高的 ETF
- **继续持有**：观点中性、数据无明确方向的 ETF
- **关注等待**：观点有价值但时机未到的 ETF

每项建议必须包含：ETF 代码、名称、操作方向、理由（引用具体数据）、风险提示。

### Step 6: 生成 HTML 报告

使用 `templates/report.html` 模板生成完整报告，保存到用户工作目录。

报告包含以下板块：
1. 报告头部：大V名称、言论日期、分析日期
2. 大V言论原文
3. 核心观点提炼
4. 多维数据验证（每个观点一张验证卡片）
5. 综合可信度评分（0-100）
6. ETF 操作建议表格
7. 风险提示与免责声明

### 输出

1. HTML 报告保存到当前工作目录：`kol_analysis_report_{timestamp}.html`
2. 在对话中展示报告摘要和关键结论
3. 数据库记录 ID 与报告关联

## 注意事项

- **时间优先是首要原则**：存储精确到分钟，查询默认最新在前，分析以最新观点为准
- 大V可能在同一天内多次修正观点，每次修正都必须单独记录（含具体时间）
- 观点演化轨迹（持仓变化、目标调整）是评估大V可信度的关键指标
- 所有分析仅供参考，不构成投资建议
- 在报告中始终包含免责声明
- ETF 代码格式：A 股 ETF 使用 6 位数字代码，港股 ETF 使用 5 位数字代码
- 大V 的言论有时效性，分析时注意标注分析日期

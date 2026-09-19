#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群问答 AI 分析引擎：队列 → 取数据 → Kimi 分析 → 发回群里。

背景与定位
----------
原先「荔枝群 @机器人 问答」的 AI 分析由**蜜蜂运行时**承担 —— 那一步依赖
本机 Agent 环境。迁移到服务器后，服务器没有 Agent 运行时，因此本脚本
用 **Kimi（llm_client）** 接替分析环节，实现全自动闭环：

    sync_qa_auto.py（拉取 @消息入队）
            ↓
    qa_analyzer.py（本脚本：取数 → Kimi → 发回群）
            ↓
    group_reply.py（发送 + @提问人 + 免责声明 + 去重）

设计要点
--------
1. **先取平台数据再问 Kimi**：把行情/言论/关键位作为 context 注入，
   让回答有事实依据，而不是泛泛而谈（实测 Kimi 会主动指出口径不符等问题）。
2. **复用 group_reply.py**：@提问人、免责声明、去重逻辑都在那边，避免重复实现。
3. **幂等**：group_reply 内部按「提问人+问题+回答」去重，重复调用不会刷屏。
4. **失败不丢队列**：只有发送成功才 `qa_queue.py done`，否则下轮重试。

用法
----
    python qa_analyzer.py                # 处理队列中的全部待答问题
    python qa_analyzer.py --dry-run      # 只分析不发送（预览）
    python qa_analyzer.py --limit 3      # 单次最多处理 3 条
    python qa_analyzer.py --json         # 结构化输出

退出码：0=正常（含无待处理）/ 1=有失败项
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 北京时间（日志时间戳与 cron 的 TZ=Asia/Shanghai 对齐）
try:
    from common import beijing_now as _bj_now
except Exception:
    def _bj_now():
        from datetime import datetime, timezone, timedelta
        return datetime.now(timezone(timedelta(hours=8)))

try:
    from common import service_env
except Exception:
    def service_env(k, d=None):
        return os.environ.get(k, d)

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(SKILL_DIR, "scripts")
QUEUE = os.path.join(SKILL_DIR, "data", "group_qa_queue.json")
LOG = os.path.join(SKILL_DIR, "data", "_qa_analyzer.log")

#: AI 增强阶段的总预算（秒）。
#
# ⚠️ 这个值是被三层超时**倒推**出来的（实测踩坑）：
#     nginx 180s  →  Flask PLATFORM_SCRIPT_TIMEOUT 120s  →  本预算
#   实测未加约束时整条链路达到 182s，直接撞 nginx 返回 **504 Gateway Time-out**；
#   即使没撞 nginx，也会撞 Flask 的 120s 而返回 context_len=0。
#
#   55s 的构成（**分阶段实测**，非估算）：
#       规则路由     3.1s
#       AI 规划     20.5s（kimi-k3，14 个技能）
#       技能执行    ~15s（十几项 × 1~3s）
#       ---- 小计  ~39s，留 ~16s 余量
#   再加上 Kimi 生成 35.4s ≈ 合计 75s，距 Flask 上限 120s 有 45s 安全边际。
#   （实测未限制时为 116s，仅剩 4s 余量，极易因技能条数波动而触顶。）
#
# 注意：规则路由已先提供基础数据，故 AI 增强即使超时也只损失「额外维度」，
# 不会再出现「零数据」。
AI_ENHANCE_BUDGET = int(os.environ.get("QA_AI_BUDGET", "55"))


def log(msg: str) -> None:
    import datetime
    line = "[%s] %s" % (_bj_now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def _py() -> str:
    return sys.executable


def run_script(args: list, timeout: int = 120) -> str:
    r = subprocess.run([_py()] + args, capture_output=True, text=True,
                       timeout=timeout, cwd=SKILL_DIR,
                       encoding="utf-8", errors="replace")
    return (r.stdout or r.stderr or "").strip()


# --------------------------------------------------------------------------
# 队列
# --------------------------------------------------------------------------

def load_queue() -> list:
    try:
        with open(QUEUE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        log("  ⚠️ 队列读取失败: %s" % e)
        return []


def mark_done(message_id: str) -> None:
    try:
        run_script(["scripts/qa_queue.py", "done", message_id], timeout=30)
    except Exception as e:
        log("  ⚠️ 标记完成失败(%s): %s" % (message_id, e))


# --------------------------------------------------------------------------
# 取上下文数据（让 Kimi 有事实依据）
# --------------------------------------------------------------------------

#: 问句里的标的提取（含常见指数与 6 位代码）
_CODE_RE = re.compile(r"\b(\d{6})\b")
_INDEX_WORDS = ("上证指数", "深证成指", "创业板指", "创业板", "科创50", "科创综指",
                "科创板", "沪深300", "北证50", "大盘", "指数")


def build_context(question: str) -> str:
    """按问题意图调用蜜蜂多技能，聚合出结构化上下文。

    实现已统一收敛到 `skill_router`（技能整合层）：
      · 意图识别 → 编排多个蜜蜂技能（行情/财务/行业/研报/宏观/ETF/资讯…）
      · 叠加本地能力（KOL 言论库、关键位）
      · 顺序执行 + 独立超时 + 总预算控制，任一失败不影响其余

    之所以抽出去：原先此处只覆盖「指数/个股/关键位/大V」四类，
    大量问句拿不到数据，AI 只能泛泛而谈；且同一逻辑散在两处易漂移。
    """
    # ⚠️ 顺序很关键：**先用规则路由打底，再用 AI 规划增强**（实测踩坑）
    #
    # 原实现是「AI 优先，失败才回退规则」，但实测发现 plan() 单次 LLM 调用
    # 可能耗时 143 秒（timeout=90 × retries=2 最坏 291s），而后续技能执行
    # 只要 31 秒 —— 合计 174 秒，**远超 REST 层的 120 秒超时**，
    # 结果是整个请求被截断、context_len=0，用户看到「没取到数据」。
    #
    # 对比：规则路由 1 秒就能取到 688 字（贵州茅台）。
    #
    # 故改为「规则路由先兜底」：保证任何情况下都有基础数据，
    # 再尝试 AI 规划做增强（增强失败也不影响已有数据）。
    _t_start = time.time()
    base_ctx = ""
    try:
        from skill_router import build_context as _build_rule
        base_ctx = _build_rule(question) or ""
    except Exception as e:
        log("    ⚠️ 规则路由失败: %s" % str(e)[:80])

    # AI 自主规划（增强）：让它根据问题自行决定额外调哪些 skill / MCP
    try:
        sys.path.insert(0, SCRIPTS)
        from skill_agent import build_context as _build_ai
        # 先扣掉规则路由已用掉的时间，再留 15s 给后续组装/发送
        used = time.time() - _t_start
        left = int(AI_ENHANCE_BUDGET - used)
        ai_ctx = _build_ai(question, budget_sec=max(20, left)) if left > 20 else ""
        if ai_ctx:
            # 两者都命中时合并去重（AI 部分更全，放前面）
            merged = ai_ctx if not base_ctx else (base_ctx + "\n\n" + ai_ctx)
            return merged
        if base_ctx:
            log("    ℹ️ AI 规划无补充，使用规则路由结果")
            return base_ctx
        log("    ⚠️ AI 规划与规则路由均未取到数据")
    except Exception as e:
        log("    ⚠️ AI 规划失败（%s），使用规则路由结果" % str(e)[:80])
        if base_ctx:
            return base_ctx

    # 兜底：仅取指数行情（保证至少有数据）
    return _build_context_fallback(question)


def _build_context_fallback(question: str) -> str:
    """兜底：skill_router 不可用时，仅取指数行情（保证至少有数据）。"""
    parts = []
    q = question or ""
    if any(w in q for w in _INDEX_WORDS):
        for idx in ("上证指数", "创业板指"):
            try:
                out = run_script(["scripts/bee_client.py", "--query", idx,
                                  "--skill-id", "hithink-zhishu-query",
                                  "--channel", "local", "--json"], timeout=45)
                d = json.loads(out)
                item = (d.get("datas") or [{}])[0]
                if item:
                    parts.append("%s：最新 %s，涨跌幅 %s%%"
                                 % (item.get("名称", idx), item.get("最新价", "?"),
                                    item.get("涨跌幅", "?")))
            except Exception:
                pass
    return "\n".join(parts)


# --------------------------------------------------------------------------
# 处理单条
# --------------------------------------------------------------------------

def process(item: dict, dry_run: bool = False) -> tuple:
    """处理一条问答。返回 (成功, 说明)。"""
    mid = item.get("message_id") or ""
    sender = item.get("sender") or ""
    sender_id = item.get("sender_id") or ""
    question = (item.get("text") or "").strip()

    if not question:
        return True, "空问题，跳过"      # 空问题视为已处理，避免卡队列

    log("  处理：%s | %s" % (sender or "?", question[:60]))

    # 1) 取上下文
    try:
        ctx = build_context(question)
    except Exception as e:
        ctx = ""
        log("    ⚠️ 取上下文失败: %s" % e)
    if ctx:
        log("    上下文: %d 字" % len(ctx))

    # 2) Kimi 分析
    try:
        from llm_client import analyze_question, is_configured, LLMError
        if not is_configured():
            return False, "未配置 LLM_API_KEY"
        answer = analyze_question(question, ctx)
    except LLMError as e:
        return False, "Kimi 调用失败: %s" % str(e)[:150]
    except Exception as e:
        return False, "分析异常: %s" % str(e)[:150]

    if not answer or not answer.strip():
        return False, "Kimi 返回空回答"

    log("    回答: %d 字" % len(answer))

    if dry_run:
        log("    [dry-run] 不发送")
        return True, "dry-run"

    # 3) 发送（@提问人 + 免责声明 + 去重，全部由 group_reply 负责）
    #
    # ⚠️ 必须传 --chat-id：group_reply 默认发到 VIP_PUSH_CHAT_ID（荔枝群）。
    #    现同时监控「荔枝种植交流群」与「每日复盘群」，若不传此参数，
    #    复盘群的提问会被**回复到荔枝群**（已实测踩坑）。
    chat_id = (item.get("chat_id") or "").strip()
    try:
        args = ["scripts/group_reply.py",
                "--sender", sender, "--question", question,
                "--text", answer]
        if sender_id:
            args += ["--sender-id", sender_id]
        if mid:
            args += ["--message-id", mid]
        if not chat_id:
            # 无来源群信息 → 拒绝发送，避免回错群（宁可留队列下轮重试）
            return False, "队列项缺少 chat_id，拒绝发送以免回错群"
        args += ["--chat-id", chat_id]
        out = run_script(args, timeout=180)
        if "[ERROR]" in out or "失败" in out[:200]:
            return False, "发送失败: %s" % out[:150]
        return True, "已发送"
    except Exception as e:
        return False, "发送异常: %s" % str(e)[:150]


def main() -> int:
    ap = argparse.ArgumentParser(description="群问答 Kimi 分析引擎")
    ap.add_argument("--dry-run", action="store_true", help="只分析不发送")
    ap.add_argument("--limit", type=int, default=0, help="单次最多处理几条（0=不限）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    queue = load_queue()
    if not queue:
        if args.json:
            print(json.dumps({"ok": True, "pending": 0, "processed": []},
                             ensure_ascii=False))
        return 0

    log("队列待处理 %d 条" % len(queue))
    todo = queue[:args.limit] if args.limit > 0 else queue

    results, failed = [], 0
    for item in todo:
        try:
            ok, detail = process(item, dry_run=args.dry_run)
        except Exception as e:
            ok, detail = False, "未预期异常: %s" % str(e)[:150]
        results.append({"message_id": item.get("message_id"),
                        "sender": item.get("sender"),
                        "question": (item.get("text") or "")[:60],
                        "ok": ok, "detail": detail})
        if ok and not args.dry_run:
            mark_done(item.get("message_id") or "")
        elif not ok:
            failed += 1
            log("    ❌ %s（保留在队列，下轮重试）" % detail)

    if args.json:
        print(json.dumps({"ok": failed == 0, "pending": len(queue),
                          "processed": results}, ensure_ascii=False, indent=1))
    log("本轮完成：成功 %d / 失败 %d" % (len(todo) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

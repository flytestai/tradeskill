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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from common import service_env
except Exception:
    def service_env(k, d=None):
        return os.environ.get(k, d)

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(SKILL_DIR, "scripts")
QUEUE = os.path.join(SKILL_DIR, "data", "group_qa_queue.json")
LOG = os.path.join(SKILL_DIR, "data", "_qa_analyzer.log")


def log(msg: str) -> None:
    import datetime
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
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
    """按问题内容取平台数据，拼成给 Kimi 的上下文。

    失败不影响主流程 —— 拿不到数据就只问 Kimi 本身（但会提示无数据）。
    """
    parts = []
    q = question or ""

    # 1) 指数行情（问句提到指数或大盘）
    if any(w in q for w in _INDEX_WORDS):
        for idx in ("上证指数", "创业板指", "科创综指"):
            if idx in q or (idx == "上证指数" and "大盘" in q):
                try:
                    out = run_script(["scripts/bee_client.py", "--query", idx,
                                      "--skill-id", "hithink-zhishu-query",
                                      "--channel", "local", "--json"], timeout=45)
                    d = json.loads(out)
                    item = (d.get("datas") or [{}])[0]
                    if item:
                        parts.append("%s：最新 %s，涨跌幅 %s%%，最高 %s，最低 %s"
                                     % (item.get("名称", idx), item.get("最新价", "?"),
                                        item.get("涨跌幅", "?"), item.get("最高", "-"),
                                        item.get("最低", "-")))
                except Exception as e:
                    parts.append("%s：取数失败(%s)" % (idx, str(e)[:40]))

    # 2) 个股代码（问句含 6 位代码）
    codes = _CODE_RE.findall(q)
    for c in codes[:3]:
        try:
            out = run_script(["scripts/bee_client.py", "--query", c,
                              "--skill-id", "hithink-market-query", "--json"], timeout=45)
            d = json.loads(out)
            item = (d.get("datas") or [{}])[0]
            if item:
                name = item.get("股票简称") or item.get("指数简称") or c
                price = item.get("最新价") or item.get("最新收盘价") or "?"
                chg = item.get("最新涨跌幅") or item.get("涨跌幅") or "?"
                parts.append("%s(%s)：最新 %s，涨跌幅 %s%%" % (name, c, price, chg))
        except Exception as e:
            parts.append("代码 %s：取数失败(%s)" % (c, str(e)[:40]))

    # 3) 关键点位（问句涉及点位/支撑压力）
    if any(w in q for w in ("点位", "支撑", "压力", "关键位", "止损")):
        for idx in ("创业板指", "上证指数"):
            if idx in q or ("创业板" in q and idx == "创业板指"):
                try:
                    out = run_script(["scripts/level_monitor.py", "--list"], timeout=45)
                    if idx in out:
                        parts.append("关键位参考：\n" + out[:600])
                        break
                except Exception:
                    pass

    # 4) 大V最新观点（问句涉及大V）
    for kol in ("wu2198",):
        if kol.lower() in q.lower() or "大V" in q or "老吴" in q:
            try:
                out = run_script(["scripts/db_query.py", "--kol-name", kol,
                                  "--days", "3", "--latest", "5", "--json"], timeout=45)
                recs = json.loads(out)
                if isinstance(recs, list) and recs:
                    lines = ["%s %s" % (str(r.get("record_date"))[:16],
                                        (r.get("content") or "")[:70]) for r in recs[:5]]
                    parts.append("%s 近3日观点：\n%s" % (kol, "\n".join(lines)))
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
    try:
        args = ["scripts/group_reply.py",
                "--sender", sender, "--question", question,
                "--text", answer]
        if sender_id:
            args += ["--sender-id", sender_id]
        if mid:
            args += ["--message-id", mid]
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

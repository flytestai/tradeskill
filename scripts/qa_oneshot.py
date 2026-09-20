#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次问答直通：入队 → 立即分析 → 回复到群（供 trade365 bot 桥接调用）。

为什么需要
----------
平台上原本有**两个机器人都在轮询同一个飞书群**：
  · kol 的 sync_litchi_auto.py（群问答，走 Kimi 分析）
  · trade365 的 bot.py（交易命令，纯后端计算）
两者都只判断「消息里有没有 @」，**不校验 @ 的是谁** ——
用户 @ 一次，两个机器人会**各回一条**。

经确认改为「**合并为一套处理**」：
  · bot.py 作为该群的**唯一轮询器**
  · 它先尝试 trade365 命令；命中「没听懂」兜底时，把问题交给 kol 问答
  · kol 侧不再轮询该群（见 sync_litchi_auto.py 的 --no-review-group）

本脚本就是那个交接点：把单条提问送进 kol 的问答流水线并**同步**返回结果，
这样 bot.py 不需要自己去轮询队列、也不会与任何定时任务抢消息。

用法
----
    python qa_oneshot.py --chat-id oc_xxx --sender-id ou_yyy \
        --sender "昵称" --text "厦门钨业能买吗" [--message-id om_zzz]

退出码：0=已成功处理并发送 / 1=失败（调用方应回退发一条提示）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPTS)
sys.path.insert(0, SCRIPTS)

QUEUE = os.path.join(SKILL_DIR, "data", "group_qa_queue.json")

#: 单条问题的总处理预算（秒）。trade365 bot 的轮询间隔是 5 秒，
#: 但问答本身（取数+Kimi）通常 40~100 秒，故这里给足 240 秒。
BUDGET_SEC = int(os.environ.get("QA_ONESHOT_BUDGET", "240"))


def _log(msg):
    sys.stderr.write("[qa_oneshot] %s\n" % msg)


def _load():
    from safe_json import read_json
    d = read_json(QUEUE, default=[])
    return d if isinstance(d, list) else []


def _save(items):
    from safe_json import write_json
    return write_json(QUEUE, items)


def main() -> int:
    ap = argparse.ArgumentParser(description="一次问答直通（供 trade365 bot 桥接）")
    ap.add_argument("--chat-id", required=True)
    ap.add_argument("--sender-id", default="")
    ap.add_argument("--sender", default="")
    ap.add_argument("--text", required=True)
    ap.add_argument("--message-id", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    text = (args.text or "").strip()
    if not text:
        _log("空问题，跳过")
        return 1

    item = {
        "message_id": args.message_id or ("oneshot-%d" % int(time.time() * 1000)),
        "sender": args.sender or "",
        "sender_id": args.sender_id or "",
        "text": text,
        "chat_id": args.chat_id,
        "chat_type": "group",
        "create_time": str(int(time.time())),
    }

    # 1) 入队（按 message_id 幂等）
    items = _load()
    if not any(it.get("message_id") == item["message_id"] for it in items):
        items.append(item)
        if not _save(items):
            _log("队列写入失败")
            return 1

    # 2) 处理队列（qa_analyzer 会在发送成功后自动 done 掉该项）
    cmd = [sys.executable, os.path.join(SCRIPTS, "qa_analyzer.py")]
    if args.dry_run:
        cmd.append("--dry-run")
    try:
        r = subprocess.run(cmd, cwd=SKILL_DIR, capture_output=True, text=True,
                           timeout=BUDGET_SEC, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        _log("处理超时（%ds）" % BUDGET_SEC)
        return 1
    except Exception as e:
        _log("处理异常: %s" % str(e)[:150])
        return 1

    out = ((r.stdout or "") + (r.stderr or "")).strip()

    # ⚠️ dry-run 下 qa_analyzer **刻意不 mark_done**（不发送就不算处理完），
    #    所以「队列项仍在」在 dry-run 下是预期行为，不能判为失败 ——
    #    否则 dry-run 会误报失败，把「能跑通」测成「跑不通」。
    if args.dry_run:
        _log("dry-run 完成（未发送）: %s" % out[-160:].replace("\n", " "))
        return 0

    # 正常模式：队列项仍在 → 未处理成功（失败会保留在队列下轮重试）
    still = any(it.get("message_id") == item["message_id"] for it in _load())
    if still:
        _log("未完成，保留在队列: %s" % out[-200:])
        return 1

    _log("已处理: %s" % out[-120:].replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())

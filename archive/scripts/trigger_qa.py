#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后端直调蜜蜂 AI（按需触发）：有 @提问时才拉起 claude 无头会话处理问答队列。

替代「荔枝群问答队列处理」这个 Bee 定时任务：
  - 队列为空      -> 直接退出，0 token。
  - 队列有内容    -> 用 auth.json 的鉴权，拉起 claude.exe（与 Bee 内部同一套鉴权+插件），
                    处理队列里的提问并回复，处理完清触发锁。

由 supervisor 每 60 秒调用一次（脚本调用，非 Bee，0 token）；也可由 sync_litchi_auto.py
在入队后立即调用，加快响应。

用法:
  python trigger_qa.py
"""
import json
import os
import subprocess
import sys
import time

from common import find_bash

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE_FILE = os.path.join(SKILL_DIR, "data", "group_qa_queue.json")
LOCK_FILE = os.path.join(SKILL_DIR, "data", "_qa_trigger.lock")
AI_LOG = os.path.join(SKILL_DIR, "data", "_qa_ai.log")
AUTH_FILE = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "bee_ai_test", "auth.json")
SHIM = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "bee_ai_test",
                    "agent-runtime", "claude-cli", "bin", "claude")

LOCK_STALE_SEC = 900  # 触发锁超过 15 分钟视为残留（上次会话可能崩溃）
BASH = find_bash()

PROMPT = (
    "执行「荔枝种植交流群」问答队列处理。先读队列："
    "cat C:/Users/Administrator/.bee/plugins/.my-plugin/skills/kol-opinion-analyzer/data/group_qa_queue.json 2>/dev/null || echo '[]'\n\n"
    "- 队列为空：直接结束。\n"
    "- 队列有内容：逐条处理；每处理完一条，用 Bash 从队列移除该条："
    "python C:/Users/Administrator/.bee/plugins/.my-plugin/skills/kol-opinion-analyzer/scripts/qa_queue.py done \"<message_id>\"\n\n"
    "队列每项 JSON 字段：message_id / sender / sender_id / text / create_time。\n\n"
    "逐条判断意图并处理：\n"
    "A) 设置提醒（含标的+价格）：解析「标的、条件（跌破/突破涨到/区间）、价格」，用 Bash：\n"
    "   python <skill>/scripts/price_alerts.py add --target <标的> --cond <below|above|range> --price <价> [--price2 <区间上界>] --note \"<text>\" --created-by \"<sender>\" --created-by-id \"<sender_id>\"\n"
    "   - add 输出含 [DUP]：发群「ℹ️ 相同提醒已存在：<标的> <条件> <价格>」；否则发群「✅ 已设置提醒：<标的> <条件> <价格> 就提醒」\n"
    "   - 发群：bash <skill>/scripts/notify_group.sh \"消息\"\n"
    "B) 删除提醒（含\"删除/取消/去掉 + 标的\"）：python <skill>/scripts/price_alerts.py remove --target <标的>，发群「🗑 已删除 <标的> 的提醒」\n"
    "C) 编辑提醒（含\"修改/改成/更新 + 标的 + 新价格/新条件\"）：python <skill>/scripts/price_alerts.py edit --target <标的> [--price <新价>] [--price2 <新上界>] [--cond <新条件>]，发群「✏️ 已更新 <标的> 的提醒」\n"
    "D) 查看列表（含\"查看/列表/我的提醒/有哪些提醒\"）：python <skill>/scripts/price_alerts.py list，把列表整理后发群\n"
    "E) 其它任何提问（个股/指数/板块行情、价位、能否建仓/买卖、财务、新闻解读等）：\n"
    "   - 用 Skill 工具优先调 hithink-market-query 查行情/价位/资金/技术指标，需要时用 hithink-finance-query 查财务、hithink-industry-query 查行业、news-search 查资讯、hithink-insresearch-query 查研报、kimi-webbridge 联网补充。给出简洁、直接、针对问题的回答（含关键数据与明确结论，对「能否建仓」给出操作倾向并提示风险）。\n"
    "   - 回答正文里的个股/ETF 名称统一写成「名称（代码）」（首次）或「名称」，脚本会自动加粗。\n"
    "   - 回复统一用（自动 @提问人 + 底部免责声明 + 发送后自动取消敲键盘）：\n"
    "     python <skill>/scripts/group_reply.py --sender-id \"<sender_id>\" --sender \"<sender>\" --question \"<text>\" --message-id \"<message_id>\" --text \"<回答（Markdown，\\n 换行）>\"\n"
    "   - 若无法分析也回复一句说明 + 可追问方向，不要沉默。\n"
    "A-D 类处理后都要取消敲键盘：python <skill>/scripts/react.py remove \"<message_id>\"。\n\n"
    "全部处理完后，用 Bash 清除触发锁：rm -f C:/Users/Administrator/.bee/plugins/.my-plugin/skills/kol-opinion-analyzer/data/_qa_trigger.lock 。\n"
    "结束后保持安静，不要向用户重复汇报。"
)


def queue_has_pending():
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            items = json.load(f)
        return isinstance(items, list) and len(items) > 0
    except Exception:
        return False


def lock_fresh():
    try:
        with open(LOCK_FILE, encoding="utf-8") as f:
            return time.time() - float(f.read().strip() or "0") < LOCK_STALE_SEC
    except Exception:
        return False


def main():
    if not queue_has_pending():
        print("[trigger] 队列为空，跳过")
        return
    if lock_fresh():
        print("[trigger] 已有会话在处理中，跳过")
        return

    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            auth = json.load(f)
    except Exception:
        auth = {}
    token = auth.get("token", "")
    base_url = auth.get("baseUrl", "https://mifeng-test.integrity.com.cn")
    if not token:
        print("[trigger] 未找到鉴权 token，跳过")
        return

    # 写触发锁（时间戳）
    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception:
        pass

    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_AUTH_TOKEN"] = token
    env["ANTHROPIC_API_KEY"] = token
    env["ANTHROPIC_MODEL"] = "gpt-5.4-mini-bee"

    try:
        logf = open(AI_LOG, "a", encoding="utf-8")
    except Exception:
        logf = subprocess.DEVNULL
    try:
        # 走 shim（内部自动加 --plugin-dir 和 --permission-mode bypassPermissions）
        subprocess.Popen(
            [BASH, SHIM, "-p", PROMPT, "--output-format", "text"],
            cwd=SKILL_DIR,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                           | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
        )
        print("[trigger] 已触发 AI 处理队列")
    except Exception as e:
        print("[trigger] 触发失败: %s" % e)
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass


if __name__ == "__main__":
    main()

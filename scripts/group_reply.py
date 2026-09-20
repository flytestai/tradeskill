#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""荔枝群通用问答回复：把 AI 生成的分析回答发到「荔枝种植交流群」，@提问人并追加免责声明。

用法:
  python group_reply.py --sender-id <open_id> --sender <昵称> \
      --question "<问题原文>" --text "<回答（Markdown，\\n 换行）>"

  python group_reply.py --sender-id <open_id> --sender <昵称> \
      --question "<问题原文>" --text-file <回答文件路径>

  python group_reply.py --sender <昵称> --question "..." --text "..." --dry-run

说明:
  - 目标群从 data/local_config.env 的 VIP_PUSH_CHAT_ID 读取（可用 --chat-id 覆盖）
  - 自动在消息里 @提问人（优先 open_id，回退昵称），并在底部追加免责声明
  - 用机器人身份（--as bot）发到群，需机器人已在该群
  - 幂等键 = 提问人+问题+回答 的 md5，重复调用同一内容不会重复发送
  - 使用 Card 2.0 分区发送，避免 Windows 命令行中文/多行编码损坏
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

from common import find_bash, send_card
import qa_dedup
import react

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_ENV = os.path.join(SKILL_DIR, "data", "local_config.env")
STOCK_NAME_FILE = os.path.join(SKILL_DIR, "data", "stock_names.txt")
ETF_NAME_FILE = os.path.join(SKILL_DIR, "data", "etf_names.txt")
BASH = find_bash()

DISCLAIMER = "⚠️ **免责声明**：本回答由 AI 生成，仅供信息参考，不构成任何投资建议。市场有风险，投资需谨慎，据此操作风险自负。"

# 「名称（代码）」形式，名称可为纯中文（厦门钨业）或含字母/数字（创业板ETF / 沪深300ETF / 科创50ETF）
CODE_NAME_RE = re.compile(r"(?<![*0-9A-Za-z一-龥])([一-龥][一-龥0-9A-Za-z]{0,11})\s*[（(](\d{5,6})[）)]")


def _env_value(key, default=""):
    try:
        with open(LOCAL_ENV, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip()
    except Exception:
        pass
    return default


def _unescape(text):
    """把命令行传入的 \\n 还原成真实换行（真实换行原样保留）。"""
    return (text or "").replace("\\n", "\n")


def load_name_list():
    """加载要自动加粗的个股/ETF 名称清单（data/stock_names.txt + data/etf_names.txt，每行一个）。"""
    names = set()
    for path in (STOCK_NAME_FILE, ETF_NAME_FILE):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            names.add(line)
            except Exception:
                pass
    return names


def auto_bold(text, extra_names=None):
    """把回答正文里的个股/ETF 名称统一加粗。

    规则：
      ① 名称清单（data/stock_names.txt + data/etf_names.txt + --bold）里的名称加粗
      ② 「名称（代码）」整段加粗（兜底，覆盖清单外的名称）
      ③ 最后把「**名称**（代码）」合并成「**名称（代码）**」
    已加粗的片段不会二次加粗。
    """
    if not text:
        return text
    names = set(n for n in (extra_names or []) if n and len(n) >= 2)
    names.update(load_name_list())

    protected = []

    def _protect(m):
        protected.append(m.group(0))
        return "\x00%d\x00" % (len(protected) - 1)

    def _restore(t):
        for i, s in enumerate(protected):
            t = t.replace("\x00%d\x00" % i, s)
        return t

    # ① 保护已有加粗段
    text = re.sub(r"\*\*.+?\*\*", _protect, text)

    # ② 名称清单（长名优先）单次替换
    if names:
        pattern = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        text = re.sub(pattern, lambda mm: "**%s**" % mm.group(0), text)

    # ③ 保护②新产生的加粗段
    text = re.sub(r"\*\*.+?\*\*", _protect, text)

    # ④ 兜底：「名称（代码）」整段加粗（清单外的名称）
    text = CODE_NAME_RE.sub(lambda m: "**%s（%s）**" % (m.group(1), m.group(2)), text)

    # ⑤ 还原
    text = _restore(text)

    # ⑥ 合并「**名称**（代码）」→「**名称（代码）**」
    text = re.sub(r"\*\*([^*\n]+)\*\*[（(](\d{5,6})[）)]", r"**\1（\2）**", text)

    return text


#: 形如 ou_ 开头的 open_id（飞书 open_id 通常 20+ 字符十六进制）
_OPEN_ID_RE = re.compile(r"^ou_[0-9a-f]{16,}$", re.I)


def _valid_open_id(v):
    """判断能否安全用于卡片里的 <at user_id=...>。

    ⚠️ 为什么必须校验（实测踩坑）
      卡片里的 `<at user_id="xxx">` 若指向**无法解析的 open_id**，
      飞书会直接拒绝整张卡片：
          code 230099 / ErrCode 100290  "Failed to create card content"
      实测对照：同一张卡片，去掉无效 <at> 即可正常发出。

      后果不只是「@不到人」，而是**整条回复永远发不出去**，
      且 qa_analyzer 将其视为失败 → 队列下轮重试 → **永久卡死**。
      故：格式可疑时宁可不 @，也绝不能让回复发不出。
    """
    return bool(_OPEN_ID_RE.match((v or "").strip()))


def _looks_like_mention_error(err):
    """判断发送失败是否与卡片里的 @ 有关。

    飞书对「无法解析的 <at user_id>」返回：
        230099  Failed to create card content
        100290  ErrCode（"there is ..." 之类的卡片内容校验失败）
    另外 lark-cli 可能只回传 "Failed to create card content"。
    """
    e = str(err or "")
    return ("230099" in e or "100290" in e
            or "Failed to create card content" in e
            or "card content" in e.lower())


def build_message(sender_id, sender, question, answer, add_disclaimer=True,
                  force_text_mention=False):
    q = (question or "").strip().replace("\n", " ")
    if len(q) > 80:
        q = q[:80] + "…"
    # 提问行：@昵称:问题（open_id 合法才用 <at> 真@通知，否则回退纯文本 @昵称）
    if sender_id and _valid_open_id(sender_id) and not force_text_mention:
        who = f'<at user_id="{sender_id}"></at>'
    elif sender:
        who = f"@{sender}"
    else:
        who = ""
    lines = []
    if q:
        lines.append(f"{who}:{q}" if who else f"📌 提问：{q}")
    lines.append("")
    lines.append((answer or "").strip())
    if add_disclaimer:
        lines.append("")
        lines.append("---")
        lines.append(DISCLAIMER)
    return "\n".join(lines)


def build_p2p_message(answer, add_disclaimer=True):
    """私信回复：只发回答正文 + 免责声明，不需要 @提问人。"""
    lines = [(answer or "").strip()]
    if add_disclaimer:
        lines.append("")
        lines.append("---")
        lines.append(DISCLAIMER)
    return "\n".join(lines)


#: chat_id → 群名（用于卡片标题按群自适应，避免「张冠李戴」）
GROUP_TITLES = {
    "VIP_PUSH_CHAT_ID": "荔枝群问答",
    "REVIEW_CHAT_ID": "复盘群问答",
}


def _title_for_chat(chat_id):
    """按目标群返回合适的卡片标题。

    ⚠️ 为什么必须自适应（实测踩坑）
      标题原先**硬编码为「荔枝群问答」**，于是只要回复发到别的群，
      卡片上仍写着「荔枝群问答」—— 用户会直接理解为
      「**荔枝群的问答跑到这个群来了**」，即典型的「串群」现象。

      尤其在本项目开启「合并为一套处理」后：每日复盘群的用户提问会被
      bot 桥接给 kol 问答，回复就发在复盘群，但标题写着「荔枝群问答」，
      必然被误认为串群。故标题必须跟着目标群走。
      可用 --title 显式覆盖；无法识别群时退化为中性标题「群问答」。
    """
    cid = (chat_id or "").strip()
    if not cid:
        return "群问答"
    for env_key, title in GROUP_TITLES.items():
        try:
            with open(LOCAL_ENV, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(env_key + "="):
                        if line.split("=", 1)[1].strip() == cid:
                            return title
        except Exception:
            pass
    return "群问答"


def send_to_group(markdown, chat_id, idem_key, title=""):
    """通过 Card 2.0（机器人身份）发到群，返回是否成功。

    :param title: 卡片标题；留空则按目标群自适应（见 _title_for_chat）
    """
    return send_card(markdown, chat_id=chat_id,
                      title=title or _title_for_chat(chat_id),
                      subtitle="AI回复", template="blue", idem_key=idem_key)


def main():
    ap = argparse.ArgumentParser(description="荔枝群通用问答回复（@提问人 + 免责声明）")
    ap.add_argument("--sender-id", default="", help="提问人 open_id（用于 @）")
    ap.add_argument("--sender", default="", help="提问人昵称（无 open_id 时回退 @昵称）")
    ap.add_argument("--question", default="", help="问题原文")
    ap.add_argument("--message-id", default="", help="对应问题消息的 message_id（用于发送后取消「敲键盘」表情）")
    ap.add_argument("--text", default="", help="回答内容（Markdown，\\n 换行）")
    ap.add_argument("--text-file", default="", help="从文件读取回答内容（优先于 --text）")
    ap.add_argument("--bold", action="append", default=[], help="额外指定要加粗的个股名称（可多次）")
    ap.add_argument("--chat-id", default="", help="目标 chat_id（群聊默认 VIP_PUSH_CHAT_ID；私信必填）")
    ap.add_argument("--chat-type", default="group", choices=["group", "p2p"], help="发送目标：group=群聊 / p2p=私信（默认 group）")
    ap.add_argument("--no-disclaimer", action="store_true", help="不加免责声明（默认加）")
    ap.add_argument("--title", default="", help="卡片标题（留空则按目标群自适应）")
    ap.add_argument("--dry-run", action="store_true", help="只打印消息，不发送")
    args = ap.parse_args()

    if args.text_file:
        try:
            with open(args.text_file, "r", encoding="utf-8") as f:
                answer = f.read()
        except Exception as e:
            print("[ERROR] 读取回答文件失败: %s" % str(e)[:200], file=sys.stderr)
            sys.exit(1)
    else:
        answer = _unescape(args.text)

    if not answer.strip():
        print("[ERROR] 回答内容为空（--text 或 --text-file）", file=sys.stderr)
        sys.exit(1)

    question = _unescape(args.question)
    answer = auto_bold(answer, args.bold)
    if args.chat_type == "p2p":
        markdown = build_p2p_message(answer, add_disclaimer=not args.no_disclaimer)
    else:
        markdown = build_message(args.sender_id, args.sender, question, answer,
                                 add_disclaimer=not args.no_disclaimer)

    if args.dry_run:
        print(markdown)
        return

    if args.chat_type == "p2p":
        chat_id = args.chat_id
        if not chat_id:
            print("[ERROR] 私信回复必须提供 --chat-id（p2p 会话 chat_id）", file=sys.stderr)
            sys.exit(1)
    else:
        chat_id = args.chat_id or _env_value("VIP_PUSH_CHAT_ID", "")
        if not chat_id:
            print("[ERROR] 未配置目标群（--chat-id 或 local_config.env 的 VIP_PUSH_CHAT_ID）", file=sys.stderr)
            sys.exit(1)

    idem_prefix = "p2p_" if args.chat_type == "p2p" else "qa_"
    idem_key = idem_prefix + hashlib.md5(
        ("%s|%s|%s" % (args.sender_id or args.sender, question, answer)).encode("utf-8")
    ).hexdigest()[:16]
    ok, err = send_to_group(markdown, chat_id, idem_key, args.title)

    # ⚠️ 兜底重试：若因「@ 的 open_id 无效」被拒（飞书 230099 / ErrCode 100290），
    #    去掉 <at> 再发一次 —— 宁可 @不到人，也不能让回复发不出去。
    #    实测：无效 <at> 会让整张卡片创建失败，导致该问题在队列里**永久重试**。
    if (not ok) and args.chat_type != "p2p" and _valid_open_id(args.sender_id) \
            and _looks_like_mention_error(err):
        print("[WARN] 首次发送失败（疑似 @ 无效用户），降级为纯文本 @ 重试: %s"
              % str(err)[:120], file=sys.stderr)
        markdown = build_message(args.sender_id, args.sender, question, answer,
                                 add_disclaimer=not args.no_disclaimer,
                                 force_text_mention=True)
        ok, err = send_to_group(markdown, chat_id, idem_key + "t", args.title)

    if ok:
        answered_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        qa_dedup.mark_answered(args.sender_id, question, args.sender, answered_at)
        # 回答完成后取消问题消息上的「敲键盘」表情
        if args.message_id:
            react.remove_typing(args.message_id)
        if args.chat_type == "p2p":
            print("[OK] 已发送私信回复 %s（已记录去重）" % (args.sender or args.sender_id or "用户"))
        else:
            print("[OK] 已发送群回复 @%s（已记录去重）" % (args.sender or args.sender_id or "用户"))
    else:
        print("[ERROR] 发送失败: %s" % err, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

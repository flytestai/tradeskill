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
  - 写入临时文件后发送，避免 Windows 命令行中文/多行编码损坏；timeout 兜底
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

from common import find_bash
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


def build_message(sender_id, sender, question, answer, add_disclaimer=True):
    q = (question or "").strip().replace("\n", " ")
    if len(q) > 80:
        q = q[:80] + "…"
    # 提问行：@昵称:问题（有 open_id 用 <at> 真@通知，回退纯文本 @昵称，都没有则保留通用提示）
    if sender_id:
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


def send_to_group(markdown, chat_id, idem_key):
    """通过 lark-cli（机器人身份）发到群，返回是否成功。"""
    tmp = os.path.join(SKILL_DIR, "data", "_group_reply_tmp.txt")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(markdown)
        cmd = ('timeout -k 3 30 lark-cli im +messages-send '
               '--chat-id %s '
               '--idempotency-key %s '
               '--as bot --markdown "$(cat data/_group_reply_tmp.txt)"' % (chat_id, idem_key))
        last_err = ""
        for attempt in range(3):
            r = subprocess.run([BASH, "-c", cmd], capture_output=True, timeout=50, cwd=SKILL_DIR)
            out = (r.stdout or b"") + (r.stderr or b"")
            if b'"ok": true' in out or b'"ok":true' in out:
                return True, ""
            last_err = out.decode("utf-8", "ignore")[:200]
            if attempt < 2:
                time.sleep(2)
        return False, last_err
    except Exception as e:
        return False, str(e)[:200]
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="荔枝群通用问答回复（@提问人 + 免责声明）")
    ap.add_argument("--sender-id", default="", help="提问人 open_id（用于 @）")
    ap.add_argument("--sender", default="", help="提问人昵称（无 open_id 时回退 @昵称）")
    ap.add_argument("--question", default="", help="问题原文")
    ap.add_argument("--message-id", default="", help="对应问题消息的 message_id（用于发送后取消「敲键盘」表情）")
    ap.add_argument("--text", default="", help="回答内容（Markdown，\\n 换行）")
    ap.add_argument("--text-file", default="", help="从文件读取回答内容（优先于 --text）")
    ap.add_argument("--bold", action="append", default=[], help="额外指定要加粗的个股名称（可多次）")
    ap.add_argument("--chat-id", default="", help="目标群 chat_id（默认 VIP_PUSH_CHAT_ID）")
    ap.add_argument("--no-disclaimer", action="store_true", help="不加免责声明（默认加）")
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
    markdown = build_message(args.sender_id, args.sender, question, answer,
                             add_disclaimer=not args.no_disclaimer)

    if args.dry_run:
        print(markdown)
        return

    chat_id = args.chat_id or _env_value("VIP_PUSH_CHAT_ID", "")
    if not chat_id:
        print("[ERROR] 未配置目标群（--chat-id 或 local_config.env 的 VIP_PUSH_CHAT_ID）", file=sys.stderr)
        sys.exit(1)

    idem_key = "qa_" + hashlib.md5(
        ("%s|%s|%s" % (args.sender_id or args.sender, question, answer)).encode("utf-8")
    ).hexdigest()[:16]
    ok, err = send_to_group(markdown, chat_id, idem_key)
    if ok:
        answered_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        qa_dedup.mark_answered(args.sender_id, question, args.sender, answered_at)
        # 回答完成后取消问题消息上的「敲键盘」表情
        if args.message_id:
            react.remove_typing(args.message_id)
        print("[OK] 已发送群回复 @%s（已记录去重）" % (args.sender or args.sender_id or "用户"))
    else:
        print("[ERROR] 发送失败: %s" % err, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用飞书卡片发送 CLI：Markdown 正文 -> Card 2.0 -> 群聊/私信。

用法:
  printf '正文' | python scripts/send_card.py --chat-id <群ID> --title 标题
  printf '正文' | python scripts/send_card.py --user-id <open_id> --title 标题
  python scripts/send_card.py --chat-id <群ID> --title 标题 --text-file path.md
"""
import argparse, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import send_card


def main():
    ap = argparse.ArgumentParser(description="通用飞书卡片发送")
    ap.add_argument("--chat-id", default="", help="群 chat_id")
    ap.add_argument("--user-id", default="", help="用户 open_id（私信）")
    ap.add_argument("--title", default="通知", help="卡片标题")
    ap.add_argument("--subtitle", default="", help="卡片副标题")
    ap.add_argument("--template", default="blue", help="卡片配色模板")
    ap.add_argument("--text-file", default="", help="从文件读取正文（多行内容推荐）")
    ap.add_argument("--idem-key", default="", help="幂等键（防重复发送）")
    args = ap.parse_args()

    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as f:
            md = f.read()
    else:
        md = sys.stdin.read()

    if not md.strip():
        print("[ERROR] 正文为空（--text-file 或 stdin）", file=sys.stderr)
        return 1
    if not (args.chat_id or args.user_id):
        print("[ERROR] 必须指定 --chat-id 或 --user-id", file=sys.stderr)
        return 1

    ok, err = send_card(md, chat_id=args.chat_id or None, user_id=args.user_id or None,
                        title=args.title, subtitle=args.subtitle, template=args.template,
                        idem_key=args.idem_key)
    if ok:
        print("[OK] 卡片已发送")
        return 0
    print("[ERROR] 卡片发送失败: %s" % err, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

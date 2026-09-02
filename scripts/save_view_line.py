#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 AI 生成的一句话观点写入对应文件，供 market_summary 读取。"""
import argparse
import os

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser(description="保存观点一句话")
    ap.add_argument("--lunch", action="store_true", help="写入午间观点文件")
    ap.add_argument("text", help="观点正文")
    args = ap.parse_args()

    fn = "_view_lunch.txt" if args.lunch else "_view_close.txt"
    out = os.path.join(SKILL_DIR, "data", fn)
    with open(out, "w", encoding="utf-8") as f:
        f.write(args.text.strip())
    print("[OK] 已写入 %s" % fn)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日观点 + 明日关注 AI 预生成管道（服务器侧重建，2026-10-16）。

背景：原「蜜蜂任务」管道（外部系统在 11:28/15:01 生成一句话观点写
_view_*.txt）2026-09-17 起停摆，写入方不在服务器文件系统、无从修复。
本脚本在服务器侧重建该管道，彻底摆脱外部依赖：

  · 10:55（工作日）  生成 _view_lunch.txt   —— 午间观点（11:00 午间汇总用）
  · 15:00（工作日）  生成 _view_close.txt   —— 收盘观点（15:05 收盘汇总用）
                        + _focus_close.txt  —— 明日关注（AI 从当天发言提取）

  · LLM 通道：services.llm_ask（bee 网关，实测可用，零新增依赖）
  · 失败安全：任一步失败不写文件 → market_summary 的 mtime 当天防御
    自动回退「当天 DB 原文兜底」，不会出现陈旧文件冒充当日观点。

用法：
  python gen_daily_view.py            # 收盘版：观点 + 明日关注
  python gen_daily_view.py --lunch    # 午间版：观点（11:00 前发言）
  python gen_daily_view.py --dry-run  # 只打印不写文件
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts", "api"))

DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")
TZ8 = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------
# 数据：当天 wu2198 发言（公开 + VIP）
# --------------------------------------------------------------------------

def wu_rows(day: str, before_time: str | None = None):
    """取当天发言 [(time, text, is_vip)]，按时间升序。"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        sql = ("select record_date, content, is_vip from kol_records "
               "where kol_name='wu2198' and record_date like ?")
        params = [day + "%"]
        if before_time:
            sql += " and record_date <= ?"
            params.append(day + " " + before_time)
        sql += " order by record_date asc"
        return [(r[0], (r[1] or "").strip(), bool(r[2]))
                for r in conn.execute(sql, params)]
    finally:
        conn.close()


def clean(text: str) -> str:
    t = re.sub(r"【仅TA的真爱粉可见】\s*", "", text)
    t = re.sub(r"▌.*$", "", t, flags=re.M)
    t = re.sub(r"收到请回复.*$", "", t)
    t = re.sub(r"明白6{2,}.*$", "", t)
    return t.strip()


# --------------------------------------------------------------------------
# LLM 生成
# --------------------------------------------------------------------------

def _llm(prompt: str, context: str) -> str:
    import services  # noqa: PLC0415 服务器侧直接用库，免 HTTP 鉴权
    r = services.llm_ask(prompt, context=context)
    return (r or {}).get("text") or ""


def _strip_meta(text: str) -> str:
    """去掉模型自带的说明性尾巴（数据不足说明 / 免责声明行等）。"""
    t = re.sub(r"数据不足说明.*$", "", text, flags=re.S)
    t = re.sub(r"本内容由\s*AI\s*生成.*$", "", t, flags=re.S)
    t = re.sub(r"依据[:：].*$", "", t, flags=re.S)
    t = t.replace("一句话结论：", "").replace("**", "").strip()
    t = re.sub(r"[\s\n]+", " ", t).strip()
    return t


def gen_view(day: str, lunch: bool, dry_run: bool = False) -> str | None:
    """生成一句话观点，返回文本（失败返回 None，不写文件）。"""
    rows = wu_rows(day, before_time=("11:00" if lunch else None))
    rows = [(tm, clean(t), v) for tm, t, v in rows if clean(t)]
    if not rows:
        print("[SKIP] 当天无发言，不生成观点")
        return None
    # 发言正文（限 6000 字，防超上下文）
    body = "\n".join("%s%s %s" % (("[VIP] " if v else ""), tm[-5:], t)
                     for tm, t, v in rows)
    if len(body) > 6000:
        body = body[-6000:]
    label = "上午" if lunch else "全天"
    prompt = (
        "以下是股评人wu2198今天%s（按时间顺序，[VIP]开头为付费内容，占比高时以VIP为准）。"
        "请把他今天观点的主线浓缩成一句话（60字以内，含早盘判断与收盘定调的演进），"
        "直接输出观点一句话本身：不要开场白、不要前缀、不要解释、不要免责声明。" % label)
    try:
        out = _strip_meta(_llm(prompt, body))
    except Exception as e:
        print("[WARN] LLM 调用失败（观点不写文件，汇总将回退原文兜底）：%s" % e)
        return None
    if not out or len(out) < 8:
        print("[WARN] LLM 返回内容为空/过短，不写文件")
        return None
    fn = "_view_lunch.txt" if lunch else "_view_close.txt"
    path = os.path.join(SKILL_DIR, "data", fn)
    if not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            f.write(out)
        print("[OK] 已写入 %s：\n      %s" % (fn, out))
    else:
        print("[DRY] %s：%s" % (fn, out))
    return out


def gen_focus(day: str, dry_run: bool = False) -> str | None:
    """从当天发言提取明日/后市关键点位，写入 _focus_close.txt。"""
    rows = wu_rows(day)
    rows = [(tm, clean(t), v) for tm, t, v in rows if clean(t)]
    if not rows:
        print("[SKIP] 当天无发言，不生成明日关注")
        return None
    body = "\n".join("%s%s %s" % (("[VIP] " if v else ""), tm[-5:], t)
                     for tm, t, v in rows)
    if len(body) > 6000:
        body = body[-6000:]
    prompt = (
        "以下是股评人wu2198今天全天发言（含付费VIP内容）。请只提取他对「明日/后市」"
        "的关键点位判断（如“先看3700”“区间参考3656-3766”“创业板2856区域”“跌破3850”），"
        "输出格式：“上证 <点位>/<点位>；创业板 <点位>/<点位>”（无相关指数就省略该段）。"
        "只输出这一行点位结果，不要解释；如果今天发言没有任何明确的点位判断，只输出：无")
    try:
        out = _strip_meta(_llm(prompt, body))
    except Exception as e:
        print("[WARN] LLM 调用失败（明日关注不写文件，汇总将回退关键位文件）：%s" % e)
        return None
    out = out.strip().rstrip("。").strip()
    if not out or out == "无" or len(out) < 6:
        print("[SKIP] 未提取到明确点位，不写文件（汇总用 alert_levels.json 兜底）")
        return None
    path = os.path.join(SKILL_DIR, "data", "_focus_close.txt")
    if not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            f.write(out)
        print("[OK] 已写入 _focus_close.txt：\n      %s" % out)
    else:
        print("[DRY] _focus_close.txt：%s" % out)
    return out


def main():
    ap = argparse.ArgumentParser(description="每日观点/明日关注 AI 预生成")
    ap.add_argument("--lunch", action="store_true", help="生成午间版（11:00 前发言）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写文件")
    args = ap.parse_args()

    day = datetime.now(TZ8).strftime("%Y-%m-%d")
    print("=== 观点生成（%s%s）===" % (day, " 午间" if args.lunch else " 收盘"))
    gen_view(day, args.lunch, args.dry_run)
    if not args.lunch:
        print("=== 明日关注提取 ===")
        gen_focus(day, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

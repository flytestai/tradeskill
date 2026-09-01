#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
确定性关键位监控：代码硬比较价格，触发时通过飞书机器人提醒（零主观判断，避免误报）

用法:
  python monitor_alerts.py            # 正常监控
  python monitor_alerts.py --dry-run  # 只打印将触发的提醒，不发

关键位从 data/levels.json 读取（可随时增删改，无需改代码）；
文件缺失或解析失败时回退到下方 DEFAULT_LEVELS。
"""
import argparse
import json
import os
import secrets
import subprocess
import urllib.request
from datetime import datetime, timezone, timedelta

from common import find_bash

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERT_ONCE = os.path.join(SKILL_DIR, "scripts", "alert_once.sh")
LEVELS_FILE = os.path.join(SKILL_DIR, "data", "alert_levels.json")

# Git Bash 的 bash.exe 完整路径（Windows 下 subprocess 调 "bash" 会误调 WSL bash 而失败）
BASH = find_bash()

API_URL = "https://bee-ai.integrity.com.cn/skills/v1/query2data"
BASE_HEADERS = {
    "Content-Type": "application/json",
    "X-Claw-Call-Type": "normal",
    "X-Claw-Skill-Id": "hithink-zhishu-query",
    "X-Claw-Skill-Version": "1.0.0",
    "X-Claw-Plugin-Id": "none",
    "X-Claw-Plugin-Version": "none",
}

STOCKS = {
    "上证指数": "上证指数最新点位",
    "创业板指": "创业板指最新点位",
}

# 兜底配置（与 data/levels.json 保持一致；json 缺失时使用）
DEFAULT_LEVELS = {
    "上证指数": [
        {"key": "上证突破3996", "type": "break", "level": 3996,
         "point": "放量突破 3996（B反前高）", "meaning": "B反未死，转多/看新高",
         "action": "不追高，等回踩 3996 确认"},
        {"key": "上证跌破3850", "type": "below", "level": 3850,
         "point": "跌破 3850（8/25低点·双头颈线）", "meaning": "短线加速下跌，反抽结束",
         "action": "减仓/离场，别接飞刀"},
        {"key": "上证跌破3767", "type": "below", "level": 3767,
         "point": "跌破 3767（3741-3767连线二次反击点）", "meaning": "连线必破，C杀启动",
         "action": "清仓防守，等 3500 附近"},
    ],
    "创业板指": [
        {"key": "创业板跌破3359", "type": "below", "level": 3359,
         "point": "跌破 3359（8/25低点）", "meaning": "加速下探，去 3300",
         "action": "空仓等待，3300 才是观察位"},
        {"key": "创业板跌破3300", "type": "below_recover", "level": 3300, "recover": 3310,
         "recover_key": "创业板企稳3300",
         "point": "跌破 3300（短线机会位失守）", "meaning": "去 3158（A杀低）",
         "action": "观望，等 C杀 完成",
         "recover_point": "从 3300 下方收回 3310 上方",
         "recover_meaning": "3300 获支撑、短线企稳",
         "recover_action": "可关注低吸，破 3300 止损"},
        {"key": "创业板跌破3158", "type": "below", "level": 3158,
         "point": "跌破 3158（A杀低点）", "meaning": "C杀确认",
         "action": "观望，等 C5 见底"},
    ],
}


def load_levels():
    """从 data/levels.json 加载关键位；失败回退 DEFAULT_LEVELS。"""
    if os.path.exists(LEVELS_FILE):
        try:
            with open(LEVELS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                return data
        except Exception as e:
            print("[WARN] levels.json 解析失败，使用内置默认值: %s" % e)
    return DEFAULT_LEVELS


def query_index(query):
    """查询指数最新价，返回 float 或 None"""
    headers = dict(BASE_HEADERS)
    headers["X-Claw-Trace-Id"] = secrets.token_hex(32)
    body = json.dumps({"query": query, "page": "1", "limit": "10",
                       "is_cache": "1", "expand_index": "true"}).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    datas = data.get("datas", [])
    if not datas:
        return None
    d = datas[0]
    price = d.get("最新价") or d.get("收盘价") or ""
    try:
        return float(str(price).replace(",", ""))
    except Exception:
        return None


def now_str():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def fmt_msg(stock, point, desc, action):
    return ("🚨 **【关键位提醒】**\n"
            f"🕐 **时间**：{now_str()}\n"
            f"**标的**：{stock}\n"
            f"**点位**：{point}\n"
            f"📉 **含义**：{desc}\n"
            f"💡 **操作**：{action}")


def alert(key, state, msg="", dry_run=False):
    """调用 alert_once.sh（状态变化才提醒）"""
    cmd = [BASH, ALERT_ONCE, key, state, msg]
    if dry_run:
        print(f"  [DRY] {key} -> {state}  {msg[:40] if msg else '(重置)'}")
        return
    subprocess.run(cmd, capture_output=True, cwd=SKILL_DIR)


def check_below_above(key, price, thresh, stock, point, desc, action, dry_run):
    """通用双向：跌破 thresh 提醒，收回 thresh 上方重置"""
    if price < thresh:
        alert(key, "below", fmt_msg(stock, point, desc, action), dry_run)
    else:
        alert(key, "above", "", dry_run)


def read_state(key):
    """读取某触发键当前状态（data/alert_state.txt），无记录返回 ''"""
    state_file = os.path.join(SKILL_DIR, "data", "alert_state.txt")
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.strip().split("=", 1)[1]
    except Exception:
        pass
    return ""


def dispatch(stock, price, e, dry_run):
    """按 type 分发一条关键位检查。"""
    key = e.get("key", "")
    lv = e.get("level")
    if not key or lv is None:
        return
    t = e.get("type", "below")

    if t == "break":
        if price > lv:
            alert(key, "break", fmt_msg(stock, e.get("point", ""), e.get("meaning", ""), e.get("action", "")), dry_run)
        else:
            alert(key, "above", "", dry_run)
    elif t == "below_recover":
        recover = e.get("recover", lv)
        rkey = e.get("recover_key", key + "企稳")
        was_below = (read_state(key) == "below")
        if price < lv:
            alert(key, "below", fmt_msg(stock, e.get("point", ""), e.get("meaning", ""), e.get("action", "")), dry_run)
            alert(rkey, "above", "", dry_run)
        elif price > recover and was_below:
            alert(key, "above", "", dry_run)
            alert(rkey, "break", fmt_msg(stock, e.get("recover_point", ""), e.get("recover_meaning", ""), e.get("recover_action", "")), dry_run)
        else:
            alert(key, "above", "", dry_run)
            alert(rkey, "above", "", dry_run)
    else:  # below
        check_below_above(key, price, lv, stock, e.get("point", ""), e.get("meaning", ""), e.get("action", ""), dry_run)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    levels = load_levels()
    for stock, entries in levels.items():
        query = STOCKS.get(stock, stock + "最新点位")
        price = query_index(query)
        if price is None:
            print("[SKIP] %s 行情查询失败，本轮跳过" % stock)
            continue
        print("%s %s" % (stock, price))
        for e in entries:
            dispatch(stock, price, e, args.dry_run)


if __name__ == "__main__":
    main()

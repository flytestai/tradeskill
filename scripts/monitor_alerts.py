#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
确定性关键位监控：代码硬比较价格，触发时通过飞书机器人提醒（零主观判断，避免误报）

用法:
  python monitor_alerts.py            # 正常监控
  python monitor_alerts.py --dry-run  # 只打印将触发的提醒，不发

关键位（依据 wu2198 原话）：
  上证  3996 突破=转多；3850 跌破=加速；3767 跌破=连线破/C杀
  创业板 3359 跌破=加速；3300 跌破=去3158 / 收回3310上方=企稳；3158 跌破=C杀确认
"""
import argparse
import json
import os
import secrets
import subprocess
import urllib.request
from datetime import datetime, timezone, timedelta

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERT_ONCE = os.path.join(SKILL_DIR, "scripts", "alert_once.sh")

# Git Bash 的 bash.exe 完整路径（Windows 下 subprocess 调 "bash" 会误调 WSL bash 而失败）
BASH = r"C:\Program Files\Git\usr\bin\bash.exe"
if not os.path.exists(BASH):
    BASH = r"C:\Program Files\Git\bin\bash.exe"
if not os.path.exists(BASH):
    BASH = "bash"

API_URL = "https://bee-ai.integrity.com.cn/skills/v1/query2data"
BASE_HEADERS = {
    "Content-Type": "application/json",
    "X-Claw-Call-Type": "normal",
    "X-Claw-Skill-Id": "hithink-zhishu-query",
    "X-Claw-Skill-Version": "1.0.0",
    "X-Claw-Plugin-Id": "none",
    "X-Claw-Plugin-Version": "none",
}


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sz = query_index("上证指数最新点位")
    cyb = query_index("创业板指最新点位")
    if sz is None or cyb is None:
        print("[SKIP] 行情查询失败，本轮跳过")
        return

    print(f"上证 {sz} / 创业板 {cyb}")

    # 上证 3996：突破转多
    if sz > 3996:
        alert("上证突破3996", "break", fmt_msg("上证指数", "放量突破 3996（B反前高）",
                                                "B反未死，转多/看新高", "不追高，等回踩 3996 确认"), args.dry_run)
    else:
        alert("上证突破3996", "above", "", args.dry_run)

    # 上证 3850：跌破加速
    check_below_above("上证跌破3850", sz, 3850, "上证指数", "跌破 3850（8/25低点·双头颈线）",
                      "短线加速下跌，反抽结束", "减仓/离场，别接飞刀", args.dry_run)

    # 上证 3767：跌破连线必破/C杀
    check_below_above("上证跌破3767", sz, 3767, "上证指数", "跌破 3767（3741-3767连线二次反击点）",
                      "连线必破，C杀启动", "清仓防守，等 3500 附近", args.dry_run)

    # 创业板 3359：跌破加速
    check_below_above("创业板跌破3359", cyb, 3359, "创业板指", "跌破 3359（8/25低点）",
                      "加速下探，去 3300", "空仓等待，3300 才是观察位", args.dry_run)

    # 创业板 3300：跌破去3158 / 从3300下方收回3310上方=企稳（需前态是 below）
    was_below_3300 = (read_state("创业板跌破3300") == "below")
    if cyb < 3300:
        alert("创业板跌破3300", "below", fmt_msg("创业板指", "跌破 3300（短线机会位失守）",
                                                  "去 3158（A杀低）", "观望，等 C杀 完成"), args.dry_run)
        alert("创业板企稳3300", "above", "", args.dry_run)
    elif cyb > 3310 and was_below_3300:
        # 从 3300 下方收回 → 企稳
        alert("创业板跌破3300", "above", "", args.dry_run)
        alert("创业板企稳3300", "break", fmt_msg("创业板指", "从 3300 下方收回 3310 上方",
                                                  "3300 获支撑、短线企稳", "可关注低吸，破 3300 止损"), args.dry_run)
    else:
        # 一直在 3300 上方（或 3300-3310 之间）：都重置，不触发
        alert("创业板跌破3300", "above", "", args.dry_run)
        alert("创业板企稳3300", "above", "", args.dry_run)

    # 创业板 3158：跌破C杀确认
    check_below_above("创业板跌破3158", cyb, 3158, "创业板指", "跌破 3158（A杀低点）",
                      "C杀确认", "观望，等 C5 见底", args.dry_run)


if __name__ == "__main__":
    main()

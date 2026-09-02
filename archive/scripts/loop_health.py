#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""loop 心跳监控：盘中若同步循环未运行/停摆，自动重启并发私信告警。

由 Bee 定时任务在盘中每 5 分钟触发一次（cron 避开 9:00/13:00 启动宽限期）。
心跳来源复用 data/_feishu_loop.lock 的时间戳（run_loop 每轮都会刷新）。
"""
import os
import subprocess
import time
from datetime import datetime, timezone, timedelta

from common import find_bash, load_holidays

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_FILE = os.path.join(SKILL_DIR, "data", "_feishu_loop.lock")

BASH = find_bash()

STALE_SEC = 180  # 超过 3 分钟未心跳视为停摆（30 秒轮询约漏 6 次心跳）


def session_active():
    """是否处于应运行循环的盘中时段（含每段开盘后 3 分钟宽限）。"""
    now = datetime.now(timezone(timedelta(hours=8)))
    if now.weekday() >= 5:
        return False
    if now.strftime("%Y-%m-%d") in load_holidays(SKILL_DIR):
        return False
    hm = now.hour * 100 + now.minute
    # 9:03-11:30 / 13:03-15:00 才检查，避开 9:00/13:00 循环拉起的竞争
    return (903 <= hm <= 1130) or (1303 <= hm <= 1500)


def heartbeat_age():
    if not os.path.exists(LOCK_FILE):
        return None
    try:
        with open(LOCK_FILE, "r") as f:
            last = float(f.read().strip() or "0")
        return time.time() - last
    except Exception:
        return None


def restart_loop():
    """后台重启循环（nohup），返回是否已成功拿到新锁。"""
    cmd = ('cd "%s" && nohup python scripts/sync_feishu_auto.py --loop --interval 30 '
           '>> data/_loop.log 2>&1 &' % SKILL_DIR)
    try:
        subprocess.Popen([BASH, "-c", cmd], cwd=SKILL_DIR)
    except Exception as e:
        print("[WARN] 重启循环失败: %s" % e)
        return False
    time.sleep(2)
    a = heartbeat_age()
    return a is not None and a <= 5


def alert(msg):
    """通过 alert_once_private.sh 发私信（按天去重）。"""
    day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    try:
        subprocess.run(
            [BASH, os.path.join(SKILL_DIR, "scripts", "alert_once_private.sh"),
             "loop停摆_%s" % day, "below", msg],
            capture_output=True, text=True, timeout=30, cwd=SKILL_DIR)
    except Exception:
        pass


def main():
    if not session_active():
        print("[SKIP] 非盘中/宽限期")
        return
    age = heartbeat_age()
    if age is None:
        print("[ALERT] 同步循环未运行，尝试重启")
        alert("🚨 **【同步循环告警】**\n盘中同步循环未运行，已尝试自动重启。")
        restart_loop()
    elif age > STALE_SEC:
        print("[ALERT] 同步循环停摆 %.0f 秒，尝试重启" % age)
        alert("🚨 **【同步循环告警】**\n盘中同步循环已停摆 %.0f 秒，已尝试自动重启。" % age)
        restart_loop()
    else:
        print("[OK] 心跳正常（%.0f 秒前）" % age)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""supervisor 守护：检测 data/_supervisor.lock 心跳，超过阈值未更新则重启 supervisor。

零 token，可注册为 Windows 计划任务（建议每 5 分钟跑一次）实现自愈，
不依赖蜜蜂。

用法:
  python supervisor_watchdog.py               # 检查一次，必要时重启
  python supervisor_watchdog.py --stale 600   # 自定义停摆阈值（秒，默认600）
"""
import argparse
import os
import subprocess
import sys
import time

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEARTBEAT_FILE = os.path.join(SKILL_DIR, "data", "_supervisor.lock")
SUPERVISOR_SCRIPT = os.path.join(SKILL_DIR, "scripts", "supervisor.py")
SUPERVISOR_LOG = os.path.join(SKILL_DIR, "data", "_supervisor.log")

# Windows 下从计划任务里启动时，脱离父进程/作业对象，避免随守护进程退出被一起杀掉
DETACHED = (getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def heartbeat_age():
    if not os.path.exists(HEARTBEAT_FILE):
        return None
    try:
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            return time.time() - float(f.read().strip() or "0")
    except Exception:
        return None


def restart():
    try:
        logf = open(SUPERVISOR_LOG, "a", encoding="utf-8")
    except Exception:
        logf = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [sys.executable, "-u", SUPERVISOR_SCRIPT],
            cwd=SKILL_DIR,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=DETACHED,
        )
        return True
    except Exception as e:
        print("[watchdog] 重启失败: %s" % e)
        return False


def main():
    ap = argparse.ArgumentParser(description="supervisor 守护")
    ap.add_argument("--stale", type=int, default=600, help="停摆阈值秒数（默认600）")
    args = ap.parse_args()

    age = heartbeat_age()
    if age is None:
        print("[watchdog] 心跳不存在，重启 supervisor")
        restart()
    elif age > args.stale:
        print("[watchdog] 心跳停摆 %.0f 秒，重启 supervisor" % age)
        restart()
    else:
        print("[watchdog] 心跳正常（%.0f 秒前）" % age)


if __name__ == "__main__":
    main()

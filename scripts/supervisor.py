#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后端服务 supervisor：零 token 托管所有轮询循环 + 定时脚本任务。

替代原先 12 个 Bee 定时任务（那些任务每次都是一次大模型会话，白白烧积分）。
本脚本是纯 Python，不含任何 LLM 调用，token 消耗为 0。

职责：
  1. 常驻循环（各自脚本内已有文件锁，重复启动会自动跳过）：
     - sync_litchi_auto.py --loop    24 小时，每 30 秒拉荔枝群 @机器人 消息入队
     - sync_feishu_auto.py --loop    交易日盘中，每 30 秒同步 wu2198 发言 + VIP 推送
     - price_alerts.py --loop        交易日盘中，每 30 秒查价触发价格提醒
  2. 定时脚本任务：
     - position_monitor.py --notify  盘中每 5 分钟
     - monitor_alerts.py             盘中每 10 分钟
     - react.py cleanup              每 5 分钟（清理残留敲键盘表情）
  3. 每日定点任务：交易日 14:55 / 16:00 兜底同步一次。

异常处理：
  - 循环子进程崩溃 → 自动重启，并带退避（10s→20s→40s…上限300s），避免脚本秒退造成空转。
  - 循环长时间存活 → 退避重置。
  - 定时/定点任务异常 → 只记日志，不影响主循环。
  - 每 10 秒写一次心跳 data/_supervisor.lock，供 supervisor_watchdog.py 检测。

用法:
  python supervisor.py                              # 前台运行（调试）
  nohup python -u supervisor.py >> data/_supervisor.log 2>&1 &   # 后台常驻
"""
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(SKILL_DIR)

from common import load_holidays  # noqa: E402

PY = sys.executable
HEARTBEAT_FILE = os.path.join(SKILL_DIR, "data", "_supervisor.lock")
BACKOFF_BASE = 10.0
BACKOFF_MAX = 300.0


def now8():
    return datetime.now(timezone(timedelta(hours=8)))


def is_trading_day():
    d = now8()
    if d.weekday() >= 5:
        return False
    return d.strftime("%Y-%m-%d") not in load_holidays(SKILL_DIR)


def is_trading_time():
    """交易日盘中，排除午休（11:30-13:00）。

    与 sync_feishu_auto / price_alerts 内部的交易时段守卫保持一致，
    避免午休期间脚本自行退出、supervisor 又反复拉起造成退避空转。
    """
    if not is_trading_day():
        return False
    hm = now8().hour * 100 + now8().minute
    return (900 <= hm <= 1130) or (1300 <= hm <= 1500)


# (名称, 脚本参数, 是否仅在交易时间运行, 日志文件)
LOOPS = [
    ("litchi_poll", ["-u", "scripts/sync_litchi_auto.py", "--loop", "--interval", "30"], False, "data/_litchi_loop.log"),
    ("feishu_sync", ["-u", "scripts/sync_feishu_auto.py", "--loop", "--interval", "30"], True, "data/_loop.log"),
    ("price_alerts", ["-u", "scripts/price_alerts.py", "--loop", "--interval", "30"], True, "data/_price_alerts_loop.log"),
]

# (名称, 间隔秒, 脚本参数, 是否仅在交易时间运行)
PERIODIC = [
    ("position_monitor", 300, ["scripts/position_monitor.py", "--notify"], True),
    ("monitor_alerts", 600, ["scripts/monitor_alerts.py"], True),
    ("react_cleanup", 300, ["scripts/react.py", "cleanup"], False),
]

# 每日定点任务：(名称, [(时, 分), ...], 脚本参数)
DAILY_AT = [
    ("summary_lunch", [(11, 35)], ["scripts/market_summary.py", "--lunch"]),
    ("sync_preclose", [(14, 55)], ["scripts/sync_feishu_auto.py"]),
    ("summary_close", [(15, 5)], ["scripts/market_summary.py"]),
    ("sync_afterclose", [(16, 0)], ["scripts/sync_feishu_auto.py"]),
]

# 定点任务的容错窗口（分钟）：错过该窗口本日不再补发，避免重启后把
# 「11:35 午间汇总」拖到下午才发这类错时触发。
DAILY_GRACE_MIN = 30


def write_heartbeat():
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def spawn(args, logfile=None):
    """后台启动一个子进程，返回 Popen 对象。"""
    try:
        logf = None
        if logfile:
            os.makedirs(os.path.dirname(os.path.join(SKILL_DIR, logfile)), exist_ok=True)
            logf = open(os.path.join(SKILL_DIR, logfile), "a", encoding="utf-8")
        return subprocess.Popen(
            [PY] + args,
            cwd=SKILL_DIR,
            stdin=subprocess.DEVNULL,
            stdout=logf or subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
    except Exception as e:
        print("[supervisor] spawn 失败 %s: %s" % (args, e))
        return None


def loop_should_run(trading_only):
    return (not trading_only) or is_trading_time()


def run_once_script(args):
    try:
        r = subprocess.run([PY] + args, cwd=SKILL_DIR,
                           capture_output=True, text=True, timeout=240)
        tail = (r.stdout or "").strip().splitlines()
        if tail:
            print("[supervisor] %s -> %s" % (args[0], tail[-1][:120]))
    except Exception as e:
        print("[supervisor] %s 异常: %s" % (args[0], str(e)[:160]))


def run_once_script_async(args):
    """在守护线程里跑定时/定点任务，避免长任务阻塞主循环和心跳。"""
    threading.Thread(target=run_once_script, args=(args,), daemon=True).start()


def main():
    print("[supervisor] 启动 %s" % now8().strftime("%Y-%m-%d %H:%M:%S"))

    # 常驻循环进程表：restart_at=下次允许重启时间戳；backoff=当前退避秒数
    procs = {}
    for name, args, trading_only, logfile in LOOPS:
        procs[name] = {
            "args": args, "trading_only": trading_only, "logfile": logfile,
            "proc": None, "restart_at": 0.0, "backoff": BACKOFF_BASE,
        }
        if loop_should_run(trading_only):
            procs[name]["proc"] = spawn(args, logfile)

    # 定时任务上次运行时间表 / 每日定点任务已执行日期
    last_run = {name: 0.0 for name, *_ in PERIODIC}
    daily_done = {name: "" for name, *_ in DAILY_AT}

    while True:
        now = time.time()
        write_heartbeat()

        # 1) 维护常驻循环：该跑但没跑 → 按退避策略拉起
        for name, cfg in procs.items():
            p = cfg["proc"]
            alive = p is not None and p.poll() is None

            if not loop_should_run(cfg["trading_only"]):
                # 非运行时段，若还活着则终止（脚本一般会自行退出，这里兜底）
                if alive:
                    try:
                        p.terminate()
                    except Exception:
                        pass
                    cfg["proc"] = None
                cfg["restart_at"] = 0.0
                cfg["backoff"] = BACKOFF_BASE
                continue

            if alive:
                # 运行正常，重置退避
                cfg["backoff"] = BACKOFF_BASE
                cfg["restart_at"] = 0.0
                continue

            # 已死且该跑
            if cfg["restart_at"] == 0.0:
                # 第一次发现它死了：排定重启时间，并翻倍退避
                cfg["restart_at"] = now + cfg["backoff"]
                cfg["backoff"] = min(cfg["backoff"] * 2, BACKOFF_MAX)
                print("[supervisor] 循环 %s 已退出，%.0f 秒后重启（退避 %.0fs）"
                      % (name, cfg["restart_at"] - now, cfg["backoff"]))
            elif now >= cfg["restart_at"]:
                print("[supervisor] 拉起循环 %s" % name)
                cfg["proc"] = spawn(cfg["args"], cfg["logfile"])
                cfg["restart_at"] = 0.0

        # 2) 定时任务到点就跑
        for name, interval, args, trading_only in PERIODIC:
            if trading_only and not is_trading_time():
                continue
            if now - last_run[name] >= interval:
                last_run[name] = now
                run_once_script_async(args)

        # 3) 每日定点任务（交易日才跑，错过容错窗口本日不再补发）
        if is_trading_day():
            d = now8()
            key = d.strftime("%Y-%m-%d")
            now_mins = d.hour * 60 + d.minute
            for name, times, args in DAILY_AT:
                if daily_done[name] == key:
                    continue
                for hh, mm in times:
                    sched = hh * 60 + mm
                    if now_mins >= sched + DAILY_GRACE_MIN:
                        # 已错过窗口，标记完成，避免重启后错时补发
                        daily_done[name] = key
                        break
                    if now_mins >= sched:
                        daily_done[name] = key
                        run_once_script_async(args)
                        break

        time.sleep(10)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公共工具：bash 路径、节假日、文本归一化、DB 连接（消除各脚本重复）。"""
import os
import re
import sqlite3
import subprocess
import sys

# Windows 下子进程静默运行，不弹黑窗
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def pythonw_path():
    """返回 pythonw.exe（无控制台窗口版）路径；不存在则回退当前解释器。

    venv 的 python.exe 是启动器，会再拉起真实解释器（带控制台），
    用 pythonw.exe 则整条链路都无控制台窗口。
    """
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        cand = exe[: -len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            return cand
    return exe


def silence_subprocess():
    """让本进程后续所有 subprocess 调用默认不弹黑窗（Windows）。

    后端脚本调用 lark-cli / bash / python 时，cmd 黑窗会反复闪烁打扰用户；
    这里给 subprocess.run / Popen 打补丁，默认加上 CREATE_NO_WINDOW。
    """
    if not NO_WINDOW:
        return
    _run = subprocess.run
    _popen = subprocess.Popen

    def _run_w(*a, **kw):
        kw.setdefault("creationflags", NO_WINDOW)
        return _run(*a, **kw)

    def _popen_w(*a, **kw):
        kw.setdefault("creationflags", NO_WINDOW)
        return _popen(*a, **kw)

    subprocess.run = _run_w
    subprocess.Popen = _popen_w


silence_subprocess()


def find_bash():
    """定位 Git Bash 的 bash.exe（Windows 下 subprocess 调 'bash' 会误调 WSL bash）。"""
    for p in (r"C:\Program Files\Git\usr\bin\bash.exe",
              r"C:\Program Files\Git\bin\bash.exe"):
        if os.path.exists(p):
            return p
    return "bash"


# A股休市日硬编码兜底（来源：沪深北交易所公告，需每年更新）
_HARDCODED_HOLIDAYS = {
    "2026-01-01", "2026-01-02",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
    "2026-04-06", "2026-05-01", "2026-05-04", "2026-05-05", "2026-06-19",
    "2026-09-25", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
}


def load_holidays(skill_dir):
    """加载节假日集合：硬编码兜底 + data/holidays.txt。"""
    days = set(_HARDCODED_HOLIDAYS)
    if not skill_dir:
        return days
    path = os.path.join(skill_dir, "data", "holidays.txt")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and re.match(r"^\d{4}-\d{2}-\d{2}$", line):
                        days.add(line)
        except Exception:
            pass
    return days


def beijing_now():
    """北京时间（UTC+8）当前时刻。"""
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8)))


def is_trading_day(skill_dir=None):
    """交易日：周一~周五且非节假日（北京时间）。"""
    d = beijing_now()
    if d.weekday() >= 5:
        return False
    return d.strftime("%Y-%m-%d") not in load_holidays(skill_dir)


def is_trading_time(skill_dir=None):
    """交易时段：9:00-11:30 / 13:00-16:00（北京时间）。

    统一放在 common，supervisor / price_alerts / sync_feishu 都从这里取，
    避免各脚本各自维护时间窗口导致不一致、循环被反复拉起又退出。
    """
    if not is_trading_day(skill_dir):
        return False
    d = beijing_now()
    hm = d.hour * 100 + d.minute
    return (900 <= hm <= 1130) or (1300 <= hm <= 1600)


def normalize(text):
    """去掉所有空白，用于文本精确去重/相似度。"""
    return "".join((text or "").split())


def clean_wu2198_text(text):
    """清洗 wu2198 发言：去 VIP 标记、去 @wu2198 前缀、去语气词，压缩空白。"""
    t = re.sub(r"【仅TA的真爱粉可见】", "", text or "")
    t = re.sub(r"^\s*@?wu2198\s*", "", t, flags=re.I)
    t = re.sub(r"(明白666|收到请回复|收到回复|明白)\s*$", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def connect_db(db_path):
    """带 WAL + busy_timeout 的 SQLite 连接。"""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

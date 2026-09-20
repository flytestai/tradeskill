#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关键位自动刷新：用 elliott-index-wave 的失效位/目标位生成，不再靠人工维护。

为什么需要
----------
`data/level_targets.json` 一直是**人工维护的点位快照**，不会自动更新。
实测它停在 8/31 而指数已涨到 3911 —— 点位与现价严重脱节，
回答里若引用这些点位，会给用户**过期的支撑压力位**。

现有防护（陈旧度告警）只能「提醒你别信」，不能解决问题本身。
本脚本把关键位改为**自动生成**：从 elliott-index-wave 的结构化输出里
取「失效位 / C 浪目标 / 4 浪区间」，转换成 level_monitor 认的点位格式。

数据来源（elliott-index-wave 的 assess_wave.py 直接输出 JSON）
    invalidation_levels : 4浪失效(上)、C浪确认(下)、C浪否定(上)
    c_wave_targets      : C 浪的斐波那契目标位
    correction          : A/B/C 浪的高低点

用法
----
    python scripts/level_refresh.py                 # 刷新全部已配置指数
    python scripts/level_refresh.py --index 创业板指  # 只刷一个
    python scripts/level_refresh.py --dry-run       # 只看会写成什么
    python scripts/level_refresh.py --ttl-days 1    # 数据超过 N 天则重算（默认 1）

退出码：0=成功（含无需刷新）/ 1=失败
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPTS)
sys.path.insert(0, SCRIPTS)

LEVELS_FILE = os.path.join(SKILL_DIR, "data", "level_targets.json")
ASOF_FILE = os.path.join(SKILL_DIR, "data", "level_asof.txt")
#: 每个指数最近一次波浪 JSON 的落盘位置（同时充当时间戳）
WAVE_CACHE = os.path.join(SKILL_DIR, "data", "_wave_%s.json")

#: elliott-index-wave 技能目录（与 kol-opinion-analyzer 同级）
ELLIOTT_DIRS = [
    os.environ.get("ELLIOTT_SKILL_DIR", ""),
    os.path.join(os.path.dirname(SKILL_DIR), "elliott-index-wave"),
    "/opt/kol-skills-platform/vendor/elliott-index-wave",
    os.path.expanduser("~/.bee/plugins/.my-plugin/skills/elliott-index-wave"),
]


def _find_elliott():
    for d in ELLIOTT_DIRS:
        if d and os.path.isfile(os.path.join(d, "scripts", "assess_wave.py")):
            return d
    return ""


def _today():
    """北京时间日期（服务器时区为 US/Eastern，裸 strftime 会差约 12 小时）。"""
    try:
        from common import beijing_now
        return beijing_now().strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d")


def _log(msg):
    print("[level_refresh] %s" % msg, flush=True)


def _run_wave(idx, timeout=240):
    """跑一次波浪分析，返回结构化 JSON（或 None）。"""
    sd = _find_elliott()
    if not sd:
        _log("未找到 elliott-index-wave，跳过")
        return None
    gen = os.path.join(sd, "scripts", "generate_report.py")
    assess = os.path.join(sd, "scripts", "assess_wave.py")
    payload = os.path.join(SKILL_DIR, "data", "_wave_payload.json")

    # 1) 取数（generate_report 内部会 fetch 并预筛；用它的 --in 复用数据不便，
    #    这里直接调 fetch_realtime 拿 payload，再交给 assess_wave）
    fetch = os.path.join(sd, "scripts", "fetch_realtime.py")
    try:
        r = subprocess.run([sys.executable, fetch, "--index", idx, "--multi",
                            "--lookback", "260"],
                           capture_output=True, text=True, timeout=timeout,
                           cwd=sd, encoding="utf-8", errors="replace")
        data = (r.stdout or "").strip()
        if not data:
            _log("%s 取数失败: %s" % (idx, (r.stderr or "")[:150]))
            return None
        with open(payload, "w", encoding="utf-8") as f:
            f.write(data)
    except Exception as e:
        _log("%s 取数异常: %s" % (idx, str(e)[:150]))
        return None

    # 2) 预筛 → JSON
    # ⚠️ assess_wave.py 从 **stdin** 读 payload（不是 --in 参数）；
    #    实测用 --in 会得到 {"error":..., "bars":...} 而非分析结果。
    #    generate_report.py 也是这么调的：run([assess], stdin=fetch_out)
    try:
        with open(payload, encoding="utf-8") as f:
            stdin_data = f.read()
        r = subprocess.run([sys.executable, assess],
                           input=stdin_data, capture_output=True, text=True,
                           timeout=timeout, cwd=sd, encoding="utf-8",
                           errors="replace")
        out = (r.stdout or "").strip()
        if not out:
            _log("%s 预筛无输出: %s" % (idx, (r.stderr or "")[:150]))
            return None
        d = json.loads(out)
        if isinstance(d, dict) and d.get("error"):
            _log("%s 预筛报错: %s" % (idx, str(d.get("error"))[:150]))
            return None
        return d
    except Exception as e:
        _log("%s 预筛异常: %s" % (idx, str(e)[:150]))
        return None


def _to_levels(wave):
    """把波浪结果转成 level_monitor 的点位列表。

    :return: [{"level": float, "type": str, "note": str, "asof": str}, ...]
    """
    today = _today()
    out, seen = [], set()

    def add(v, typ, note):
        try:
            f = float(v)
        except Exception:
            return
        if f <= 0:
            return
        # 按 level 去重（同一价位只保留先出现的、语义更强的那条）
        key = round(f, 2)
        if key in seen:
            return
        seen.add(key)
        out.append({"level": round(f, 2), "type": typ,
                    "note": note[:40], "asof": today})

    # ⚠️ key 名以**实测输出**为准（不是猜的）：
    #   invalidation_levels: wave4_invalidation_up / C_confirm_below / C_reject_above
    #   c_wave_targets     : C_equals_0.618A / C_equals_A / C_equals_1.618A
    #   correction         : {"A": {"top": {"price":..}, "bottom": {"price":..}}, "B": {...}}
    inv = wave.get("invalidation_levels") or {}
    if isinstance(inv, dict):
        m = (("wave4_invalidation_up", "压力", "4浪失效（向上）"),
             ("C_confirm_below", "风控线", "C浪确认（向下）"),
             ("C_reject_above", "支撑", "C浪否定（向上）"))
        for k, typ, note in m:
            if k in inv:
                add(inv[k], typ, note)

    tgt = wave.get("c_wave_targets") or {}
    if isinstance(tgt, dict):
        # 只取**最近的 2 个**目标：过深的目标（如 1.618A）离现价太远，参考价值低
        _pairs = []
        for k, v in tgt.items():
            try:
                _pairs.append((float(v), k))
            except Exception:
                continue
        _pairs.sort(reverse=True)          # 从高到低（离现价近的在前）
        for v, k in _pairs[:2]:
            add(v, "目标", "C浪目标 %s" % str(k).replace("C_equals_", "="))

    corr = wave.get("correction") or {}
    if isinstance(corr, dict):
        for leg in ("A", "B", "C"):
            seg = corr.get(leg)
            if not isinstance(seg, dict):
                continue
            top = (seg.get("top") or {}).get("price")
            bot = (seg.get("bottom") or {}).get("price")
            if top:
                add(top, "压力", "%s浪顶" % leg)
            if bot:
                add(bot, "支撑", "%s浪底" % leg)

    return out


def refresh(idx, dry_run=False, ttl_days=1):
    """刷新单个指数的关键位。返回 (是否更新, 点位数)。"""
    cache = WAVE_CACHE % idx
    wave = None
    if ttl_days > 0 and os.path.isfile(cache):
        try:
            if time.time() - os.path.getmtime(cache) < ttl_days * 86400:
                with open(cache, encoding="utf-8") as f:
                    wave = json.load(f)
                _log("%s 命中缓存（%.1f 天内）" % (idx, ttl_days))
        except Exception:
            wave = None
    if wave is None:
        wave = _run_wave(idx)
        if wave:
            try:
                from safe_json import write_json
                write_json(cache, wave)
            except Exception:
                pass
    if not wave:
        return False, 0

    levels = _to_levels(wave)
    if not levels:
        _log("%s 未解析出可用点位" % idx)
        return False, 0

    if dry_run:
        _log("%s 将写入 %d 个点位：" % (idx, len(levels)))
        for it in levels:
            print("      %8s | %-6s | %s" % (it["level"], it["type"], it["note"]))
        return True, len(levels)

    try:
        from safe_json import read_json, write_json, write_text
    except Exception:
        _log("safe_json 不可用")
        return False, 0

    all_levels = read_json(LEVELS_FILE, default={})
    if not isinstance(all_levels, dict):
        all_levels = {}
    all_levels[idx] = levels
    if not write_json(LEVELS_FILE, all_levels):
        _log("写入失败: %s" % LEVELS_FILE)
        return False, 0
    # 同步数据日期（陈极度判断用内容日期，不依赖文件 mtime）
    try:
        write_text(ASOF_FILE, _today())
    except Exception:
        pass
    _log("%s 已刷新 %d 个点位" % (idx, len(levels)))
    return True, len(levels)


def main():
    ap = argparse.ArgumentParser(description="关键位自动刷新（基于波浪失效位）")
    ap.add_argument("--index", help="只刷新指定指数（默认：现有配置里的全部）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ttl-days", type=int, default=1,
                    help="波浪结果缓存天数，0=每次重算")
    args = ap.parse_args()

    if args.index:
        targets = [args.index]
    else:
        try:
            from safe_json import read_json
            d = read_json(LEVELS_FILE, default={})
            targets = list(d.keys()) if isinstance(d, dict) else []
        except Exception:
            targets = []
    if not targets:
        _log("没有需要刷新的指数")
        return 1

    ok = 0
    for idx in targets:
        try:
            changed, n = refresh(idx, dry_run=args.dry_run, ttl_days=args.ttl_days)
            if changed:
                ok += 1
        except Exception as e:
            _log("%s 刷新异常: %s" % (idx, str(e)[:150]))
    _log("完成：%d/%d 个指数已更新" % (ok, len(targets)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

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
import importlib.util
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

#: elliott-index-wave 技能目录（本仓库 skills/ 下；脚本已重构为单一 generate_report.py）
ELLIOTT_SKILL_DIR = os.path.join(SKILL_DIR, "skills", "elliott-index-wave")

_gen = None
_gen_err = None


def _load_gen():
    """懒加载 generate_report.py（内建 _multi_analyze 直连腾讯多周期K）。"""
    global _gen, _gen_err
    if _gen is not None or _gen_err is not None:
        return _gen
    path = os.path.join(ELLIOTT_SKILL_DIR, "scripts", "generate_report.py")
    if not os.path.isfile(path):
        _gen_err = "generate_report.py 未找到：%s" % path
        return None
    try:
        spec = importlib.util.spec_from_file_location("elliott_generate_report", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _gen = mod
    except Exception as e:
        _gen_err = "%s: %s" % (type(e).__name__, str(e)[:160])
    return _gen


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
    """跑一次波浪分析，返回结构化 JSON（或 None）。

    适配 2026-09 重构后的 elliott-index-wave：原 fetch_realtime.py / assess_wave.py
    已合并进单一 generate_report.py（内建 _multi_analyze 直连腾讯多周期K）。
    """
    gen = _load_gen()
    if gen is None:
        _log("未找到 elliott-index-wave，跳过")
        return None
    symbol = gen.INDEX_CODES.get(idx)
    if not symbol:
        _log("%s 无行情代码，跳过" % idx)
        return None
    try:
        multi = gen._multi_analyze(symbol, 2000)
    except Exception as e:
        _log("%s 波浪分析异常: %s" % (idx, str(e)[:150]))
        return None
    day = (multi or {}).get("day")
    if not day:
        _log("%s 波浪分析无日线结果" % idx)
        return None

    fib = day.get("fib") or {}
    # 转成 level_refresh 既有的 JSON 形状，_to_levels 无需改动
    return {
        "invalidation_levels": {
            "wave4_invalidation_up": day.get("top"),
            "C_confirm_below": day.get("A"),
            "C_reject_above": day.get("B"),
        },
        "c_wave_targets": {
            "C_equals_0.618A": fib.get("0.618"),
            "C_equals_A": fib.get("1.000"),
            "C_equals_1.618A": fib.get("1.618"),
        },
        "correction": {
            "A": {"top": {"price": day.get("top")}, "bottom": {"price": day.get("A")}},
            "B": {"top": {"price": day.get("B")}, "bottom": {"price": day.get("A")}},
            "C": {"top": {"price": day.get("B")}, "bottom": {"price": day.get("C")}},
        },
    }


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

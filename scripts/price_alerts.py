#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""价格提醒系统：自然语言设置「标的到某价 / 跌破 / 突破 / 区间」提醒，盘中定时检查并群发提醒。

存储：data/price_alerts.json（每条一个任务，幂等可重复运行）

用法：
  python price_alerts.py add --text "创业板指跌破3356就提醒我"
  python price_alerts.py add --target 创业板指 --cond below --price 3356
  python price_alerts.py list
  python price_alerts.py remove --id <id>
  python price_alerts.py reset --id <id>          # 重置已触发状态，可再次提醒
  python price_alerts.py check                    # 定时跑：查价，触发则群发提醒
  python price_alerts.py check --dry-run
"""
import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

from common import find_bash, load_holidays

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERTS_FILE = os.path.join(SKILL_DIR, "data", "price_alerts.json")
LOOP_LOCK_FILE = os.path.join(SKILL_DIR, "data", "_price_alerts_loop.lock")
LOOP_STALE_SEC = 180
BASH = find_bash()

API_URL = "https://bee-ai.integrity.com.cn/skills/v1/query2data"

# 常见指数关键词 → 规范查询名
INDEX_ALIASES = {
    "上证指数": "上证指数", "上证": "上证指数", "沪指": "上证指数",
    "深证成指": "深证成指", "深成指": "深证成指", "深证": "深证成指",
    "创业板指": "创业板指", "创业板": "创业板指",
    "科创50": "科创50", "科创板": "科创50",
    "沪深300": "沪深300", "中证500": "中证500", "中证1000": "中证1000",
    "上证50": "上证50", "恒生科技": "恒生科技", "纳斯达克": "纳斯达克",
}

COND_KEYWORDS = {
    "below": ["跌破", "下破", "跌到", "跌至", "跌破至", "以下", "低于", "掉到", "回落到",
              "回踩到", "跌下", "向下到", "下探到", "跌下去", "跌破到", "回撤到"],
    "above": ["突破", "上破", "站上", "涨破", "涨到", "涨至", "升破", "收复", "以上", "高于",
              "冲上", "达到", "到达", "涨过", "上冲", "越过", "拉高到", "涨到", "上去"],
    "range": ["区间", "之间", "震荡区间", "范围内", "到...之间", "到…之间"],
}

NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _load():
    if os.path.exists(ALERTS_FILE):
        try:
            with open(ALERTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save(alerts):
    os.makedirs(os.path.dirname(ALERTS_FILE), exist_ok=True)
    with open(ALERTS_FILE, "w", encoding="utf-8") as f:
        json.dump(alerts, f, ensure_ascii=False, indent=2)


def query_price(target):
    """查询标的现价，返回 (price, name, chg) 或 None。"""
    headers = {
        "Content-Type": "application/json",
        "X-Claw-Call-Type": "normal",
        "X-Claw-Skill-Id": "hithink-market-query",
        "X-Claw-Skill-Version": "1.0.0",
        "X-Claw-Plugin-Id": "none",
        "X-Claw-Plugin-Version": "none",
        "X-Claw-Trace-Id": secrets.token_hex(32),
    }
    for q in (target, target + "最新价"):
        body = json.dumps({"query": q, "page": "1", "limit": "10",
                           "is_cache": "1", "expand_index": "true"}).encode("utf-8")
        req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:
            continue
        datas = data.get("datas", [])
        if not datas:
            continue
        it = datas[0]
        raw = it.get("最新价") or it.get("最新收盘价") or it.get("收盘价[20260901]")
        if raw is None:
            continue
        try:
            price = float(str(raw).replace(",", ""))
        except Exception:
            continue
        name = it.get("股票简称") or it.get("指数简称") or it.get("基金简称") or target
        chg = it.get("最新涨跌幅") or it.get("最新涨跌幅:前复权") or ""
        return price, name, chg
    return None


_FILLER = ["就", "要", "想", "提醒", "通知", "告诉", "一下", "的时候", "了", "元",
           "块", "到", "价", "帮我", "请", "给", "我", "你", "在", "是", "点",
           "左右", "附近", "就提醒", "提醒我", "提醒一下", "帮我提醒", "设置", "加个",
           "设置提醒", "帮我设置", "帮我设置提醒", "当", "如果", "监控", "盯",
           "盯一下", "盯着", "看着", "注意", "帮我盯", "帮我盯着", "麻烦", "麻烦帮我",
           "帮我关注", "关注", "留意", "帮我留意", "个", "一条", "一个", "的"]


def parse_alert_text(text):
    """把自然语言解析成 (target, cond, price, price2) 或 None。

    顺序：先定标的（代码 > 指数别名 > 条件词前的文本），再去标的后提取价格数字。
    """
    if not text:
        return None

    # 1. 标的
    target = None
    code_m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if code_m:
        target = code_m.group(1)
        work = text.replace(target, " ")
    else:
        for alias, full in INDEX_ALIASES.items():
            if alias in text:
                target = full
                work = text.replace(alias, " ")
                break
        if not target:
            # 通用：条件词之前的那段文本就是标的（去掉数字和语气词）
            pos = len(text)
            for kws in COND_KEYWORDS.values():
                for kw in kws:
                    p = text.find(kw)
                    if p >= 0:
                        pos = min(pos, p)
            target_part = text[:pos]
            target_part = NUM_RE.sub(" ", target_part)
            for w in sorted(_FILLER, key=len, reverse=True):  # 先替换长词，避免“盯”把“盯着”拆散
                target_part = target_part.replace(w, " ")
            target_part = re.sub(r"[，。！？：；、,.!?;:()（）\[\]【】]", " ", target_part)
            target = "".join(target_part.split())
            work = text
    if not target:
        return None

    # 2. 条件
    cond = None
    for c, kws in COND_KEYWORDS.items():
        for kw in kws:
            if kw in text:
                cond = c
                break
        if cond:
            break
    if cond is None:
        cond = "above"  # 默认「到价」视为涨到

    # 3. 价格（从去掉标的后的文本里取）
    nums = [float(x) for x in NUM_RE.findall(work)]
    if not nums:
        return None
    if cond == "range":
        nums = sorted(set(nums))
        if len(nums) >= 2:
            price, price2 = nums[0], nums[-1]
        else:
            price, price2 = nums[0], None
    else:
        price, price2 = nums[0], None

    return target, cond, price, price2


def add_alert(target, cond, price, price2=None, note="", chat_id=""):
    if cond not in ("below", "above", "range"):
        print(f"[ERROR] 条件类型无效: {cond}")
        return
    if cond == "range" and (price2 is None or price2 <= price):
        print("[ERROR] 区间需要两个递增的价格")
        return
    alerts = _load()
    # 去重：同标的+同条件+同价格 已存在则不重复加
    for a in alerts:
        if (a.get("target") == target and a.get("cond") == cond
                and a.get("price") == price and a.get("price2") == price2
                and a.get("status") == "active"):
            print(f"[INFO] 已存在相同提醒（id={a.get('id')}），不重复添加")
            return
    a = {
        "id": secrets.token_hex(4),
        "target": target,
        "cond": cond,
        "price": price,
        "price2": price2,
        "note": note,
        "chat_id": chat_id,
        "status": "active",          # active / triggered
        "created_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "triggered_at": "",
    }
    alerts.append(a)
    _save(alerts)
    print(f"[OK] 已添加提醒 {a['id']}: {target} {cond} {price}" +
          (f"~{price2}" if price2 else "") + (f"（{note}）" if note else ""))


def list_alerts():
    alerts = _load()
    if not alerts:
        print("[INFO] 暂无价格提醒")
        return
    print(f"\n  📢 价格提醒（共 {len(alerts)} 条）\n")
    for a in alerts:
        st = "✅已触发" if a["status"] == "triggered" else "🟢待触发"
        cond_txt = {"below": "跌破", "above": "突破/涨到", "range": "区间"}[a["cond"]]
        rng = f"{a['price']} ~ {a['price2']}" if a["price2"] else str(a["price"])
        print(f"  [{a['id']}] {st} {a['target']} {cond_txt} {rng}"
              + (f" ｜{a['note']}" if a.get("note") else ""))


def _triggered(cond, price, alert):
    """判断是否触发。"""
    p, p1, p2 = price, alert["price"], alert.get("price2")
    if cond == "below":
        return p <= p1
    if cond == "above":
        return p >= p1
    if cond == "range":
        return p2 is not None and p1 <= p <= p2
    return False


def check_alerts(dry_run=False, quiet=False):
    alerts = _load()
    active = [a for a in alerts if a.get("status") == "active"]
    if not active:
        if not quiet:
            print("[INFO] 无待触发提醒")
        return
    changed = False
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    for a in active:
        res = query_price(a["target"])
        if res is None:
            if not quiet:
                print(f"[SKIP] 查询失败: {a['target']}")
            continue
        price, name, chg = res
        if not _triggered(a["cond"], price, a):
            if not quiet:
                print(f"[OK] {name} 现价 {price}，未触发 {a['cond']} {a['price']}")
            continue
        # 触发
        cond_txt = {"below": "跌破", "above": "突破/涨到", "range": "进入区间"}[a["cond"]]
        rng = f"{a['price']}~{a['price2']}" if a["price2"] else str(a["price"])
        msg = (f"🚨 **【价格提醒】**\n"
               f"🕐 {now}\n"
               f"标的：{name}（{a['target']}）\n"
               f"触发：{cond_txt} {rng}\n"
               f"现价：{price}" + (f"（{chg}%）" if chg else "") +
               (f"\n备注：{a['note']}" if a.get("note") else ""))
        if dry_run:
            print(f"[DRY] {msg}")
        else:
            try:
                subprocess.run([BASH, os.path.join(SKILL_DIR, "scripts", "notify_group.sh"), msg],
                               capture_output=True, timeout=30, cwd=SKILL_DIR)
                print(f"[ALERT] 已发送提醒: {name} {cond_txt} {rng}")
            except Exception as e:
                print(f"[WARN] 发送失败: {e}")
        a["status"] = "triggered"
        a["triggered_at"] = now
        changed = True
    if changed and not dry_run:
        _save(alerts)


def _acquire_lock():
    """文件锁：防止多个 --loop 循环同时跑。"""
    try:
        fd = os.open(LOOP_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(time.time()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            with open(LOOP_LOCK_FILE) as f:
                last = float(f.read().strip() or "0")
            if time.time() - last < LOOP_STALE_SEC:
                return False
        except Exception:
            pass
        try:
            os.remove(LOOP_LOCK_FILE)
        except Exception:
            return False
        try:
            fd = os.open(LOOP_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(time.time()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            return False
    except Exception:
        return True


def _touch_lock():
    try:
        with open(LOOP_LOCK_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _release_lock():
    try:
        if os.path.exists(LOOP_LOCK_FILE):
            os.remove(LOOP_LOCK_FILE)
    except Exception:
        pass


def _trading_time():
    now = datetime.now(timezone(timedelta(hours=8)))
    if now.weekday() >= 5:
        return False
    if now.strftime("%Y-%m-%d") in load_holidays(SKILL_DIR):
        return False
    hm = now.hour * 100 + now.minute
    return (900 <= hm <= 1130) or (1300 <= hm <= 1500)


def run_loop(interval, force=False):
    """盘中高频轮询：每 interval 秒查一次价，命中即群发提醒；非盘中自动退出。"""
    interval = max(10, interval)
    if not _acquire_lock():
        print("[LOOP] 已有价格提醒循环在运行，本次跳过")
        return
    print("[LOOP] 价格提醒循环启动：每 %d 秒检查一次" % interval)
    try:
        while True:
            _touch_lock()
            if not force and not _trading_time():
                print("[LOOP] 非盘中时间，循环退出")
                return
            try:
                check_alerts(dry_run=False, quiet=True)
            except Exception as e:
                print("[LOOP] 检查异常: %s" % e)
            time.sleep(interval)
    finally:
        _release_lock()


def main():
    ap = argparse.ArgumentParser(description="价格提醒系统")
    ap.add_argument("--loop", action="store_true", help="盘中高频轮询循环（每 --interval 秒检查一次）")
    ap.add_argument("--interval", type=int, default=30, help="--loop 模式的检查间隔秒数（默认 30）")
    ap.add_argument("--force", action="store_true", help="--loop 模式下忽略盘中时间守卫（测试用）")
    sub = ap.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add", help="添加提醒")
    p_add.add_argument("--text", help="自然语言，如：创业板指跌破3356就提醒我")
    p_add.add_argument("--target", help="标的（指数/股票/ETF 名称或代码）")
    p_add.add_argument("--cond", choices=["below", "above", "range"], help="条件")
    p_add.add_argument("--price", type=float, help="触发价")
    p_add.add_argument("--price2", type=float, help="区间上界（cond=range 时必填）")
    p_add.add_argument("--note", default="", help="备注")
    p_add.add_argument("--chat-id", default="", help="提醒目标群（默认通知群）")

    sub.add_parser("list", help="列出所有提醒")
    p_rm = sub.add_parser("remove", help="删除提醒")
    p_rm.add_argument("--id", required=True)
    p_rst = sub.add_parser("reset", help="重置已触发状态")
    p_rst.add_argument("--id", required=True)
    p_chk = sub.add_parser("check", help="检查并触发提醒")
    p_chk.add_argument("--dry-run", action="store_true")

    args = ap.parse_args()

    if args.loop:
        run_loop(args.interval, force=args.force)
        return

    if args.cmd == "add":
        if args.text:
            r = parse_alert_text(args.text)
            if not r:
                print("[ERROR] 无法解析该指令，请用 --target/--cond/--price 明确指定")
                sys.exit(1)
            target, cond, price, price2 = r
            add_alert(target, cond, price, price2, note=args.note or args.text, chat_id=args.chat_id)
        else:
            if not args.target or not args.cond or args.price is None:
                print("[ERROR] 需要 --target --cond --price（或 --text）")
                sys.exit(1)
            add_alert(args.target, args.cond, args.price, args.price2, args.note, args.chat_id)
    elif args.cmd == "list":
        list_alerts()
    elif args.cmd == "remove":
        alerts = _load()
        alerts = [a for a in alerts if a.get("id") != args.id]
        _save(alerts)
        print(f"[OK] 已删除 {args.id}")
    elif args.cmd == "reset":
        alerts = _load()
        for a in alerts:
            if a.get("id") == args.id:
                a["status"] = "active"
                a["triggered_at"] = ""
        _save(alerts)
        print(f"[OK] 已重置 {args.id}")
    elif args.cmd == "check":
        check_alerts(args.dry_run)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

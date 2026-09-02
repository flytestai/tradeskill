#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日收盘 / 午间汇总（后端版，观点行走一次轻量 AI 摘要）。

行情/成交额/主力资金直连 API，套固定模板发到「荔枝种植交流群」；
仅「wu2198观点」一行用 AI 做一句话总结（每天最多 2 次，失败自动回退原文）。
替代原「wu2198收盘汇总提醒」「wu2198午休汇总提醒」两个 Bee 定时任务。

用法:
  python market_summary.py            # 收盘汇总（当日全部发言）
  python market_summary.py --lunch    # 午间汇总（11:35 前发言）
  python market_summary.py --dry-run  # 只打印，不发群
"""
import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone, timedelta

from common import find_bash, load_holidays, connect_db

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = find_bash()
API_URL = "https://bee-ai.integrity.com.cn/skills/v1/query2data"
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")
LEVELS_FILE = os.path.join(SKILL_DIR, "data", "alert_levels.json")
NOTIFY = os.path.join(SKILL_DIR, "scripts", "notify_group.sh")
STATE_FILE = os.path.join(SKILL_DIR, "data", "_market_summary_state.txt")
AUTH_FILE = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "bee_ai_test", "auth.json")
CLAUDE_SHIM = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "bee_ai_test",
                           "agent-runtime", "claude-cli", "bin", "claude")
AI_MODEL = "gpt-5.4-mini-bee"

INDICES = ["上证指数", "深证成指", "科创50", "创业板指"]
DISPLAY = {"上证指数": "上证", "深证成指": "深证", "科创50": "科创50", "创业板指": "创业板"}


def _headers():
    return {
        "Content-Type": "application/json",
        "X-Claw-Call-Type": "normal",
        "X-Claw-Skill-Id": "hithink-market-query",
        "X-Claw-Skill-Version": "1.0.0",
        "X-Claw-Plugin-Id": "none",
        "X-Claw-Plugin-Version": "none",
        "X-Claw-Trace-Id": secrets.token_hex(32),
    }


def query_item(query):
    """查询单条数据，返回首个 datas 项 dict 或 None。"""
    body = json.dumps({"query": query, "page": "1", "limit": "10",
                       "is_cache": "1", "expand_index": "true"}).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, headers=_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print("[WARN] %s 查询失败: %s" % (query, e))
        return None
    datas = data.get("datas", [])
    if not datas:
        print("[WARN] %s 无数据" % query)
        return None
    return datas[0]


def query_index(index):
    """查询指数最新价与涨跌幅，返回 (price, chg) 或 None。"""
    it = query_item(index + "最新价")
    if not it:
        return None
    try:
        price = float(str(it.get("最新价", "")).replace(",", ""))
    except Exception:
        return None
    chg = None
    for k in ("最新涨跌幅:前复权", "最新涨跌幅", "涨跌幅"):
        if it.get(k) is not None:
            try:
                chg = float(it[k])
                break
            except Exception:
                continue
    return price, chg


def query_amount(query, field_prefix):
    """查询带日期后缀的金额字段（如 成交额[20260902]），返回元或 None。"""
    it = query_item(query)
    if not it:
        return None
    for k, v in it.items():
        if k.startswith(field_prefix):
            try:
                return float(v)
            except Exception:
                return None
    return None


def fmt_yi(yuan):
    """元 → 亿/万亿 可读字符串。"""
    yi = yuan / 1e8
    if abs(yi) >= 10000:
        return "%.2f万亿" % (yi / 10000)
    return "%.2f亿" % yi


def fmt_chg(chg):
    if chg is None:
        return ""
    return "%+.2f%%" % chg


def wu2198_texts(day, before_time=None):
    """取 wu2198 当天（可选时间点前）发言原文列表，去 VIP 标记与语气词。"""
    conn = connect_db(DB_PATH)
    try:
        cur = conn.cursor()
        sql = ("select content from kol_records "
               "where kol_name='wu2198' and record_date like ?")
        params = [day + "%"]
        if before_time:
            sql += " and record_date <= ?"
            params.append(day + " " + before_time)
        sql += " order by record_date asc"
        rows = [r[0] for r in cur.execute(sql, params)]
    finally:
        conn.close()

    texts = []
    for t in rows:
        t = re.sub(r"【仅TA的真爱粉可见】", "", t or "")
        t = re.sub(r"^\s*@?wu2198\s*", "", t, flags=re.I)
        t = re.sub(r"(明白666|收到请回复|收到回复|明白)\s*$", "", t, flags=re.I)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            texts.append(t)
    return texts


def raw_view_line(texts):
    """AI 不可用时的兜底：取最后 2 条原文截断拼接。"""
    out = []
    for t in texts[-2:]:
        if len(t) > 60:
            t = t[:60] + "…"
        out.append(t)
    return "；".join(out)


def ai_summarize_one_sentence(texts):
    """用 AI 把当天发言总结成一句话核心观点；失败返回 None 供调用方回退。"""
    if not texts:
        return None
    # 只取最近若干条、每条截断，控制单次 token 与耗时
    sample = [t[:80] for t in texts[-6:]]
    prompt = ("你是财经观点摘要助手。把下面 wu2198 今天的发言总结成一句核心观点，"
              "直接给结论，不超过 45 个字，不要解释、不要序号、不要引用符号：\n" +
              "\n".join("- " + t for t in sample))
    outpath = None
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            auth = json.load(f)
        token = auth.get("token", "")
        base_url = auth.get("baseUrl", "https://mifeng-test.integrity.com.cn")
        if not token:
            print("[WARN] 未找到鉴权 token，观点行回退原文")
            return None
        env = dict(os.environ)
        env["ANTHROPIC_BASE_URL"] = base_url
        env["ANTHROPIC_AUTH_TOKEN"] = token
        env["ANTHROPIC_API_KEY"] = token
        env["ANTHROPIC_MODEL"] = AI_MODEL

        # 输出重定向到临时文件而非管道（避免孙进程持有管道导致超时卡死）
        fd, outpath = tempfile.mkstemp(prefix="view_", suffix=".txt",
                                       dir=os.path.join(SKILL_DIR, "data"))
        os.close(fd)
        logf = open(outpath, "w", encoding="utf-8")
        p = subprocess.Popen(
            [BASH, CLAUDE_SHIM, "-p", prompt, "--output-format", "text"],
            cwd=SKILL_DIR, env=env, stdin=subprocess.DEVNULL,
            stdout=logf, stderr=subprocess.STDOUT,
        )
        try:
            p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            # 杀掉整棵进程树，避免 claude 孤儿进程残留
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True)
            except Exception:
                pass
            print("[WARN] AI 总结超时，观点行回退原文")
            return None
        finally:
            logf.close()

        with open(outpath, encoding="utf-8", errors="ignore") as f:
            out = f.read().strip()
        out = re.sub(r"\s+", " ", out).strip()
        out = re.sub(r"^[\"'“”]+", "", out)
        out = re.sub(r"[\"'“”]+$", "", out)
        if len(out) > 80:
            out = out[:80] + "…"
        return out or None
    except Exception as e:
        print("[WARN] AI 一句话总结失败，回退原文: %s" % e)
        return None
    finally:
        if outpath and os.path.exists(outpath):
            try:
                os.remove(outpath)
            except Exception:
                pass


def key_levels_line():
    """从 alert_levels.json 汇总关键位（确定性，非主观判断）。"""
    try:
        with open(LEVELS_FILE, encoding="utf-8") as f:
            levels = json.load(f)
    except Exception:
        levels = {}
    parts = []
    for name in ("上证指数", "创业板指"):
        entries = levels.get(name, [])
        lv = "/".join(str(e["level"]) for e in entries if e.get("level") is not None)
        if lv:
            parts.append("%s %s" % (DISPLAY.get(name, name), lv))
    return "；".join(parts) or "关键位待更新"


def is_trading_day():
    d = datetime.now(timezone(timedelta(hours=8)))
    if d.weekday() >= 5:
        return False
    return d.strftime("%Y-%m-%d") not in load_holidays(SKILL_DIR)


def build_message(lunch=False):
    now = datetime.now(timezone(timedelta(hours=8)))
    day = now.strftime("%Y-%m-%d")

    idx_lines = []
    for name in INDICES:
        r = query_index(name)
        if not r:
            print("[ABORT] %s 行情查询失败，不发消息" % name)
            return None
        price, chg = r
        chg_txt = fmt_chg(chg)
        if chg_txt:
            chg_txt = "（%s）" % chg_txt
        idx_lines.append("📈 **%s**：%s%s" % (DISPLAY[name], _fmt_num(price), chg_txt))

    turnover = query_amount("两市成交额", "成交额")
    fund = query_amount("两市主力资金净流入", "主力净买入额")
    if turnover is None or fund is None:
        print("[ABORT] 成交额/主力资金查询失败，不发消息")
        return None

    if fund >= 0:
        fund_text = "净流入 " + fmt_yi(fund)
    else:
        fund_text = "净流出 " + fmt_yi(abs(fund))

    texts = wu2198_texts(day, before_time=("11:35" if lunch else None))
    if not texts:
        views = "今日暂无发言"
    else:
        ai_view = ai_summarize_one_sentence(texts)
        views = ai_view if ai_view else raw_view_line(texts)

    title = "每日午间汇总" if lunch else "每日收盘汇总"
    focus_label = "下午关注" if lunch else "明日关注"
    time_label = "%s（午间）" % day if lunch else day

    lines = [
        "📊 **【%s】**" % title,
        "🕐 **时间**：%s" % time_label,
    ] + idx_lines + [
        "💰 **两市成交额**：%s" % fmt_yi(turnover),
        "💸 **主力资金**：%s" % fund_text,
        "💬 **wu2198观点**：%s" % views,
        "🎯 **%s**：%s" % (focus_label, key_levels_line()),
        "---",
        "⚠️ **免责声明**：以上内容仅为信息整理与观点复盘，仅供参考，不构成投资建议。",
    ]
    return "\n".join(lines)


def _fmt_num(v):
    """价格保留两位小数，去掉多余 0。"""
    return ("%.2f" % v).rstrip("0").rstrip(".")


def send(msg, dry_run=False):
    if dry_run:
        print("---- DRY-RUN（不发群）----")
        print(msg)
        print("--------------------------")
        return True
    try:
        subprocess.run([BASH, NOTIFY, msg], capture_output=True, timeout=30, cwd=SKILL_DIR)
        print("[OK] 汇总已发送")
        return True
    except Exception as e:
        print("[WARN] 发送失败: %s" % e)
        return False


def already_sent(key):
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return key in f.read().splitlines()
    except Exception:
        return False


def mark_sent(key):
    try:
        with open(STATE_FILE, "a", encoding="utf-8") as f:
            f.write(key + "\n")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="每日收盘/午间汇总（零 token 后端版）")
    ap.add_argument("--lunch", action="store_true", help="午间汇总（11:35 前发言）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不发群")
    args = ap.parse_args()

    if not is_trading_day():
        print("[SKIP] 非交易日，跳过")
        return

    period = "lunch" if args.lunch else "close"
    key = "%s|%s" % (datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"), period)
    if already_sent(key):
        print("[SKIP] %s 已发送过，跳过（防重复）" % key)
        return

    msg = build_message(lunch=args.lunch)
    if msg is None:
        sys.exit(1)
    if send(msg, dry_run=args.dry_run) and not args.dry_run:
        mark_sent(key)


if __name__ == "__main__":
    main()

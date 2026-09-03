#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wu2198 五号群 消息自动同步（合并版：飞书群拉取 + 大V发言分析）

功能：
  1. 通过 lark-cli（OAuth 用户授权）拉取 wu2198五号群 的机器人消息（即 wu2198 的发言）
  2. 增量拉取：只记住「最后一次拉取的群消息时间」，仅拉取该时间之后的新消息
  3. 按 97% 文本相似度去重后，增量导入 kol-opinion-analyzer 的 SQLite 数据库
  4. 测试消息自动跳过
  5. 报告本次同步结果（新增导入条数、跳过条数）
  6. 有新增时自动导出 JSON 并推送到 GitHub
  7. VIP 消息（内容含「仅TA的真爱粉可见」）实时推送到「荔枝种植交流群」，失败自动补推

盘中时间定义：交易日 9:00-11:30 / 13:00-15:00（9:00-9:30 也算盘中），
另在盘后 16:00 兜底同步一次；其余时间自动跳过。
盘中高频轮询：由 Bee 定时任务每 30 秒触发一次；为控制单次耗时，
授权状态最多每小时检查一次、git pull 每 10 分钟才拉一次、各网络调用都收紧超时。

用法:
  python sync_feishu_auto.py                # 正常同步（盘中 + 交易日守卫）
  python sync_feishu_auto.py --force        # 忽略盘中/交易日守卫，强制同步
  python sync_feishu_auto.py --no-push      # 不同步到 GitHub
  python sync_feishu_auto.py --dry-run      # 只预览，不写库不推送
  python sync_feishu_auto.py --reset-watermark  # 重置增量水位（下次重新全量拉取）
"""
import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

from records_hash import content_hash
from ocr_image import ocr
from common import find_bash, load_holidays, pythonw_path

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(SKILL_DIR, "data", "kol_opinions.db")
SYNC_SCRIPT = os.path.join(SKILL_DIR, "scripts", "sync.py")
STATE_PATH = os.path.join(SKILL_DIR, "sync", "feishu_sync_state.json")
ERROR_LOG = os.path.join(SKILL_DIR, "data", "sync_errors.log")
AUTH_CACHE_FILE = os.path.join(SKILL_DIR, "data", "_last_auth_check.txt")
AUTH_CHECK_INTERVAL = 3600  # 30 秒高频轮询下，授权状态最多每小时检查一次
LOOP_LOCK_FILE = os.path.join(SKILL_DIR, "data", "_feishu_loop.lock")
LOOP_STALE_SEC = 180  # 循环锁超过 180 秒未心跳视为残留，可被接管

# Git Bash 的 bash.exe 完整路径（Windows 下 subprocess 调 "bash" 会误调 WSL bash 而失败）
BASH = find_bash()

LOCAL_CONFIG_ENV = os.path.join(SKILL_DIR, "data", "local_config.env")


def _env_value(key, default=""):
    """从 data/local_config.env 读取单个 KEY=VALUE，缺失回退默认值。"""
    try:
        with open(LOCAL_CONFIG_ENV, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip()
    except Exception:
        pass
    return default


DEFAULT_CHAT_ID = _env_value("WU2198_CHAT_ID")
VIP_PUSH_CHAT_ID = _env_value("VIP_PUSH_CHAT_ID")
REVIEW_CHAT_ID = _env_value("REVIEW_CHAT_ID")


def _load_vip_markers():
    """VIP 消息标记词，可从 local_config.env 的 VIP_MARKERS（逗号分隔）覆盖。"""
    raw = _env_value("VIP_MARKERS", "")
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    return ["仅TA的真爱粉可见"]


VIP_MARKERS = _load_vip_markers()
BOT_SENDER_TYPES = ("app", "bot")          # 机器人消息（wu2198 发言由自定义机器人发出）
TEST_KEYWORDS = ["转发测试", "同步测试", "设备A同步测试", "test", "TEST"]

# 节假日从 common.load_holidays(skill_dir) 加载（硬编码兜底 + data/holidays.txt）


def find_lark_cli():
    """定位 lark-cli 可执行文件（兼容 PATH 与常见全局安装目录）"""
    p = shutil.which("lark-cli")
    if p:
        return p
    candidates = [
        os.path.expandvars(r"%APPDATA%\npm\lark-cli"),
        os.path.expandvars(r"%APPDATA%\npm\lark-cli.cmd"),
        os.path.expanduser("~/.npm-global-user/lark-cli"),
        "/usr/local/bin/lark-cli",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return "lark-cli"


def check_auth(lark_cli):
    """检查 lark-cli 用户授权状态，临近过期时告警（不阻塞）。

    30 秒高频轮询下不必每次都查授权：成功检查后缓存时间戳，最多每小时查一次；
    检查失败则不缓存，下一轮自动重试。
    """
    try:
        if os.path.exists(AUTH_CACHE_FILE):
            with open(AUTH_CACHE_FILE, "r") as f:
                last = float(f.read().strip() or "0")
            if time.time() - last < AUTH_CHECK_INTERVAL:
                return  # 最近已检查过，跳过（节省单次耗时）
    except Exception:
        pass
    try:
        # 走 Git Bash POSIX 版 lark-cli（Windows 的 .CMD 版本会卡死）
        tmp = os.path.join(SKILL_DIR, "data", "_lark_auth_out.json")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        cmd = " ".join(shlex.quote(p) for p in ["timeout", "-k", "3", "15", "lark-cli", "auth", "status"]) + " > data/_lark_auth_out.json 2>&1"
        subprocess.run([BASH, "-c", cmd], capture_output=True, timeout=20, cwd=SKILL_DIR)
        with open(tmp, "r", encoding="utf-8") as f:
            data = json.loads(f.read())
    except Exception as e:
        print("[AUTH] 无法检查授权状态: %s" % e)
        return  # 失败不缓存，下一轮重试
    try:
        os.makedirs(os.path.dirname(AUTH_CACHE_FILE), exist_ok=True)
        with open(AUTH_CACHE_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass
    user = (data.get("identities") or {}).get("user") or {}
    expires = user.get("refreshExpiresAt") or user.get("expiresAt") or ""
    if not expires:
        print("[AUTH] ⚠️ 未检测到用户授权，请执行 lark-cli auth login")
        return
    try:
        exp = datetime.fromisoformat(expires)
        now8 = datetime.now(timezone(timedelta(hours=8)))
        days = (exp - now8).days
        if days < 0:
            print("[AUTH] ⚠️ 授权已过期，请重新执行 lark-cli auth login")
            alert_feishu("授权过期", "🚨 **【授权告警】**\nlark-cli 授权已过期，请重新执行 lark-cli auth login 扫码授权")
        elif days <= 3:
            print("[AUTH] ⚠️ 授权将在 %d 天后过期(%s)，请提前重新授权" % (days, expires[:10]))
            alert_feishu("授权过期", "🚨 **【授权告警】**\nlark-cli 授权将在 %d 天后过期(%s)，请提前重新扫码授权" % (days, expires[:10]))
        else:
            print("[AUTH] 授权正常，%s 到期" % expires[:10])
    except Exception:
        pass


def trading_time_guard():
    """交易日(含节假日) + 盘中/盘后时间守卫（北京时间）。返回 (是否可运行, 原因)"""
    now = datetime.now(timezone(timedelta(hours=8)))
    if now.weekday() >= 5:
        return False, "非交易日（周末）"
    if now.strftime("%Y-%m-%d") in load_holidays(SKILL_DIR):
        return False, "非交易日（节假日）"
    hm = now.hour * 100 + now.minute
    # 盘中 9:00-11:30（9:00-9:30 也算盘中）/ 13:00-16:00（收盘后延长至 16:00）
    if (900 <= hm <= 1130) or (1300 <= hm <= 1600):
        return True, ""
    return False, "非盘中/盘后时间（当前 %02d:%02d）" % (now.hour, now.minute)


def to_iso(ts):
    """把水位时间转成 ISO8601（北京时间）；兼容 'YYYY-MM-DD HH:MM[:SS]' 和 ISO8601。"""
    ts = (ts or "").strip()
    if not ts:
        return None
    if "T" in ts:
        # 已是 ISO8601（可能带毫秒/时区）：去掉 Z/毫秒/时区后缀，截取前 19 位
        s = ts.replace("Z", "").replace("z", "")
        if "+" in s:
            s = s.split("+", 1)[0]
        s = s[:19]
        if len(s) == 19:
            return s + "+08:00"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(ts, fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"
        except Exception:
            continue
    return None


def pull_latest():
    """从 GitHub 拉取最新同步数据（含水位），失败不阻塞；10 分钟内不重复拉（高频同步用）"""
    cache_file = os.path.join(SKILL_DIR, "data", "_last_pull.txt")
    try:
        if os.path.exists(cache_file):
            with open(cache_file, "r") as f:
                last = float(f.read().strip() or "0")
            if time.time() - last < 600:
                return  # 10 分钟内已拉过，跳过
    except Exception:
        pass
    try:
        r = subprocess.run(["git", "-C", SKILL_DIR, "pull", "--rebase"],
                           capture_output=True, text=True, timeout=20)
        with open(cache_file, "w") as f:
            f.write(str(time.time()))
        if r.returncode == 0:
            print("[PULL] 已拉取最新同步数据（含水位）")
        else:
            msg = (r.stderr or r.stdout).strip().splitlines()
            print("[PULL] 拉取失败（继续使用本地水位）: %s" % (msg[-1][:120] if msg else "unknown"))
    except Exception as e:
        print("[PULL] 拉取异常（继续使用本地水位）: %s" % e)


def load_watermark(db_path, kol_name="wu2198"):
    """加载增量水位：优先 sync/ 状态文件，回退到 DB 中飞书群最新记录时间，再回退 None"""
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            t = d.get("last_message_time")
            if t:
                return t
        except Exception:
            pass
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT MAX(record_date) FROM kol_records WHERE kol_name=? AND platform='飞书群'", (kol_name,))
            t = cur.fetchone()[0]
            conn.close()
            if t:
                return t
        except Exception:
            pass
    return None


def save_watermark(t):
    """保存增量水位（最后拉取的群消息时间）"""
    if not t:
        return
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"last_message_time": t}, f, ensure_ascii=False)
    except Exception as e:
        print("[WARN] 保存水位失败: %s" % e)


def log_error(msg):
    """记录同步错误到 data/sync_errors.log（带时间戳；超 512KB 自动轮转为 .old）"""
    try:
        os.makedirs(os.path.dirname(ERROR_LOG), exist_ok=True)
        if os.path.exists(ERROR_LOG) and os.path.getsize(ERROR_LOG) > 512 * 1024:
            try:
                os.replace(ERROR_LOG, ERROR_LOG + ".old")
            except Exception:
                pass
        ts = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (ts, msg))
    except Exception:
        pass


def alert_feishu(key, msg):
    """同步/技术告警通过飞书机器人私信（alert_once_private.sh 去重，当天同一类只提醒一次）"""
    try:
        day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        subprocess.run(
            [BASH, os.path.join(SKILL_DIR, "scripts", "alert_once_private.sh"),
             "%s_%s" % (key, day), "below", msg],
            capture_output=True, text=True, timeout=30, cwd=SKILL_DIR)
    except Exception:
        pass


FAIL_COUNT_FILE = os.path.join(SKILL_DIR, "data", "_feishu_pull_fail_count.txt")
REVIEW_FORWARD_WATERMARK = os.path.join(SKILL_DIR, "data", "_review_forward_watermark.txt")


def _record_pull_fail():
    """拉取失败计数 +1，返回当前连续失败次数。"""
    n = 0
    try:
        with open(FAIL_COUNT_FILE, encoding="utf-8") as f:
            n = int(f.read().strip() or "0")
    except Exception:
        pass
    n += 1
    try:
        with open(FAIL_COUNT_FILE, "w", encoding="utf-8") as f:
            f.write(str(n))
    except Exception:
        pass
    return n


def _reset_pull_fail():
    try:
        with open(FAIL_COUNT_FILE, "w", encoding="utf-8") as f:
            f.write("0")
    except Exception:
        pass


def _commit_with_retry(conn, retries=5):
    """提交事务，遇 database is locked 指数退避重试。"""
    delay = 1.0
    for i in range(retries):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and i < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


WEEKDAYS_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def fmt_vip_time(ct):
    """'YYYY-MM-DD HH:MM[:SS]' -> '星期X YYYY-MM-DD HH:MM[:SS]'（兼容无秒）"""
    ts = (ct or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(ts, fmt)
            return "%s %s" % (WEEKDAYS_CN[dt.weekday()], ts)
        except Exception:
            continue
    return ct


def is_vip_text(text):
    """判断是否为 VIP 消息（含任一 VIP 标记词）。"""
    return bool(text) and any(m in text for m in VIP_MARKERS)


def strip_vip_markers(text):
    """去掉正文里的 VIP 标记（含【】和裸词），让推送排版更干净。"""
    t = text or ""
    for m in VIP_MARKERS:
        t = t.replace("【%s】" % m, "").replace(m, "")
    return t.strip()


def push_vip_to_group(text, ct):
    """VIP 消息推送到群（荔枝种植交流群），返回是否成功"""
    try:
        # 去掉正文里重复的 VIP 标记，让排版更干净
        body = strip_vip_markers(text)
        msg = ("🔒 VIP・仅TA的真爱粉可见\n"
               f"\n"
               f"🕐{fmt_vip_time(ct)}\n"
               f"\n"
               f"{body}")
        # 写入临时文件（避免命令行传中文/多行在 Windows 下编码损坏）
        tmp = os.path.join(SKILL_DIR, "data", "_vip_push_tmp.txt")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(msg)
        # 用 BASH -c 内联执行，捕获输出判断是否成功（lark-cli 返回 ok:true 即成功）；失败重试 3 次
        # 幂等键：同一(内容+时间)只发一次，防止重试/补推重复发
        idem_key = "vip_" + content_hash(text + "|" + (ct or ""))
        cmd = ('timeout -k 3 30 lark-cli im +messages-send '
               '--chat-id %s '
               '--idempotency-key %s '
               '--as bot --markdown "$(cat data/_vip_push_tmp.txt)"' % (VIP_PUSH_CHAT_ID, idem_key))
        ok = False
        last_err = ""
        for attempt in range(3):
            r = subprocess.run([BASH, "-c", cmd], capture_output=True, timeout=50, cwd=SKILL_DIR)
            out = (r.stdout or b"") + (r.stderr or b"")
            if b'"ok": true' in out or b'"ok":true' in out:
                ok = True
                break
            last_err = out.decode("utf-8", "ignore")[:200]
            if attempt < 2:
                time.sleep(2)
        try:
            os.remove(tmp)
        except Exception:
            pass
        if not ok:
            log_error("VIP 消息推送失败: %s | err=%s" % (ct, last_err))
            alert_feishu("VIP推送失败", "🔒 **【VIP推送告警】**\n有 VIP 消息推送到「荔枝种植交流群」失败，将在后续轮次自动补推。\n🕐 %s" % ct)
        return ok
    except Exception as e:
        log_error("VIP 推送异常: %s" % e)
        return False


def _send_review_image(image_path, ct):
    """转发本地图片到每日复盘群，返回是否成功。"""
    try:
        full = image_path if os.path.isabs(image_path) else os.path.join(SKILL_DIR, image_path)
        if not os.path.exists(full):
            log_error("复盘群图片转发跳过（本地文件不存在）: %s" % image_path)
            return False
        rel = image_path.replace("\\", "/")
        idem_key = "review_img_" + content_hash(image_path + "|" + (ct or ""))
        cmd = ('timeout -k 3 30 lark-cli im +messages-send '
               '--chat-id %s --idempotency-key %s --as bot --image "%s"'
               % (REVIEW_CHAT_ID, idem_key, rel))
        ok = False
        last_err = ""
        for attempt in range(3):
            r = subprocess.run([BASH, "-c", cmd], capture_output=True, timeout=50, cwd=SKILL_DIR)
            out = (r.stdout or b"") + (r.stderr or b"")
            if b'"ok": true' in out or b'"ok":true' in out:
                ok = True
                break
            last_err = out.decode("utf-8", "ignore")[:200]
            if attempt < 2:
                time.sleep(2)
        if not ok:
            log_error("复盘群图片转发失败: %s | err=%s" % (ct, last_err))
        return ok
    except Exception as e:
        log_error("复盘群图片转发异常: %s" % e)
        return False


def push_to_review_group(text, ct, is_vip=False, image_path=""):
    """把 wu2198 发言转发到每日复盘群，返回是否成功。

    - 图片消息：转发本地图片文件
    - VIP 消息：🔒 VIP 格式
    - 普通消息：🕐 时间 + 正文
    """
    try:
        if image_path:
            return _send_review_image(image_path, ct)
        body = strip_vip_markers(text)
        if is_vip:
            msg = ("🔒 VIP・仅TA的真爱粉可见\n\n🕐%s\n\n%s" % (fmt_vip_time(ct), body))
        else:
            msg = ("🕐%s\n\n%s" % (fmt_vip_time(ct), body))
        tmp = os.path.join(SKILL_DIR, "data", "_review_push_tmp.txt")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(msg)
        idem_key = "review_" + content_hash(text + "|" + (ct or ""))
        cmd = ('timeout -k 3 30 lark-cli im +messages-send '
               '--chat-id %s '
               '--idempotency-key %s '
               '--as bot --markdown "$(cat data/_review_push_tmp.txt)"' % (REVIEW_CHAT_ID, idem_key))
        ok = False
        last_err = ""
        for attempt in range(3):
            r = subprocess.run([BASH, "-c", cmd], capture_output=True, timeout=50, cwd=SKILL_DIR)
            out = (r.stdout or b"") + (r.stderr or b"")
            if b'"ok": true' in out or b'"ok":true' in out:
                ok = True
                break
            last_err = out.decode("utf-8", "ignore")[:200]
            if attempt < 2:
                time.sleep(2)
        try:
            os.remove(tmp)
        except Exception:
            pass
        if not ok:
            log_error("复盘群转发失败: %s | err=%s" % (ct, last_err))
        return ok
    except Exception as e:
        log_error("复盘群转发异常: %s" % e)
        return False


def forward_all_to_review_group(conn):
    """把 wu2198 当天(及之后)的发言转发到每日复盘群（VIP+公开+图片，跳过测试消息）。

    用 data/_review_forward_watermark.txt 记录已转发的最后一条 record_date；
    首次运行默认从今天 00:00 起（只补今天，不补历史）。
    """
    if not REVIEW_CHAT_ID:
        return
    today00 = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d 00:00")
    wm = today00
    try:
        with open(REVIEW_FORWARD_WATERMARK, encoding="utf-8") as f:
            v = f.read().strip()
            if v:
                wm = v
    except Exception:
        pass
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT content, record_date, is_vip, image_path FROM kol_records WHERE kol_name='wu2198' AND record_date > ? ORDER BY record_date ASC",
        (wm,)).fetchall()
    last_ct = wm
    for content, ct, is_vip, image_path in rows:
        if content and is_test_message(content):
            last_ct = ct
            continue
        if push_to_review_group(content or "", ct, is_vip=bool(is_vip), image_path=image_path or ""):
            last_ct = ct
        else:
            # 图片本地不存在时也算「已处理」，前移水位避免每轮重试同一张；下一条继续
            last_ct = ct
    try:
        with open(REVIEW_FORWARD_WATERMARK, "w", encoding="utf-8") as f:
            f.write(last_ct)
    except Exception:
        pass


def fetch_messages_since(lark_cli=None, chat_id=None, start_iso=None):
    """通过 Git Bash POSIX 版 lark-cli 拉取 start_iso 之后的消息（升序，自动分页）。

    Windows 的 lark-cli.CMD 版本「输出后进程不退出」会卡死，所以走 bash -c 调 POSIX 版。
    """
    tmp = os.path.join(SKILL_DIR, "data", "_lark_chat_out.json")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    parts = ["timeout", "-k", "3", "60", "lark-cli", "im", "+chat-messages-list",
             "--chat-id", chat_id, "--as", "user", "--order", "asc",
             "--page-all", "--page-limit", "1000", "--no-reactions", "--json"]
    if start_iso:
        parts += ["--start", start_iso]
    cmd = " ".join(shlex.quote(p) for p in parts) + " > data/_lark_chat_out.json 2>&1"
    try:
        subprocess.run([BASH, "-c", cmd], capture_output=True, timeout=75, cwd=SKILL_DIR)
    except subprocess.TimeoutExpired:
        print("[ERROR] lark-cli 拉取超时")
        log_error("lark-cli 拉取超时")
        return None
    except Exception as e:
        print("[ERROR] lark-cli 拉取异常: %s" % str(e)[:200])
        log_error("lark-cli 拉取异常: %s" % str(e)[:200])
        return None
    try:
        with open(tmp, "r", encoding="utf-8") as f:
            out = f.read()
    except Exception:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        print("[ERROR] 解析 lark-cli 输出失败")
        log_error("解析 lark-cli 输出失败")
        return None
    if not data.get("ok"):
        err = json.dumps(data.get("error", {}), ensure_ascii=False)[:200]
        print("[ERROR] lark-cli 返回异常: %s" % err)
        log_error("lark-cli 返回异常: %s" % err)
        return None
    return data.get("data", {}).get("messages", []) or []


def extract_text(msg):
    """从消息 item 提取纯文本（仅 text 类型）"""
    if msg.get("msg_type") != "text":
        return None
    c = msg.get("content", "")
    if isinstance(c, str) and c.strip().startswith("{"):
        try:
            o = json.loads(c)
            return (o.get("text", "") or "").strip()
        except Exception:
            return c.strip()
    return c.strip() if isinstance(c, str) else ""


IMG_KEY_RE = re.compile(r"img_[A-Za-z0-9_-]+")


def extract_image_key(msg):
    """从图片消息提取 image_key（用于去重与后续下载）"""
    if msg.get("msg_type") != "image":
        return ""
    c = msg.get("content", "") or ""
    m = IMG_KEY_RE.search(c)
    return m.group(0) if m else ""


def download_image(lark_cli, message_id, image_key):
    """下载图片到 assets/feishu_images/，成功返回相对路径，失败返回空串"""
    rel = "assets/feishu_images/" + image_key
    try:
        r = subprocess.run(
            [lark_cli, "im", "+messages-resources-download",
             "--message-id", message_id, "--file-key", image_key,
             "--type", "image", "--output", rel, "--json"],
            capture_output=True, text=True, timeout=60, cwd=SKILL_DIR)
        if r.returncode == 0:
            # 实际文件名可能带扩展名，回退用 key 作为路径
            for fn in os.listdir(os.path.join(SKILL_DIR, "assets", "feishu_images")):
                if fn.startswith(image_key):
                    return os.path.join("assets", "feishu_images", fn)
            return rel
        return ""
    except Exception:
        return ""


def is_test_message(text):
    """测试消息判定：命中关键词则跳过（不入库、不同步）。"""
    if not text:
        return True
    for kw in TEST_KEYWORDS:
        if kw in text:
            return True
    return False


POS_SIZE_RE = re.compile(r"持仓[仅剩]?(\d+)\s*米")


def extract_position(text):
    """从发言中提取仓位信号，返回 (size, action, note)。

    仅匹配明确的「持仓N米」或「清仓」表述（wu2198 用「米」表示仓位），保守避免误判。
    """
    if not text:
        return None, "", ""
    m = POS_SIZE_RE.search(text)
    if m:
        size = int(m.group(1))
    elif re.search(r"清仓", text):
        size = 0
    else:
        return None, "", ""
    if re.search(r"清仓", text):
        action = "清仓"
    elif re.search(r"兑现|减仓|减到", text):
        action = "减仓"
    elif re.search(r"加仓|买进|买入", text):
        action = "加仓"
    else:
        action = "持有"
    return size, action, text.strip()[:40]


def ensure_schema(conn):
    """确保 kol_records 表存在，不存在则调用 db_init.py 初始化"""
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='kol_records'")
    if cur.fetchone() is None:
        print("[INIT] 数据库表不存在，初始化中 ...")
        subprocess.run([pythonw_path(), os.path.join(SKILL_DIR, "scripts", "db_init.py")],
                       capture_output=True, timeout=60)


def _acquire_loop_lock():
    """原子获取循环锁，防止多个 --loop 进程同时轮询；返回 True 表示获得锁。"""
    try:
        fd = os.open(LOOP_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(time.time()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            with open(LOOP_LOCK_FILE, "r") as f:
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
        return True  # 锁机制异常时不阻塞同步


def _touch_loop_lock():
    try:
        with open(LOOP_LOCK_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _release_loop_lock():
    try:
        if os.path.exists(LOOP_LOCK_FILE):
            os.remove(LOOP_LOCK_FILE)
    except Exception:
        pass


def run_loop(args):
    """盘中高频循环：每 interval 秒同步一次，直到非盘中时间自动退出。"""
    interval = max(5, args.interval)
    if not _acquire_loop_lock():
        print("[LOOP] 已有同步循环在运行，本次跳过")
        return
    print("[LOOP] 盘中循环同步启动：每 %d 秒一次" % interval)
    try:
        while True:
            _touch_loop_lock()
            if not args.force:
                ok, reason = trading_time_guard()
                if not ok:
                    print("[LOOP] %s，循环退出" % reason)
                    return
            t0 = time.time()
            try:
                run_once(args, skip_guard=True)
            except Exception as e:
                print("[LOOP] 单次同步异常: %s" % e)
                log_error("盘中循环单次同步异常: %s" % e)
            elapsed = time.time() - t0
            time.sleep(max(0, interval - elapsed))
    finally:
        _release_loop_lock()


def run_once(args, skip_guard=False):
    """执行一次完整同步（拉取→去重→入库→VIP推送→GitHub 推送）。返回 0=正常/跳过，1=拉取失败。"""
    if not args.force and not skip_guard:
        ok, reason = trading_time_guard()
        if not ok:
            print("[SKIP] %s" % reason)
            return 0

    lark_cli = find_lark_cli()
    check_auth(lark_cli)
    if not args.no_pull:
        pull_latest()
    watermark = load_watermark(args.db, args.kol_name)
    start_iso = to_iso(watermark) if watermark else None

    print("=" * 56)
    print("  wu2198五号群 消息自动同步")
    print("  时间: %s" % datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"))
    print("  群ID: %s" % args.chat_id)
    print("  增量水位: %s" % (watermark or "无（全量拉取）"))
    print("=" * 56)

    # 1. 只拉取水位之后的消息
    messages = fetch_messages_since(lark_cli, args.chat_id, start_iso)
    if messages is None:
        print("[FAIL] 拉取失败，本轮结束")
        n = _record_pull_fail()
        if n == 3:
            alert_feishu("拉取失败", "🚨 **【同步告警】**\n飞书群消息已连续 3 次拉取失败，请检查 lark-cli 授权或网络")
        return 1
    _reset_pull_fail()
    print("[1/5] 拉到 %d 条新消息" % len(messages))

    # 2. 过滤机器人消息（水位只按机器人消息前移，避免群里闲聊/系统消息触发高频 GitHub 推送）
    bot_msgs = []
    for m in messages:
        s = m.get("sender") or {}
        stype = s.get("sender_type", "")
        sname = s.get("name", "")
        if stype not in BOT_SENDER_TYPES and sname != args.bot_name:
            continue
        bot_msgs.append(m)

    # 3. 计算新水位 = 所有机器人消息的最大 create_time
    new_watermark = watermark
    if bot_msgs:
        times = [m.get("create_time", "") for m in bot_msgs if m.get("create_time")]
        if times:
            latest = max(times)
            if not new_watermark or latest > new_watermark:
                new_watermark = latest

    # 3b. 提取文本/图片
    bot_texts = []
    bot_images = []
    for m in bot_msgs:
        t = extract_text(m)
        if t:
            bot_texts.append((m.get("create_time", ""), t))
        else:
            img_key = extract_image_key(m)
            if img_key:
                bot_images.append((m.get("create_time", ""), m.get("message_id", ""), img_key))
    print("[3/5] 机器人文本消息 %d 条 / 图片消息 %d 条" % (len(bot_texts), len(bot_images)))

    # 4. 入库（按 content_hash 精确去重；增量依据水位；测试消息跳过）
    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    ensure_schema(conn)
    cur = conn.cursor()

    inserted = 0
    dup_skipped = 0
    test_skipped = 0
    empty_skipped = 0
    seen_hashes = set()

    for ct, text in reversed(bot_texts):  # 旧在前
        if not text:
            empty_skipped += 1
            continue
        if is_test_message(text):
            test_skipped += 1
            continue
        h = content_hash(text)
        # 精确去重：先查本次批量，再查库（走 content_hash 唯一索引，O(1)）
        if h in seen_hashes:
            dup_skipped += 1
            continue
        if not args.dry_run:
            cur.execute("SELECT 1 FROM kol_records WHERE kol_name=? AND content_hash=? LIMIT 1", (args.kol_name, h))
            if cur.fetchone():
                dup_skipped += 1
                continue
        seen_hashes.add(h)
        if not args.dry_run:
            vip = 1 if is_vip_text(text) else 0
            pos_size, pos_action, pos_note = extract_position(text)
            cur.execute("""INSERT INTO kol_records
                (kol_name, platform, content, extracted_viewpoints, related_assets,
                 record_date, position_size, position_action, position_note, is_vip, content_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (args.kol_name, "飞书群", text, "", "", ct, pos_size, pos_action,
                 pos_note or "飞书群自动同步", vip, h))
            if vip:
                if push_vip_to_group(text, ct):
                    cur.execute("UPDATE kol_records SET vip_pushed=1 WHERE id=?", (cur.lastrowid,))
        inserted += 1

    # 4b. 图片消息入库（按 image_key 精确去重）
    img_inserted = 0
    img_dup = 0
    if not args.dry_run:
        cur.execute("SELECT image_path FROM kol_records WHERE kol_name=? AND image_path != ''", (args.kol_name,))
        seen_img = {r[0] for r in cur.fetchall() if r[0]}
    else:
        seen_img = set()
    for ct, mid, img_key in reversed(bot_images):
        if img_key in seen_img:
            img_dup += 1
            continue
        local_path = ""
        ocr_text = ""
        if not args.dry_run and args.download_images:
            local_path = download_image(lark_cli, mid, img_key)
            if local_path:
                # 下载成功后尝试 OCR（未装 tesseract 时静默返回空串）
                ocr_text = ocr(os.path.join(SKILL_DIR, local_path))
        if not args.dry_run:
            extracted = ("OCR: " + ocr_text[:2000]) if ocr_text else ""
            cur.execute("""INSERT INTO kol_records
                (kol_name, platform, content, extracted_viewpoints, related_assets,
                 record_date, position_size, position_action, position_note, image_path, is_vip, content_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (args.kol_name, "飞书群", "[图片消息]", extracted, "", ct, None, "", "飞书群图片",
                 local_path or img_key, 0, content_hash("[图片消息]", image_path=img_key)))
        seen_img.add(img_key)
        img_inserted += 1

    # 4c. 补推未推送成功的 VIP 消息（数据已入库，推送失败的下次自动补）
    if not args.dry_run:
        cur.execute("SELECT id, content, record_date FROM kol_records WHERE kol_name=? AND is_vip=1 AND vip_pushed=0 ORDER BY id ASC", (args.kol_name,))
        pending_vip = cur.fetchall()
        for pid, pcontent, pct in pending_vip:
            if push_vip_to_group(pcontent, pct):
                cur.execute("UPDATE kol_records SET vip_pushed=1 WHERE id=?", (pid,))

    if not args.dry_run:
        _commit_with_retry(conn)
        # 4d. 转发全部消息（VIP+公开）到每日复盘群，仅今天起
        forward_all_to_review_group(conn)
        # 5. 保存新水位（记录本次拉取到的最新群消息时间）
        if new_watermark:
            save_watermark(new_watermark)
        if new_watermark != watermark:
            print("[5/5] 水位已更新: %s -> %s" % (watermark or "无", new_watermark))
        else:
            print("[5/5] 无新消息，水位保持: %s" % (new_watermark or "无"))
    total = cur.execute("SELECT COUNT(*) FROM kol_records WHERE kol_name=?", (args.kol_name,)).fetchone()[0]
    conn.close()
    print("[4/5] 入库完成")

    # 6. GitHub 推送（有新记录，或水位前移时也推送，保证多设备水位一致）
    watermark_advanced = bool(new_watermark and new_watermark != watermark)
    push_ok = None
    if not args.dry_run and not args.no_push and (inserted > 0 or img_inserted > 0 or watermark_advanced):
        print("[6/6] 有新增，导出并推送到 GitHub ...")
        push_ok = None
        try:
            r = subprocess.run([pythonw_path(), SYNC_SCRIPT, "push"], capture_output=True, text=True, timeout=180)
            push_ok = r.returncode == 0
            tail = (r.stdout + r.stderr).strip().splitlines()
        except Exception as e:
            print("      [WARN] sync.py push 异常: %s" % str(e)[:200])
            log_error("GitHub 推送异常: %s" % str(e)[:200])
            tail = []
        if not push_ok:
            log_error("GitHub 推送失败")
            alert_feishu("推送失败", "🚨 **【同步告警】**\nGitHub 推送失败（已自动重试），请检查网络")
        for line in tail[-6:]:
            print("      " + line)
    else:
        print("[6/6] 跳过推送（dry-run=%s, inserted=%d, no-push=%s）"
              % (args.dry_run, inserted, args.no_push))

    # 7. 报告
    print("\n" + "=" * 56)
    print("  同步结果报告")
    print("=" * 56)
    print("  增量水位: %s" % (watermark or "无"))
    print("  新增导入条数: %d" % inserted)
    if img_inserted or img_dup:
        print("  图片消息: 新增 %d 条 / 跳过 %d 条" % (img_inserted, img_dup))
    print("  跳过条数: %d（重复: %d / 测试消息: %d / 空消息: %d）"
          % (dup_skipped + test_skipped + empty_skipped, dup_skipped, test_skipped, empty_skipped))
    print("  数据库 wu2198 总条数: %d" % total)
    if push_ok is not None:
        print("  GitHub 推送: %s" % ("成功" if push_ok else "失败（见上方日志）"))
    print("=" * 56)
    return 0


def main():
    ap = argparse.ArgumentParser(description="wu2198五号群消息自动同步（合并版）")
    ap.add_argument("--chat-id", default=DEFAULT_CHAT_ID)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--force", action="store_true", help="忽略盘中/交易日守卫")
    ap.add_argument("--no-push", action="store_true", help="跳过 GitHub 推送")
    ap.add_argument("--dry-run", action="store_true", help="只预览不写库")
    ap.add_argument("--reset-watermark", action="store_true", help="重置增量水位，下次全量拉取")
    ap.add_argument("--no-pull", action="store_true", help="同步前不拉取最新水位")
    ap.add_argument("--download-images", action="store_true", help="同步时下载图片到 assets/feishu_images/")
    ap.add_argument("--kol-name", default="wu2198", help="KOL 名称（默认 wu2198）")
    ap.add_argument("--bot-name", default="自定义机器人", help="发言机器人名称（默认 自定义机器人）")
    ap.add_argument("--loop", action="store_true", help="盘中高频循环模式（每 --interval 秒同步一次）")
    ap.add_argument("--interval", type=int, default=30, help="--loop 模式下的拉取间隔秒数（默认 30）")
    args = ap.parse_args()

    if not DEFAULT_CHAT_ID or not VIP_PUSH_CHAT_ID:
        print("[CONFIG] ⚠️ 未配置 data/local_config.env 中的 WU2198_CHAT_ID / VIP_PUSH_CHAT_ID，同步/推送可能失败")

    if args.reset_watermark:
        if os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)
            print("[OK] 已重置增量水位（下次将全量拉取）")
        else:
            print("[INFO] 无水位文件，无需重置")
        return

    if args.loop:
        run_loop(args)
        return

    sys.exit(run_once(args))


if __name__ == "__main__":
    main()

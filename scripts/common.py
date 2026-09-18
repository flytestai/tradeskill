#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公共工具：bash 路径、节假日、文本归一化、DB 连接（消除各脚本重复）。"""
import json
import os
import platform
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys

# ============================================================================
# 平台抽象层（P0）
# ----------------------------------------------------------------------------
# 目的：让同一份代码在 Windows / Linux 都能跑，业务脚本不再直接写平台相关代码。
# 注意：Windows 行为与改造前**完全一致**（NO_WINDOW 等取值不变），
#       Linux 上自动降级为 no-op，不影响任何现有逻辑。
# ============================================================================

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

# Windows 下子进程静默运行，不弹黑窗（Linux 上为 0）
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def detach_flags():
    """子进程「完全脱离父进程」标志：Windows 专有；Linux 返回 0。

    用于 supervisor 之类需要后台常驻的场景。
    """
    if not IS_WINDOWS:
        return 0
    return (getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0))


def no_window_flag():
    """子进程静默标志：Windows 返回 CREATE_NO_WINDOW；Linux 返回 0。"""
    if not IS_WINDOWS:
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def timeout_wrap(seconds, cmd_parts):
    """跨平台 timeout 包装。

    返回可直接传给 shell 的字符串（Windows 走 Git Bash 的 timeout）
    或参数列表（Linux 走 coreutils timeout）。

    >>> timeout_wrap(20, ["lark-cli", "auth", "status"])
    """
    if IS_WINDOWS:
        return "timeout -k 3 %d %s" % (seconds, " ".join(shlex.quote(str(c)) for c in cmd_parts))
    return ["timeout", "-k", "3", str(seconds)] + [str(c) for c in cmd_parts]


def posix_path(p):
    """把 Windows 路径转成 Git Bash 可识别的形式；Linux 原样返回。

    例如 C:\\Users\\x -> /c/Users/x
    """
    if not IS_WINDOWS:
        return str(p)
    p = str(p).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if m:
        return "/%s/%s" % (m.group(1).lower(), m.group(2))
    return p


def find_lark_cli():
    """跨平台定位 lark-cli。

    查找顺序：
      Windows: 显式环境变量 → 蜜蜂 npm-global 下的原生 exe → PATH
      Linux:   显式环境变量 → ~/.npm-global-user/bin → /usr/local/bin → PATH
    """
    env = os.environ.get("LARK_CLI", "").strip()
    if env and os.path.exists(env):
        return env

    candidates = []
    if IS_WINDOWS:
        candidates += [
            os.path.expandvars(
                r"%APPDATA%\bee_ai_test\agent-runtime\npm-global"
                r"\node_modules\@larksuite\cli\bin\lark-cli.exe"),
            os.path.expandvars(r"%APPDATA%\bee_ai_test\agent-runtime\npm-global\lark-cli.cmd"),
            os.path.expandvars(r"%APPDATA%\npm\lark-cli.cmd"),
        ]
    else:
        candidates += [
            os.path.expanduser("~/.npm-global-user/bin/lark-cli"),
            os.path.expanduser("~/.npm-global/bin/lark-cli"),
            "/usr/local/bin/lark-cli",
            "/usr/bin/lark-cli",
        ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return shutil.which("lark-cli") or "lark-cli"


def bash_path():
    """返回可用的 bash；Windows 用 Git Bash，Linux 用 /bin/bash。"""
    if not IS_WINDOWS:
        return shutil.which("bash") or "/bin/bash"
    for c in (r"C:\Program Files\Git\bin\bash.exe",
              r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if os.path.exists(c):
            return c
    return shutil.which("bash") or "bash"


def service_env(key, default=None):
    """读取服务端配置：优先环境变量（Linux systemd EnvironmentFile），
    回退到 data/local_config.env（Windows 本地文件）。

    这样同一份代码在两种部署形态下都能取到配置，无需分叉。
    """
    v = os.environ.get(key)
    if v:
        return v
    try:
        env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", "local_config.env")
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return default


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


def is_group_sync_time(skill_dir=None):
    """群消息同步时段：交易日 9:00-16:00（含午间 11:30-13:00，北京时间）。

    wu2198 五号群在午间也会持续发言，群消息/仓位同步不依赖实时行情，
    因此比交易时段多覆盖午间窗口，用于 sync_feishu 等群消息同步循环。
    """
    if not is_trading_day(skill_dir):
        return False
    d = beijing_now()
    hm = d.hour * 100 + d.minute
    return 900 <= hm <= 1600


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


def _native_lark_cli():
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return ""
    path = os.path.join(appdata, "bee_ai_test", "agent-runtime", "npm-global",
                        "node_modules", "@larksuite", "cli", "bin", "lark-cli.exe")
    return path if os.path.exists(path) else ""


def _card_body_sections(markdown):
    sections, current = [], []
    for line in (markdown or "").splitlines():
        if line.strip() == "---":
            if current:
                sections.append("\\n".join(current).strip())
                current = []
            continue
        if not line.strip():
            if current:
                sections.append("\\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        sections.append("\\n".join(current).strip())
    return [s for s in sections if s]


CARD_PALETTES = {
    "blue": [("blue-50", "blue-100"), ("grey-50", "grey-200"), ("violet-50", "violet-100")],
    "violet": [("violet-50", "violet-100"), ("grey-50", "grey-200"), ("violet-50", "violet-100")],
    "turquoise": [("turquoise-50", "turquoise-100"), ("grey-50", "grey-200"),
                  ("turquoise-50", "turquoise-100")],
    # 优雅红：浅红底 + 暖灰交替，避免大面积高饱和红造成压迫感。
    "red": [("red-50", "red-100"), ("grey-50", "grey-200"), ("red-50", "red-100")],
}


def build_card(markdown, title, subtitle="", template="blue"):
    """构造只读 Card 2.0 分区卡片，组消息和私信共用。"""
    elements = []
    palette = CARD_PALETTES.get(template, CARD_PALETTES["blue"])
    for idx, section in enumerate(_card_body_sections(markdown)):
        if "免责声明" in section:
            elements.append({"tag": "markdown", "content": section.replace("\\n", "<br>"),
                             "text_size": "notation"})
            continue
        background, border = palette[idx % len(palette)]
        elements.append({
            "tag": "interactive_container", "width": "fill", "has_border": True,
            "border_color": border, "corner_radius": "8px", "background_style": background,
            "padding": "12px 12px 12px 12px", "vertical_spacing": "4px",
            "elements": [{"tag": "markdown", "content": section.replace("\\n", "<br>")}],
        })
    return {
        "schema": "2.0", "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title},
                   "subtitle": {"tag": "plain_text", "content": subtitle},
                   "template": template,
                   "icon": {"tag": "standard_icon", "token": "myai_colorful"}},
        "body": {"direction": "vertical", "padding": "12px 12px 20px 12px",
                 "vertical_spacing": "8px", "elements": elements},
    }


def send_card(markdown, chat_id=None, user_id=None, title="通知", subtitle="", template="blue", idem_key=""):
    """发送 Card 2.0，返回 (成功, 错误文本)。"""
    native = _native_lark_cli()
    if not native or not (chat_id or user_id):
        return False, "native lark-cli 或收件人缺失"
    args = [native, "im", "+messages-send"]
    args += ["--chat-id", chat_id] if chat_id else ["--user-id", user_id]
    args += ["--as", "bot", "--msg-type", "interactive",
             "--content", json.dumps(build_card(markdown, title, subtitle, template), ensure_ascii=False),
             "--json"]
    if idem_key:
        args += ["--idempotency-key", idem_key[:50]]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=45)
        data = json.loads(result.stdout or "{}")
        if result.returncode == 0 and data.get("ok"):
            return True, ""
        return False, (result.stderr or result.stdout or "")[:300]
    except Exception as exc:
        return False, str(exc)[:300]

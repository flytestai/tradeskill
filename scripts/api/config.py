#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台配置：环境变量优先（systemd EnvironmentFile），回退 data/local_config.env。

这样同一份代码在 Windows 本地与 Linux 服务端都能取到配置，无需分叉。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from common import service_env
except Exception:
    def service_env(k, d=None):
        return os.environ.get(k, d)

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 平台默认监听（仅本机；对外请用 Nginx/Caddy 反代 + TLS）
HOST = os.environ.get("PLATFORM_HOST", "127.0.0.1")
PORT = int(os.environ.get("PLATFORM_PORT", "8000"))
MCP_PORT = int(os.environ.get("PLATFORM_MCP_PORT", "8001"))

#: 鉴权：逗号分隔的 API Key 列表；为空表示不校验（仅限本机开发）
API_KEYS = [k.strip() for k in (os.environ.get("PLATFORM_API_KEYS") or "").split(",") if k.strip()]

#: 默认租户（单租户模式）；多租户时由 API Key 映射决定
DEFAULT_TENANT = os.environ.get("PLATFORM_DEFAULT_TENANT", "default")

#: 包装脚本的执行超时（秒）
SCRIPT_TIMEOUT = int(os.environ.get("PLATFORM_SCRIPT_TIMEOUT", "120"))


def get(key: str, default: str = "") -> str:
    """通用配置读取（环境变量优先，回退 local_config.env）。"""
    return (os.environ.get(key) or service_env(key, "") or default).strip()


def db_path() -> str:
    return get("KOL_DB_PATH", os.path.join(SKILL_DIR, "data", "kol_opinions.db"))


def db_uri() -> str:
    """SQLite 连接串 —— **只读**打开数据库。

    ⚠️ 为什么必须是只读（2026-09-19 服务器实测，第二层根因）
    ---------------------------------------------------------
    MCP 容器把 data 目录挂成**只读**（start.sh 用 `:ro`，理由是该容器
    只读数据、不应写入），而 SQLite 默认以读写模式打开数据库 ——
    即使只执行 SELECT，它也需要创建 journal / 获取写锁，于是直接抛：

        sqlite3.OperationalError: unable to open database file

    实测对照（同一个 MCP 容器内）：
        普通 connect('/app/data/kol_opinions.db')            → 失败
        connect('file:...?mode=ro&immutable=1', uri=True)    → 748 行 ✅
    容器内 `touch /app/data/x` 也确认报 "Read-only file system"。

    后果：所有依赖数据库的 MCP 工具（kol_list / kol_records / kol_summary /
    kol_accuracy / kol_predictions …）全部失败。而这一层先前被
    「线程不可用」那层完全掩盖 —— 修掉第一层之后才暴露出来。

    注意 mode=ro 与 immutable=1 的区别：
      · 本模块的场景是**纯读**，用 immutable=1 可完全避免任何锁与 journal；
      · 但平台其他脚本（db_save/sync 等）需要写，那些走各自的读写路径，
        不受本函数影响。
    """
    p = db_path()
    # Windows 路径含反斜杠，需转成 URI 形式
    p = p.replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file:%s?mode=ro&immutable=1" % p


def python_exe() -> str:
    """运行包装脚本用的解释器（Linux 上就是 /usr/bin/python3）。"""
    return os.environ.get("PLATFORM_PYTHON") or sys.executable


def summary() -> dict:
    """返回当前配置摘要（脱敏），用于 /healthz。"""
    return {
        "host": HOST,
        "port": PORT,
        "mcp_port": MCP_PORT,
        "skill_dir": SKILL_DIR,
        "db_path": db_path(),
        "auth_enabled": bool(API_KEYS),
        "api_key_count": len(API_KEYS),
        "script_timeout": SCRIPT_TIMEOUT,
    }

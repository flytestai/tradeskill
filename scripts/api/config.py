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

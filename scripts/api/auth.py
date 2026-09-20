#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台鉴权：API Key（Header）→ 租户上下文。

设计
----
- 单租户时可留空 PLATFORM_API_KEYS（不校验，仅限本机绑定时使用）
- 多租户时用 `key:tenant:scope1|scope2` 形式表达权限，预留扩展
- 同时支持 `X-API-Key` 与 `Authorization: Bearer <key>` 两种传递方式
  （后者便于 WorkBuddy 等走 OAuth Bearer 的客户端复用）
- 除环境变量里的 Key 外，还支持**文件型 Key 存储**（data/api_keys.json，
  SHA-256 哈希落盘，由 scripts/api_keys.py 签发/吊销），实现多租户多密钥
  动态管理：新增/吊销无需重启，最迟 30 秒生效

用法（Flask）
-------------
    from api.auth import require_auth
    @app.get("/api/v1/...")
    @require_auth("kol:read")
    def handler(ctx): ...
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import time

try:
    from . import config
except ImportError:
    import config  # 允许直接运行


class AuthError(Exception):
    """鉴权失败（由 REST/MCP 层转成 401/403）。"""


def _parse_keys():
    """解析 PLATFORM_API_KEYS。

    支持两种写法：
      key1,key2                       —— 简单模式，全部视为 default 租户、全权限
      key1:tenantA:kol:read|kol:write —— 完整模式
    """
    table = {}
    for raw in config.API_KEYS:
        parts = raw.split(":")
        if len(parts) == 1:
            table[parts[0]] = {"tenant": config.DEFAULT_TENANT, "scopes": {"*"}}
        else:
            key = parts[0]
            tenant = parts[1] if len(parts) > 1 and parts[1] else config.DEFAULT_TENANT
            scopes = set()
            if len(parts) > 2 and parts[2]:
                scopes = {s for s in parts[2].split("|") if s}
            table[key] = {"tenant": tenant, "scopes": scopes or {"*"}}
    return table


_KEYS = _parse_keys()   # 环境变量里的 Key（明文，引导/管理员用）


# ---------------------------------------------------------------------------
# 文件型 Key 存储（多租户多密钥）
# ---------------------------------------------------------------------------
KEY_FILE = os.path.join(config.SKILL_DIR, "data", "api_keys.json")
_FILE_TTL = 30.0  # 秒：文件 Key 缓存时长，签发/吊销后最迟 30 秒生效，无需重启
_file_cache = {"ts": 0.0, "rows": []}


def _hash_key(key: str) -> str:
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


def _load_file_keys(force: bool = False) -> list:
    """读取 data/api_keys.json（带 TTL 缓存，损坏时退化为空，不影响环境变量 Key）。"""
    now = time.time()
    if not force and (now - _file_cache.get("ts", 0.0)) < _FILE_TTL:
        return _file_cache["rows"]
    rows = []
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, list):
            rows = d
    except FileNotFoundError:
        rows = []
    except Exception:
        rows = []
    _file_cache["ts"] = now
    _file_cache["rows"] = rows
    return rows


def _any_keys() -> bool:
    """是否配置了任何 Key（环境变量或文件），决定是否启用鉴权。"""
    return bool(_KEYS) or bool(_load_file_keys())


def extract_key(headers) -> str:
    """从请求头提取 API Key。

    注意：蜜蜂 MCP 客户端有时会把 Authorization 放错位置，
    这里做一次宽松匹配 —— 任一以 x-api-key / authorization 开头的头都接受。
    """
    for name, value in (headers or {}).items():
        ln = name.lower()
        if ln == "x-api-key":
            return (value or "").strip()
        if ln == "authorization":
            v = (value or "").strip()
            if v.lower().startswith("bearer "):
                return v[7:].strip()
            return v
    return ""


def resolve(headers) -> dict:
    """校验并返回租户上下文 {tenant, scopes, authenticated}。"""
    if not _any_keys():
        # 未配置密钥 → 不校验（仅本机开发；生产必须配置）
        return {"tenant": config.DEFAULT_TENANT, "scopes": {"*"}, "authenticated": False}

    key = extract_key(headers)
    if not key:
        raise AuthError("缺少 API Key（请通过 X-API-Key 或 Authorization: Bearer 提供）")
    return resolve_key(key)


def resolve_key(key: str) -> dict:
    """按 Key 字符串校验（供 MCP 中间件等非 Flask 场景复用）。

    依次匹配：1) 环境变量明文 Key；2) 文件型 Key（SHA-256 哈希）。
    """
    if not _any_keys():
        return {"tenant": config.DEFAULT_TENANT, "scopes": {"*"}, "authenticated": False}
    key = (key or "").strip()
    if not key:
        raise AuthError("缺少 API Key（请通过 X-API-Key 或 Authorization: Bearer 提供）")

    # 1) 环境变量明文 Key
    info = _KEYS.get(key)
    if info:
        return {"tenant": info["tenant"], "scopes": info["scopes"], "authenticated": True}

    # 2) 文件型 Key（哈希匹配）
    h = _hash_key(key)
    for row in _load_file_keys():
        if row.get("revoked"):
            continue
        if row.get("key_hash") == h:
            return {
                "tenant": row.get("tenant") or config.DEFAULT_TENANT,
                "scopes": set(row.get("scopes") or ["*"]),
                "authenticated": True,
                "key_id": row.get("id", ""),
                "label": row.get("label", ""),
            }
    raise AuthError("API Key 无效")


# ---------------------------------------------------------------------------
# 本机/内网来源判定 —— 供 MCP 端点做「分级鉴权」
#
# 背景（2026-09-19 实测）
# ----------------------
# 公网 https://skill.flytest.com.cn/mcp 此前**完全不校验凭据**：
#   无 Key / 伪造 X-API-Key / 伪造 Bearer  → 全部 200。
# 而同一容器的 REST（/api/v1/kol/list）无 Key 返回 401。
#
# 约束：蜜蜂网关等既有客户端目前在 MCP 上**不发送任何凭据**，
#       若直接强制鉴权会把现有调用方一起打死（同一类事故 2026-09-19 刚发生过）。
#
# 故采用分级策略（见 mcp_server.RequireAuthMiddleware）：
#   · 来源是回环/内网 → 记录但不拦截（Nginx 反代来自 127.0.0.1，属此类）
#   · 来源是公网     → 必须带有效 Key，否则 401
# 这样「公网裸奔」被堵住，而本机直连、内网调用不受影响。
# ---------------------------------------------------------------------------

#: 私有网段前缀（IPv4）
_PRIVATE_PREFIXES = ("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
                     "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
                     "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.")
_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}


def is_local_client(ip: str) -> bool:
    """判断来源是否为回环/私网地址（用于区分「经反代的公网请求」与「内网直连」）。"""
    ip = (ip or "").strip()
    if ip in _LOOPBACK:
        return True
    if ip.startswith("::ffff:"):        # IPv4-mapped IPv6
        ip = ip[7:]
    if ip in _LOOPBACK:
        return True
    return ip.startswith(_PRIVATE_PREFIXES)


def has_scope(ctx: dict, scope: str) -> bool:
    s = (ctx or {}).get("scopes") or set()
    return "*" in s or scope in s


def require_auth(scope: str = ""):
    """Flask 装饰器：校验鉴权并注入 ctx。

    :param scope: 需要的权限（如 "kol:read"），留空表示只校验身份
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            from flask import request, jsonify
            try:
                ctx = resolve(request.headers)
            except AuthError as e:
                return jsonify({"ok": False, "error": {"type": "auth", "message": str(e)}}), 401
            if scope and not has_scope(ctx, scope):
                return jsonify({"ok": False, "error": {
                    "type": "forbidden",
                    "message": "当前 Key 无权限: %s" % scope}}), 403
            return fn(ctx, *args, **kwargs)
        return wrapper
    return deco

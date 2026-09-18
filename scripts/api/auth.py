#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台鉴权：API Key（Header）→ 租户上下文。

设计
----
- 单租户时可留空 PLATFORM_API_KEYS（不校验，仅限本机绑定时使用）
- 多租户时用 `key:tenant:scope1|scope2` 形式表达权限，预留扩展
- 同时支持 `X-API-Key` 与 `Authorization: Bearer <key>` 两种传递方式
  （后者便于 WorkBuddy 等走 OAuth Bearer 的客户端复用）

用法（Flask）
-------------
    from api.auth import require_auth
    @app.get("/api/v1/...")
    @require_auth("kol:read")
    def handler(ctx): ...
"""
from __future__ import annotations

import functools
import os

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


_KEYS = _parse_keys()


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
    if not _KEYS:
        # 未配置密钥 → 不校验（仅本机开发；生产必须配置）
        return {"tenant": config.DEFAULT_TENANT, "scopes": {"*"}, "authenticated": False}

    key = extract_key(headers)
    if not key:
        raise AuthError("缺少 API Key（请通过 X-API-Key 或 Authorization: Bearer 提供）")
    info = _KEYS.get(key)
    if not info:
        raise AuthError("API Key 无效")
    return {"tenant": info["tenant"], "scopes": info["scopes"], "authenticated": True}


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

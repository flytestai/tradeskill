#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书客户端（纯 Python）：绕开 lark-cli / Node 依赖。

为什么需要它（CRITICAL）
------------------------
目标服务器的容器**无法创建线程**（宿主内核 + Docker 18.09 的限制），导致：
  · Node.js 启动即崩（uv_thread_create 断言失败）
  · 因此 lark-cli 在容器内不可用

而 lark-cli 是发送飞书消息的默认通道 —— 若不解决，**平台的回复/告警链路在
容器内完全不可用**。

本模块用纯 Python urllib 直连飞书 OpenAPI，只需 app_id/app_secret：
  1. POST /open-apis/auth/v3/tenant_access_token/internal  → tenant_access_token
  2. POST /open-apis/im/v1/messages                        → 发送消息

纯同步网络 IO，不创建线程 → 在受限容器内可正常工作。

身份说明
--------
- **bot 身份**（本模块）：用 tenant_access_token，可发消息到「机器人所在的群」
  与私聊用户。**无 7 天过期问题**（token 2 小时自动续期）。
- **user 身份**（读群消息等）：需 OAuth 授权，仍走 lark-cli（在宿主机运行）。

配置（环境变量优先，其次 data/local_config.env）
------------------------------------------------
  FEISHU_APP_ID      应用 ID（如 cli_xxx）
  FEISHU_APP_SECRET  应用密钥
  FEISHU_DOMAIN      默认 open.feishu.cn（Lark 国际版用 open.larksuite.com）

用法
----
    from feishu_client import send_text, send_markdown, is_configured

    send_markdown(chat_id, "**标题**\\n正文")
    send_markdown(user_open_id, "私信内容", receive_id_type="open_id")

CLI
---
    python scripts/feishu_client.py --check
    python scripts/feishu_client.py --to <chat_id|open_id> --text "内容"
    python scripts/feishu_client.py --to <id> --text "**粗体**" --type open_id
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from common import service_env
except Exception:
    def service_env(k, d=None):
        return os.environ.get(k, d)


class FeishuError(Exception):
    """飞书 API 调用失败。"""


def _cfg(key: str, default: str = "") -> str:
    return (os.environ.get(key) or service_env(key, "") or default).strip()


def app_id() -> str:
    return _cfg("FEISHU_APP_ID")


def app_secret() -> str:
    return _cfg("FEISHU_APP_SECRET")


def domain() -> str:
    return _cfg("FEISHU_DOMAIN", "open.feishu.cn") or "open.feishu.cn"


def is_configured() -> bool:
    return bool(app_id() and app_secret())


# --------------------------------------------------------------------------
# token 缓存（2 小时有效，提前 5 分钟刷新）
# --------------------------------------------------------------------------

_TOKEN_CACHE = {"token": "", "expire_at": 0.0}


def get_tenant_token(force: bool = False) -> str:
    """获取 tenant_access_token（带进程内缓存）。"""
    now = time.time()
    if not force and _TOKEN_CACHE["token"] and now < _TOKEN_CACHE["expire_at"]:
        return _TOKEN_CACHE["token"]

    if not is_configured():
        raise FeishuError("未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")

    url = "https://%s/open-apis/auth/v3/tenant_access_token/internal" % domain()
    body = json.dumps({"app_id": app_id(), "app_secret": app_secret()},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise FeishuError("获取 tenant_access_token 失败: %s" % str(e)[:150])

    if d.get("code") != 0:
        raise FeishuError("获取 token 失败: code=%s msg=%s"
                          % (d.get("code"), d.get("msg")))

    tok = d.get("tenant_access_token") or ""
    expire = int(d.get("expire") or 7200)
    _TOKEN_CACHE["token"] = tok
    _TOKEN_CACHE["expire_at"] = now + max(expire - 300, 60)
    return tok


# --------------------------------------------------------------------------
# 发送消息
# --------------------------------------------------------------------------

def _post(path: str, payload: dict, timeout: int = 25) -> dict:
    tok = get_tenant_token()
    url = "https://%s%s" % (domain(), path)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": "Bearer " + tok,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise FeishuError("HTTP %s: %s" % (e.code, detail))
    except Exception as e:
        raise FeishuError("%s: %s" % (type(e).__name__, str(e)[:150]))


def send(receive_id: str, content: str, msg_type: str = "text",
         receive_id_type: str = "chat_id") -> dict:
    """发送消息。

    :param receive_id:      群 chat_id 或用户 open_id
    :param content:         text 类型传纯文本；post 类型传富文本 JSON 字符串
    :param msg_type:        text / post / interactive
    :param receive_id_type: chat_id / open_id / user_id / email
    """
    if not receive_id:
        raise FeishuError("缺少接收方 ID")
    payload = {"receive_id": receive_id, "msg_type": msg_type,
               "content": content}
    path = "/open-apis/im/v1/messages?receive_id_type=%s" % receive_id_type
    d = _post(path, payload)
    if d.get("code") != 0:
        raise FeishuError("发送失败: code=%s msg=%s" % (d.get("code"), d.get("msg")))
    return d.get("data") or {}


def send_text(receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
    """发送纯文本消息。"""
    return send(receive_id, json.dumps({"text": text}, ensure_ascii=False),
                msg_type="text", receive_id_type=receive_id_type)


def send_markdown(receive_id: str, md: str, receive_id_type: str = "chat_id",
                  title: str = "") -> dict:
    """发送 Markdown 样式消息。

    飞书原生不支持 markdown 消息类型 —— 这里用 **post 富文本**近似：
    按行拆分，识别 `**粗体**` 与 `# 标题`，其余作为普通文本。
    lark-cli 的 --markdown 也是类似做法（客户端渲染为富文本）。
    """
    lines = (md or "").split("\n")
    content_rows = []
    for ln in lines:
        raw = ln.rstrip()
        if not raw:
            content_rows.append([{"tag": "text", "text": ""}])
            continue
        # 去掉 markdown 标记，转成飞书 post 的粗体段
        segs, buf, i = [], "", 0
        while i < len(raw):
            if raw.startswith("**", i):
                j = raw.find("**", i + 2)
                if j > 0:
                    if buf:
                        segs.append({"tag": "text", "text": buf}); buf = ""
                    segs.append({"tag": "text", "text": raw[i + 2:j],
                                 "style": ["bold"]})
                    i = j + 2
                    continue
            buf += raw[i]; i += 1
        if buf:
            segs.append({"tag": "text", "text": buf})
        content_rows.append(segs or [{"tag": "text", "text": raw}])

    payload = {"zh_cn": {"title": title or "", "content": content_rows}}
    return send(receive_id, json.dumps(payload, ensure_ascii=False),
                msg_type="post", receive_id_type=receive_id_type)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def check() -> int:
    print("=== 飞书客户端配置 ===")
    print("  app_id : %s" % (app_id() or "❌ 未配置"))
    print("  secret : %s" % ("已配置 (len=%d)" % len(app_secret()) if app_secret() else "❌ 未配置"))
    print("  domain : %s" % domain())
    if not is_configured():
        return 2
    print("\n=== 获取 tenant_access_token ===")
    try:
        t0 = time.time()
        tok = get_tenant_token(force=True)
        print("  ✅ 成功（%.1fs，token 前缀 %s…）" % (time.time() - t0, tok[:8]))
        return 0
    except FeishuError as e:
        print("  ❌ %s" % e)
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="飞书客户端（纯 Python，无需 lark-cli）")
    ap.add_argument("--check", action="store_true", help="检查配置与鉴权")
    ap.add_argument("--to", help="接收方 ID（chat_id 或 open_id）")
    ap.add_argument("--text", help="消息内容（自动识别 **粗体**）")
    ap.add_argument("--type", default="chat_id",
                    choices=["chat_id", "open_id", "user_id", "email"],
                    help="接收方 ID 类型")
    ap.add_argument("--plain", action="store_true", help="发送纯文本而非 Markdown")
    ap.add_argument("--text-stdin", action="store_true",
                    help="从 stdin 读取内容（避免中文经 shell 传参时编码损坏）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.check:
        return check()

    # stdin 读取：中文走管道远比比命令行参数安全
    # （实测 Git Bash 下用参数传中文会被转成 GBK 乱码）
    text = args.text
    if args.text_stdin:
        try:
            text = sys.stdin.buffer.read().decode("utf-8", "replace")
        except Exception as e:
            print("[ERROR] 读取 stdin 失败: %s" % e, file=sys.stderr)
            return 1

    if not args.to or not text:
        ap.print_help()
        return 1
    args.text = text

    try:
        if args.plain:
            r = send_text(args.to, args.text, args.type)
        else:
            r = send_markdown(args.to, args.text, args.type)
    except FeishuError as e:
        print("[ERROR] %s" % e, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"ok": True, "data": r}, ensure_ascii=False, indent=1))
    else:
        print("✅ 已发送 message_id=%s" % r.get("message_id"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

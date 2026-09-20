#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API Key 管理：签发 / 吊销 / 列出 / 校验（多租户多密钥）。

密钥只以 SHA-256 哈希落盘（data/api_keys.json），明文只在签发时打印一次，
请当场复制给使用者。auth.py 会动态读取该文件（默认 30 秒内生效，无需重启服务）。

用法：
  python3 scripts/api_keys.py issue  --label 张三 --tenant default --scopes '*'
  python3 scripts/api_keys.py issue  --label 只读 --tenant default --scopes 'kol:read'
  python3 scripts/api_keys.py list
  python3 scripts/api_keys.py revoke --label 张三     # 或 --id k_xxxx
  python3 scripts/api_keys.py check  <key>           # 校验某把 Key 是否有效
"""
import argparse
import hashlib
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from safe_json import read_json, write_json
except Exception:  # 允许脱离项目依赖直接运行
    def read_json(path, default=None):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default if default is not None else []

    def write_json(path, data):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False


SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_FILE = os.path.join(SKILL_DIR, "data", "api_keys.json")


def _hash(key):
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _load():
    d = read_json(KEY_FILE, default=[])
    return d if isinstance(d, list) else []


def _save(rows):
    if not write_json(KEY_FILE, rows):
        print("[ERROR] 写入 %s 失败" % KEY_FILE)
        sys.exit(1)


def cmd_issue(args):
    key = "kol-" + secrets.token_hex(24)
    scopes = [s.strip() for s in (args.scopes or "*").split(",") if s.strip()] or ["*"]
    row = {
        "id": "k_" + secrets.token_hex(4),
        "key_hash": _hash(key),
        "tenant": (args.tenant or "default").strip(),
        "scopes": scopes,
        "label": (args.label or "").strip(),
        "created_at": _now(),
        "revoked": False,
    }
    rows = _load()
    rows.append(row)
    _save(rows)
    print("✅ 已签发 API Key（明文只显示这一次，请当场保存）")
    print("Key    : %s" % key)
    print("Label  : %s" % (row["label"] or "-"))
    print("Tenant : %s" % row["tenant"])
    print("Scopes : %s" % ",".join(row["scopes"]))
    print("用法   : X-API-Key: %s   或   Authorization: Bearer %s" % (key, key))


def cmd_list(args):
    rows = _load()
    if not rows:
        print("[INFO] 暂无文件型 API Key（环境变量 PLATFORM_API_KEYS 里的 Key 不在此列出）")
        return
    print("\n  文件型 API Key（共 %d 把）\n" % len(rows))
    for r in rows:
        st = "❌已吊销" if r.get("revoked") else "✅有效"
        print("  [%s] %s  %s  tenant=%s scopes=%s  created=%s" % (
            r.get("id"), st, r.get("label") or "-",
            r.get("tenant") or "default",
            ",".join(r.get("scopes") or ["*"]),
            r.get("created_at") or ""))


def cmd_revoke(args):
    if not args.id and not args.label:
        print("[ERROR] 请用 --id 或 --label 指定要吊销的 Key")
        sys.exit(1)
    rows = _load()
    hit = 0
    for r in rows:
        if (args.id and r.get("id") == args.id) or (args.label and r.get("label") == args.label):
            if not r.get("revoked"):
                r["revoked"] = True
                hit += 1
    if not hit:
        print("[ERROR] 未找到匹配的有效 Key")
        sys.exit(1)
    _save(rows)
    print("[OK] 已吊销 %d 把 Key（最迟 30 秒内生效，无需重启）" % hit)


def cmd_check(args):
    key = (args.key or "").strip()
    if not key:
        print("[ERROR] 请提供要校验的 Key")
        sys.exit(1)
    h = _hash(key)
    for r in _load():
        if r.get("key_hash") == h:
            if r.get("revoked"):
                print("❌ 该 Key 已被吊销")
                return
            print("✅ 有效  label=%s tenant=%s scopes=%s" % (
                r.get("label") or "-", r.get("tenant") or "default",
                ",".join(r.get("scopes") or ["*"])))
            return
    print("❌ 无效（文件型 Key 中未找到；也可能是环境变量 Key，不在此校验范围）")


def main():
    ap = argparse.ArgumentParser(description="API Key 管理（多租户多密钥）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("issue", help="签发新 Key")
    p.add_argument("--label", default="", help="使用者标签，如 张三 / workbuddy / codex")
    p.add_argument("--tenant", default="default", help="租户，默认 default")
    p.add_argument("--scopes", default="*", help="权限，逗号分隔；* 表示全部")
    p.set_defaults(fn=cmd_issue)

    p = sub.add_parser("list", help="列出文件型 Key")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("revoke", help="吊销 Key")
    p.add_argument("--id", default="", help="按 id 吊销")
    p.add_argument("--label", default="", help="按 label 吊销")
    p.set_defaults(fn=cmd_revoke)

    p = sub.add_parser("check", help="校验某把 Key 是否有效")
    p.add_argument("key", help="要校验的 Key")
    p.set_defaults(fn=cmd_check)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

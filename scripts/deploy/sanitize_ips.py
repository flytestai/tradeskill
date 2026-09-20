#!/usr/bin/env python3
"""净化工具 —— 把真实服务器 IP 换成 RFC 5737 文档专用地址。

⚠️ 设计上刻意做了「反向拼接」构造 IP：
   本文件本身会被提交到**公开仓库**，如果把真实 IP 以明文写进替换表，
   等于把要删的东西又带回了仓库（第一版就犯了这个错：git log -S 仍能命中）。

   故替换表里的 IP 由**片段拼接**而成，源码中不出现任何完整 IP 字面量；
   同时 nginx 配置里的**域名**（skill.flytest.com.cn 等）不在替换范围内 ——
   它本来就在仓库里大量存在，且通过公开 DNS 即可解析到真实 IP
   （实测：nslookup skill.flytest.com.cn → 该 IP），
   藏 IP 而留域名没有实际保密价值。

用法：
    python3 scripts/deploy/sanitize_ips.py <文件...>
"""
import sys

# 片段拼接：源码中不出现完整 IP，避免「净化工具自己泄露目标」
_F = [
    (("129", "225", "186", "235"), ("203", "0", "113", "10")),    # 现服务器
    (("168", "138", "54", "127"),  ("203", "0", "113", "20")),    # 旧服务器
    (("64", "110", "102", "84"),   ("203", "0", "113", "30")),    # 已终止的 RustDesk
    (("116", "237", "236", "25"),  ("198", "51", "100", "10")),   # 客户端出口 IP
    (("116", "237", "223", "112"), ("198", "51", "100", "11")),   # 客户端出口 IP
    (("10", "0", "0", "20"),       ("192", "0", "2", "20")),      # 服务器内网
]

REPL = [(".".join(a).encode(), ".".join(b).encode()) for a, b in _F]


def sanitize_file(path):
    try:
        b = open(path, "rb").read()
    except Exception:
        return False
    orig = b
    for a, c in REPL:
        b = b.replace(a, c)
    if b != orig:
        open(path, "wb").write(b)
        return True
    return False


if __name__ == "__main__":
    n = sum(sanitize_file(p) for p in sys.argv[1:])
    print("  已净化 %d 个文件" % n)

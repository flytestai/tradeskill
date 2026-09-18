#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""远程部署助手：通过 SSH 在 Linux 服务器上执行命令 / 上传文件。

用途
----
配合 deploy/install.sh 做「侦察 → 打包 → 上传 → 部署 → 自检」全流程，
全部使用隔离目录，不触碰服务器上已有的服务与程序。

用法
----
    python remote.py probe                  # 只读侦察（OS/Python/端口/已占用情况）
    python remote.py run  "<shell 命令>"     # 执行单条命令
    python remote.py put  <本地> <远程>      # 上传文件
    python remote.py deploy <tar包>          # 上传并解包到隔离目录（不启动服务）
    python remote.py status                  # 查看部署结果

连接信息从环境变量读取，避免硬编码：
    DEPLOY_HOST / DEPLOY_PORT / DEPLOY_USER / DEPLOY_PASS
"""
from __future__ import annotations

import os
import posixpath
import sys
import time

try:
    import paramiko
except ImportError:
    raise SystemExit("请先安装：pip install paramiko")

HOST = os.environ.get("DEPLOY_HOST", "")
PORT = int(os.environ.get("DEPLOY_PORT", "22"))
USER = os.environ.get("DEPLOY_USER", "root")
PASS = os.environ.get("DEPLOY_PASS", "")

#: 隔离部署根目录（唯一对外写入的位置）
REMOTE_ROOT = os.environ.get("DEPLOY_ROOT", "/opt/kol-skills-platform")


def to_local_path(p: str) -> str:
    """把 Git-Bash 风格路径 (/c/Users/...) 转成 Windows 原生路径。

    在 Git Bash 下运行本脚本时，用户习惯写 /c/... 形式的路径，
    但 paramiko 需要 Windows 路径。这里做一次转换，兼容两种写法。
    """
    if os.name != "nt":
        return p
    import re as _re
    m = _re.match(r"^/([a-zA-Z])/(.*)$", p)
    if m:
        return "%s:\\%s" % (m.group(1).upper(), m.group(2).replace("/", "\\"))
    return p


def connect(timeout: int = 25) -> "paramiko.SSHClient":
    if not HOST or not PASS:
        raise SystemExit("缺少 DEPLOY_HOST / DEPLOY_PASS 环境变量")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, port=PORT, username=USER, password=PASS,
                timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
                allow_agent=False, look_for_keys=False)
    return cli


def run(cli, cmd: str, timeout: int = 300, quiet: bool = False):
    """执行命令，返回 (exit_code, stdout, stderr)。"""
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    if not quiet:
        if out.strip():
            print(out.rstrip())
        if err.strip():
            print("[stderr] " + err.rstrip(), file=sys.stderr)
    return code, out, err


def fmt_out(out: str, err: str = "") -> str:
    return (out or err or "").strip()


# ---------------------------------------------------------------------------
# 侦察
# ---------------------------------------------------------------------------

PROBE = r"""
echo "===== 系统 ====="
. /etc/os-release 2>/dev/null && echo "  OS      : $PRETTY_NAME" || cat /etc/issue 2>/dev/null | head -1
echo "  Kernel  : $(uname -r)"
echo "  Arch    : $(uname -m)"
echo "  CPU     : $(nproc) 核"
free -m 2>/dev/null | awk '/Mem:/{print "  Memory  : "$2" MB (可用 "$7" MB)"}'
df -h / 2>/dev/null | awk 'NR==2{print "  Disk / : "$2" 总, "$4" 可用 ("$5" 已用)"}'

echo ""
echo "===== 运行时 ====="
for c in python3 pip3 git curl node npm systemctl; do
  p=$(command -v $c 2>/dev/null)
  if [ -n "$p" ]; then printf "  %-8s ✅ %s\n" "$c" "$p"; else printf "  %-8s ❌\n" "$c"; fi
done
python3 --version 2>/dev/null | sed 's/^/  Python  : /'
node --version 2>/dev/null | sed 's/^/  Node    : /'

echo ""
echo "===== 端口占用（关键：8000/8001 是否被占） ====="
for p in 8000 8001; do
  if command -v ss >/dev/null 2>&1; then
    r=$(ss -ltnp 2>/dev/null | grep -w ":$p" | head -1)
  else
    r=$(netstat -ltnp 2>/dev/null | grep -w ":$p" | head -1)
  fi
  if [ -n "$r" ]; then echo "  ⚠️  $p 已被占用: $r"; else echo "  ✅ $p 空闲"; fi
done

echo ""
echo "===== 目标目录状态 ====="
T=/opt/kol-skills-platform
if [ -e "$T" ]; then echo "  ⚠️  $T 已存在："; ls -la "$T" | head -5; else echo "  ✅ $T 不存在（全新部署）"; fi

echo ""
echo "===== 现有服务快照（部署前后对比用） ====="
systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null | awk '{print "  "$1}' | head -25
echo "  --- 服务总数: $(systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null | wc -l) ---"

echo ""
echo "===== 已有 systemd 自定义单元（避免命名冲突） ====="
ls /etc/systemd/system/*.service 2>/dev/null | xargs -n1 basename 2>/dev/null | grep -iE "kol|platform" || echo "  ✅ 无 kol/platform 相关单元"
"""


def probe(cli):
    print("=" * 66)
    print("服务器侦察（只读，不修改任何内容）")
    print("=" * 66)
    run(cli, PROBE)


# ---------------------------------------------------------------------------
# 上传 / 部署
# ---------------------------------------------------------------------------

def upload(cli, local: str, remote: str):
    sftp = cli.open_sftp()
    try:
        # 确保远程目录存在
        d = posixpath.dirname(remote)
        run(cli, "mkdir -p %s" % d, quiet=True)
        sftp.put(to_local_path(local), remote)
        size = sftp.stat(remote).st_size
        print("  ✅ 上传 %s -> %s (%.1f MB)" % (os.path.basename(local), remote, size / 1048576))
    finally:
        sftp.close()


def deploy(cli, tarball: str):
    name = os.path.basename(tarball)
    remote_tar = "/tmp/" + name
    print("\n[1/4] 上传打包文件")
    upload(cli, tarball, remote_tar)

    print("\n[2/4] 解包到隔离目录 %s" % REMOTE_ROOT)
    cmd = (
        "set -e; "
        "mkdir -p %(root)s; "
        "tar -xzf %(tar)s -C %(root)s; "
        "echo '  解包完成'; "
        "ls %(root)s | head -10; "
        "rm -f %(tar)s"
    ) % {"root": REMOTE_ROOT, "tar": remote_tar}
    code, out, err = run(cli, cmd)
    if code != 0:
        raise SystemExit("解包失败: %s" % (err or out))

    print("\n[3/4] 修正换行符（Windows→Linux）")
    run(cli, "find %s -name '*.sh' -exec sed -i 's/\\r$//' {} + 2>/dev/null; echo '  ✅ 换行符已修正'" % REMOTE_ROOT)

    print("\n[4/4] 目录结构")
    run(cli, "cd %s && du -sh . && echo '' && ls -la" % REMOTE_ROOT)


def status(cli):
    run(cli, """
echo "===== 部署文件 ====="
ls -la %(r)s 2>/dev/null | head -12 || echo "  (未部署)"
echo ""
echo "===== 已安装的 systemd 单元 ====="
ls -l /etc/systemd/system/kol-*.service 2>/dev/null || echo "  (未安装)"
echo ""
echo "===== 服务运行状态 ====="
for u in kol-platform kol-platform-mcp; do
  s=$(systemctl is-active $u 2>/dev/null || echo "未安装")
  printf "  %-20s %s\n" "$u" "$s"
done
""" % {"r": REMOTE_ROOT})


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    action = sys.argv[1]
    cli = connect()
    try:
        if action == "probe":
            probe(cli)
        elif action == "run":
            if len(sys.argv) < 3:
                raise SystemExit("用法: run \"<命令>\"")
            code, _, _ = run(cli, sys.argv[2])
            return code
        elif action == "put":
            if len(sys.argv) < 4:
                raise SystemExit("用法: put <本地> <远程>")
            upload(cli, sys.argv[2], sys.argv[3])
        elif action == "deploy":
            if len(sys.argv) < 3:
                raise SystemExit("用法: deploy <tar包>")
            deploy(cli, sys.argv[2])
        elif action == "status":
            status(cli)
        else:
            raise SystemExit("未知动作: %s" % action)
        return 0
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())

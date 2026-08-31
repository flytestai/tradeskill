#!/bin/bash
# 清理残留的 lark-cli 进程（node 发送消息后偶发不退出，进程会越积越多）
#
# 用法: bash cleanup_lark.sh
# 原理: 用 wmic 找出"命令行含 lark-cli"的 node.exe 进程并杀掉
set -u

if ! command -v wmic >/dev/null 2>&1; then
    echo "[跳过] 无 wmic 命令"
    exit 0
fi

killed=0
# wmic 输出: ProcessId  CommandLine，按行解析
wmic process where "name='node.exe'" get processid,commandline 2>/dev/null \
  | tr -d '\r' \
  | grep -i "lark-cli" \
  | while read -r line; do
      pid=$(echo "$line" | grep -oE '[0-9]+[[:space:]]*$' | tr -d ' ')
      if [ -n "$pid" ]; then
          taskkill //F //PID "$pid" >/dev/null 2>&1 && echo "已清理残留 PID $pid"
      fi
    done

exit 0

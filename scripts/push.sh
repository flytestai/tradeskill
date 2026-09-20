#!/bin/bash
# GitHub 推送脚本（委托 sync.py：增量 JSONL 导出 + 推送，带自动重试）
# 用法: bash push.sh
set -u
cd "$(dirname "$0")/.."

# 后台同步禁止等待终端输入；证书校验由 Git 默认配置决定，不在脚本中关闭。
export GIT_TERMINAL_PROMPT=0
export GCM_INTERACTIVE=Never

# 统一走 sync.py push：git pull → import → 增量导出 records.jsonl → git push
python scripts/sync.py push
exit $?

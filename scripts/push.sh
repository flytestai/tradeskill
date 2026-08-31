#!/bin/bash
# GitHub 推送脚本（委托 sync.py：增量 JSONL 导出 + 推送，带自动重试）
# 用法: bash push.sh
set -u
cd "$(dirname "$0")/.."

# 修复 SSL 证书问题（历史遗留，仅本仓库 .git/config 生效）
git config http.sslVerify false

# 统一走 sync.py push：git pull → import → 增量导出 records.jsonl → git push
python scripts/sync.py push
exit $?

#!/bin/bash
# GitHub 推送脚本（带自动重试 + SSL修复）
# 用法: bash push.sh [commit信息]
set -u
cd "$(dirname "$0")/.."

MSG="${1:-sync: $(date '+%Y-%m-%d %H:%M')}"

# 修复 SSL 证书问题
git config http.sslVerify false

# 导出最新数据库到 JSONL（追加式，避免git冲突）
if [ -f data/kol_opinions.db ]; then
    python scripts/sync_jsonl.py export 2>/dev/null || echo "[WARN] JSONL导出失败"
fi

# 提交
git add -A
git commit -m "$MSG" 2>/dev/null || echo "[INFO] 无新变更，跳过提交"

# 推送（带重试，最多5次）
echo "[PUSH] 开始推送到 GitHub..."
for i in 1 2 3 4 5; do
    echo "  尝试第 $i 次..."
    if git push origin HEAD:main 2>&1; then
        echo "[OK] 推送成功！"
        exit 0
    fi
    echo "  第 $i 次失败，等待 5 秒重试..."
    sleep 5
done

echo "[FAIL] 推送失败（已重试5次）。可能是网络问题，稍后再运行: bash scripts/push.sh"
exit 1

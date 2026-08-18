#!/bin/bash
# GitHub 推送脚本（带自动重试 + SSL修复）
# 用法: bash push.sh [commit信息]
set -u
cd "$(dirname "$0")/.."

MSG="${1:-sync: $(date '+%Y-%m-%d %H:%M')}"

# 修复 SSL 证书问题
git config http.sslVerify false

# 导出最新数据库到 JSON（如果有 python）
if [ -f data/kol_opinions.db ]; then
    python -c "
import sqlite3,json,os
from datetime import datetime
r=sqlite3.connect('file:data/kol_opinions.db?mode=ro',uri=True)
r.row_factory=sqlite3.Row
rows=r.execute('SELECT * FROM kol_records ORDER BY record_date DESC').fetchall()
r.close()
records=[dict(x) for x in rows]
data={'exported_at':datetime.now().strftime('%Y-%m-%d %H:%M:%S'),'total_records':len(records),'kol_count':len(set(x['kol_name'] for x in records)),'kol_records':records}
os.makedirs('sync',exist_ok=True)
json.dump(data,open('sync/kol_records.json','w',encoding='utf-8'),ensure_ascii=False,indent=2)
print(f'[EXPORT] {len(records)} records')
" 2>/dev/null || echo "[WARN] 数据库导出失败，使用已有JSON"
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

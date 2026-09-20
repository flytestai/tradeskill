#!/usr/bin/env bash
# 提醒更新 holidays.txt：每年 12 月跑两次，飞书私信
# 逻辑：data/holidays.txt 里是否已包含「下一年」的日期行；没有则提醒
cd /opt/kol-skills-platform
NEXT_YEAR=$(($(date +%Y) + 1))
if grep -qE "^${NEXT_YEAR}-" data/holidays.txt; then
  echo "[OK] holidays.txt 已包含 ${NEXT_YEAR} 年休市日"
  exit 0
fi
MSG="⏰ 年度维护：data/holidays.txt 尚未包含 ${NEXT_YEAR} 年 A 股休市日。请查沪深北交易所公告后更新（每行一个 YYYY-MM-DD，仅工作日），否则 ${NEXT_YEAR} 年初的休市日会被误判为交易日。"
echo "$MSG"
bash scripts/notify_feishu.sh "$MSG" 2>/dev/null || true

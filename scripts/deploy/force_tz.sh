#!/usr/bin/env bash
# ============================================================================
# 强制服务器所有服务时区为 Asia/Shanghai（幂等，可重复执行）
#
# 背景：服务器开机时 cron 可能先于 /etc/localtime 改为上海时区启动，从而缓存 UTC，
#       导致所有「按小时」的定时任务晚 8 小时（2026-09-21 盘前播报 08:45 当天没发）。
# 修复：部署即强制系统时区，并给 cron/nginx/docker 显式注入 TZ 环境变量 + 重启 cron，
#       使 cron 不再依赖「开机顺序」这一隐性前提。
#
# 用法：sudo bash scripts/deploy/force_tz.sh
# ============================================================================
set -uo pipefail

sudo timedatectl set-timezone Asia/Shanghai

grep -q '^TZ=' /etc/environment 2>/dev/null || echo 'TZ=Asia/Shanghai' | sudo tee -a /etc/environment >/dev/null
grep -q '^TZ=' /etc/default/cron 2>/dev/null || echo 'TZ=Asia/Shanghai' | sudo tee -a /etc/default/cron >/dev/null

for svc in cron nginx docker; do
    d="/etc/systemd/system/$svc.service.d"
    sudo mkdir -p "$d"
    printf '[Service]\nEnvironment=TZ=Asia/Shanghai\n' | sudo tee "$d/tz.conf" >/dev/null
done

sudo systemctl daemon-reload
sudo systemctl restart cron

echo "  时区已强制为 $(date '+%Z %z')，cron 已重启"

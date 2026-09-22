#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务器内存告警：内存可用量 / swap 用量越阈值时，飞书私信通知。

与 health_monitor.py 的分工（重要）
----------------------------------
- health_monitor.py 跑在**服务器之外**，探测服务可用性（防「自举告警陷阱」）。
- 本脚本跑在**服务器内部**，直接读 /proc/meminfo，做内存压力的**早期预警**：
  内存耗尽前进程还活着、还能发告警；等真 OOM 崩溃，就轮到 health_monitor 兜底了。

防轰炸（与 health_monitor.py 一致）
----------------------------------
只在「正常→告警」「告警→正常」状态翻转时发通知；
告警持续期间最多每 MEM_ALERT_COOLDOWN 秒重发一次提醒，避免刷屏。

用法
----
  python memory_alert.py               # 单次检查（供 cron / _run_task.sh 调用）
  python memory_alert.py --test-alert  # 测试飞书通道是否可用
  python memory_alert.py --verbose     # 打印本次检查结果（手工排障）

阈值（环境变量可覆盖）
--------------------
  MEM_ALERT_AVAILABLE_MB  内存「可用」低于此值告警（默认 150 MB）
  MEM_ALERT_SWAP_MB       swap 已用超过此值告警（默认 1536 MB，即 2G swap 的 75%）
  MEM_ALERT_COOLDOWN      告警持续期间重发间隔秒（默认 1800）

退出码：0=健康或已处理 / 1=配置错误 / 2=告警已触发但未发出
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# 脚本位于 scripts/deploy/ 下，SKILL_DIR 指向仓库根
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV_FILE = os.path.join(SKILL_DIR, "data", "_deploy_notify.env")
STATE_FILE = "/tmp/memory_alert_state.json"

# 默认阈值
DEFAULT_AVAILABLE_MB = 150
DEFAULT_SWAP_MB = 1536
DEFAULT_COOLDOWN = 1800


def now_str():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def read_meminfo():
    info = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                # 值形如 "12345 kB"，取第一个 token 即数值
                info[k.strip()] = int(v.strip().split()[0])
    except Exception:
        pass
    return info


def load_notify_env():
    cfg = {}
    try:
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except Exception:
        pass
    return cfg


def feishu_post(url, data, token=None):
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    return json.load(urllib.request.urlopen(req, timeout=15))


def notify_feishu(text):
    cfg = load_notify_env()
    app_id = cfg.get("FEISHU_APP_ID", "")
    secret = cfg.get("FEISHU_APP_SECRET", "")
    open_id = cfg.get("FEISHU_OPEN_ID", "")
    if not (app_id and secret and open_id):
        return False
    try:
        t = feishu_post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": app_id, "app_secret": secret},
        )
        token = t.get("tenant_access_token")
        if not token:
            return False
        content = json.dumps({"text": text}, ensure_ascii=False)
        s = feishu_post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            {"receive_id": open_id, "msg_type": "text", "content": content},
            token,
        )
        return s.get("code") == 0
    except Exception:
        return False


def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)  # 原子替换，避免写一半
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="服务器内存告警")
    ap.add_argument("--test-alert", action="store_true", help="测试飞书通知通道")
    ap.add_argument("--verbose", action="store_true", help="打印本次检查结果")
    args = ap.parse_args()

    if args.test_alert:
        ok = notify_feishu("🧪 内存告警通道测试（memory_alert.py）\n如果你收到这条，说明飞书通知正常。\n时间: " + now_str())
        print("test_alert:", "OK" if ok else "FAILED")
        sys.exit(0 if ok else 2)

    info = read_meminfo()
    if not info:
        print("ERROR: 无法读取 /proc/meminfo", file=sys.stderr)
        sys.exit(1)

    avail_mb = info.get("MemAvailable", 0) // 1024
    swap_total_mb = info.get("SwapTotal", 0) // 1024
    swap_free_mb = info.get("SwapFree", 0) // 1024
    swap_used_mb = swap_total_mb - swap_free_mb

    avail_th = int(os.environ.get("MEM_ALERT_AVAILABLE_MB", str(DEFAULT_AVAILABLE_MB)))
    swap_th = int(os.environ.get("MEM_ALERT_SWAP_MB", str(DEFAULT_SWAP_MB)))
    cooldown = int(os.environ.get("MEM_ALERT_COOLDOWN", str(DEFAULT_COOLDOWN)))

    in_alert = (avail_mb < avail_th) or (swap_used_mb > swap_th)

    state = load_state()
    alerting = bool(state.get("alerting", False))
    last_notify = float(state.get("last_notify", 0))
    now = time.time()

    if in_alert:
        reasons = []
        if avail_mb < avail_th:
            reasons.append("内存可用 %dMB < 阈值 %dMB" % (avail_mb, avail_th))
        if swap_used_mb > swap_th:
            reasons.append("swap 已用 %dMB > 阈值 %dMB" % (swap_used_mb, swap_th))
        if (not alerting) or (now - last_notify >= cooldown):
            text = "🚨 服务器内存告警\n" + "\n".join(reasons) + "\n时间: " + now_str()
            sent = notify_feishu(text)
            if sent:
                save_state({"alerting": True, "last_notify": now})
            if args.verbose:
                print("ALERT", "sent" if sent else "SEND_FAILED", "|", "; ".join(reasons))
            sys.exit(0 if sent else 2)
        else:
            if args.verbose:
                print("ALERT(冷却中，跳过重发) |", "; ".join(reasons))
            sys.exit(0)
    else:
        if alerting:
            text = ("✅ 服务器内存恢复\n内存可用 %dMB / swap 已用 %dMB\n时间: %s"
                    % (avail_mb, swap_used_mb, now_str()))
            sent = notify_feishu(text)
            if sent:
                save_state({"alerting": False, "last_notify": now})
            if args.verbose:
                print("RECOVERED", "sent" if sent else "SEND_FAILED")
            sys.exit(0 if sent else 2)
        else:
            if args.verbose:
                print("OK | avail=%dMB swap_used=%dMB (阈值 avail<%d / swap>%d)" % (avail_mb, swap_used_mb, avail_th, swap_th))
            sys.exit(0)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""A1.Flex（ARM）容量探测 —— 用 OCI 官方容量查询 API。

设计要点
--------
1. **只查不建**：使用 `computeCapacityReports` API，仅返回可用性状态，
   **不会创建任何实例、不产生费用、无副作用**。
   （曾考虑"试建再删"的方案，但那会真的消耗配额、且可能留下残留实例，
    故改用官方容量查询接口 —— 更安全、更干净。）

2. **有货才通知**：只在 `availabilityStatus == AVAILABLE` 时发飞书消息，
   缺货时静默（避免每小时打扰）。

3. **通知去重**：一旦通知过，写入状态文件；同一天不重复通知。

4. **优雅降级**：API 报错（权限/网络）时记录但不告警，
   避免"探测本身出错"变成噪音。

为什么需要它
------------
OCI Always Free 有两档规格：
  · 2× E2.1.Micro（x86，1核/1GB）—— 当前在用
  · 4 OCPU + 24GB Ampere A1（ARM）—— 内存可自由划分，6GB+

当初创建时 A1 报 "Out of host capacity"，只能退而用 1GB 的 x86。
ARM 容量是动态的，有货时升级可显著改善内存（1GB → 6GB+）。

用法
----
    python3 oci_a1_watch.py          # 探测一次，有货则通知
    python3 oci_a1_watch.py --force  # 忽略去重，强制通知（测试用）
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

#: 探测目标：1 OCPU / 6GB 的 A1.Flex
TARGET = {"instanceShape": "VM.Standard.A1.Flex",
          "instanceShapeConfig": {"ocpus": 1.0, "memoryInGBs": 6.0}}

#: 可用域（大阪）
AD = os.environ.get("OCI_AD", "ZYcV:AP-OSAKA-1-AD-1")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "_a1_watch_state.json")
LOG_FILE = "/opt/kol-skills-platform/data/_a1_watch.log"


def log(msg: str) -> None:
    """写日志（同时尝试写平台日志目录）。"""
    line = "[%s] %s" % (dt.datetime.now().strftime("%F %T"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(d: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def notify(title: str, body: str) -> bool:
    """用平台的飞书通道发通知（复用现有实现，不另造轮子）。"""
    # 复用平台的 alert_once.sh —— 它天生适合这个场景：
    #   · 只在「状态变化」时发提醒（有货→提醒；缺货→只重置状态）
    #   · 同一状态不重复提醒，恢复后自动重置
    # 故去重逻辑不必自己实现，交给已有且经过验证的组件。
    script = "/opt/kol-skills-platform/scripts/alert_once.sh"
    if not os.path.isfile(script):
        log("  ⚠️ 未找到 alert_once.sh（通知已写入日志）")
        return False
    try:
        r = subprocess.run(["bash", script, "a1_capacity", "below", body],
                           capture_output=True, text=True, timeout=90)
        if r.returncode == 0:
            log("  已通过 alert_once.sh 发送通知（同状态自动去重）")
            return True
        log("  alert_once.sh 返回 %s: %s" % (r.returncode, (r.stderr or r.stdout or "")[:150]))
    except Exception as e:
        log("  alert_once.sh 调用异常: %s" % e)
    return False


def check_capacity():
    """查询 A1 容量。返回 (status, detail)。"""
    try:
        import oci_action as A
    except ImportError:
        # 回退：直接用平台内的签名工具（服务器上可能没有 oci_action）
        log("  ⚠️ 未找到 oci_action 模块，无法查询")
        return None, "no_oci_module"

    body = {"compartmentId": A._C["tenancy"], "availabilityDomain": AD,
            "shapeAvailabilities": [TARGET]}
    try:
        r = A.post_json("/20160918/computeCapacityReports", body)
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:100])

    if r.status_code not in (200, 201):
        return None, "HTTP %s: %s" % (r.status_code, r.text[:150])

    try:
        d = r.json()
        item = (d.get("shapeAvailabilities") or [{}])[0]
        return item.get("availabilityStatus"), json.dumps(item, ensure_ascii=False)[:200]
    except Exception as e:
        return None, "解析失败: %s" % e


def main() -> int:
    ap = argparse.ArgumentParser(description="A1.Flex 容量探测")
    ap.add_argument("--force", action="store_true", help="忽略去重，强制通知")
    args = ap.parse_args()

    status, detail = check_capacity()
    today = dt.datetime.now().strftime("%F")
    state = load_state()

    if status is None:
        log("探测失败: %s" % detail)
        return 1

    log("状态: %s" % status)

    if status == "AVAILABLE":
        if not args.force and state.get("notified_date") == today:
            log("  今日已通知过，跳过")
            return 0
        log("  ★ 有可用容量！发送通知")
        notify("A1.Flex 有容量了",
               "🟢 **A1.Flex（ARM）出现可用容量**\n\n"
               "规格：1 OCPU / 6GB（当前服务器只有 1GB）\n"
               "区域：ap-osaka-1（大阪）\n"
               "探测时间：%s\n\n"
               "如需升级内存，请告知，我来执行迁移。"
               % dt.datetime.now().strftime("%F %T"))
        state["notified_date"] = today
        state["notified_at"] = dt.datetime.now().strftime("%F %T")
        save_state(state)
        return 0

    # 缺货：静默（避免每小时打扰），只记状态
    state["last_status"] = status
    state["last_checked"] = dt.datetime.now().strftime("%F %T")
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

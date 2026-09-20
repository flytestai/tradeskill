#!/usr/bin/env bash
# ============================================================================
# 群问答 24×7 监控安装 —— **兼容转发层**（2026-09-20 起）
#
# 历史
# ---------------------------------------------------------------------------
# 本脚本原本会**自己生成** runner `_run_qa.sh`，并用独立标记
# `# kol-platform-qa` 注册一条 cron。当时的理由：群问答要 24 小时，而其它
# 任务都带交易日语义，「用独立运行器更清晰」。
#
# 但实测下来，两个 runner 的代价大于收益：
#   1) 两套标记 → 登记/卸载必须成对执行，漏一个就留下幽灵任务
#   2) 守卫语义不一致 → 非交易日仍在空转
#   3) 群问答链路「在不在跑」在 setup_host_tasks.sh 里完全看不出来
# 故已合并为**单一 runner** `_run_task.sh`（含 --trading/--lock/--qa 能力），
# 群问答作为其中一条任务（qa-poll）统一登记。
#
# 现在本脚本只做两件事：
#   ① 迁移：清掉历史的 _run_qa.sh 与 # kol-platform-qa 标记（防幽灵任务）
#   ② 转发：调用 setup_host_tasks.sh 完成（重新）登记
# 保留本文件与原参数，是为了不打断既有文档/习惯用法。
#
# 用法（与旧版一致）：
#   sudo bash scripts/deploy/setup_qa_24x7.sh              # 每 2 分钟
#   sudo bash scripts/deploy/setup_qa_24x7.sh --every 1    # 每 1 分钟（更快）
#   sudo bash scripts/deploy/setup_qa_24x7.sh --uninstall  # 移除群问答任务
# ============================================================================
set -uo pipefail

DEPLOY_DIR="/opt/kol-skills-platform"
LEGACY_MARK="# kol-platform-qa"
LEGACY_RUNNER="$DEPLOY_DIR/scripts/deploy/_run_qa.sh"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"

UNINSTALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --every) EVERY="${2:-2}"; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        *) shift ;;
    esac
done

if [ "${UNINSTALL:-0}" = "1" ]; then
    echo "=== 移除群问答任务（含历史独立 runner）==="
    crontab -l 2>/dev/null | grep -vE "$LEGACY_MARK|qa-poll" | crontab - || true
    if [ -f "$LEGACY_RUNNER" ]; then
        rm -f "$LEGACY_RUNNER"
        echo "  ✅ 已删除历史 runner $LEGACY_RUNNER"
    fi
    echo "  剩余定时任务："
    crontab -l 2>/dev/null | grep -v "^#" | sed 's/^/     /' || echo "     (crontab 为空)"
    exit 0
fi

echo "=== 群问答 24×7（已并入统一 runner，本脚本转为转发）==="

# ---- ① 清理历史遗留：独立 runner + 独立标记 --------------------------------
# ⚠️ 必须清掉 _run_qa.sh：若它与 _run_task.sh 同时在 cron 里，
#    两条链路会同时消费同一队列 → 用户被**重复回复**。
crontab -l 2>/dev/null | grep -v "$LEGACY_MARK" | crontab - || true
if [ -f "$LEGACY_RUNNER" ]; then
    rm -f "$LEGACY_RUNNER"
    echo "  ✅ 已移除历史 runner _run_qa.sh 与标记 $LEGACY_MARK"
else
    echo "  ✅ 无历史 runner _run_qa.sh（已是合并形态）"
fi

# ---- ② 转发到统一登记入口 ---------------------------------------------------
if [ "${EVERY:-2}" != "2" ]; then
    echo "  ⚠️ --every $EVERY 已忽略：统一 runner 的 qa-poll 固定每 2 分钟。"
    echo "     需要调频请直接改 setup_host_tasks.sh 里 qa-poll 的 cron 字段。"
    echo "     （为什么默认 2 分钟：每轮都真实调用飞书 API，1 分钟 = 1440 次/天，"
    echo "       有触发飞书侧限流的风险；2 分钟在「及时回复」与「稳定」间取平衡）"
fi

# ⚠️ 只重装 qa-poll 一条要处理保序与去重，而 setup_host_tasks.sh 是**幂等**的
#    （先按统一标记清空，再全量登记），直接调用最稳。
if [ -f "$SELF_DIR/setup_host_tasks.sh" ]; then
    bash "$SELF_DIR/setup_host_tasks.sh"
else
    echo "  ❌ 未找到 $SELF_DIR/setup_host_tasks.sh"; exit 1
fi

echo ""
echo "=== 完成 ==="
echo "  群问答任务：qa-poll（每 2 分钟，24×7，含节假日）"
echo "  运行器    ：$DEPLOY_DIR/scripts/deploy/_run_task.sh --qa"
echo "  错误日志  ：$DEPLOY_DIR/data/_task_errors.log（仅异常时写入）"
echo "  卸载      ：bash $0 --uninstall"

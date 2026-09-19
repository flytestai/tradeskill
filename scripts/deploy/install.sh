#!/usr/bin/env bash
# ============================================================================
# KOL Skills Platform —— Linux 一键部署脚本
#
# 用法：
#   sudo bash scripts/deploy/install.sh                 # 完整安装
#   sudo bash scripts/deploy/install.sh --no-lark       # 跳过 lark-cli 安装
#   sudo bash scripts/deploy/install.sh --dir /opt/kol  # 自定义安装目录
#
# 做的事：
#   1. 创建系统用户 kol 与目录
#   2. 安装 Python 依赖（mcp / flask）与 lark-cli（Node）
#   3. 生成 /etc/kol-platform/env（API Key 等敏感配置）
#   4. 安装 systemd 单元并启动
#   5. 自检（健康检查 + MCP tools 列表）
#
# 幂等：可重复执行，已存在的配置不会被覆盖。
# ============================================================================
set -euo pipefail

TARGET_DIR="/opt/kol-opinion-analyzer"
ENV_DIR="/etc/kol-platform"
LOG_DIR="/var/log/kol-platform"
SVC_USER="kol"
INSTALL_LARK=1

while [ $# -gt 0 ]; do
    case "$1" in
        --no-lark) INSTALL_LARK=0; shift ;;
        --dir)     TARGET_DIR="$2"; shift 2 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
done

log() { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 sudo 运行"

# ---------------------------------------------------------------------------
# 1. 用户与目录
# ---------------------------------------------------------------------------
log "创建用户与目录"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd -r -m -s /bin/bash "$SVC_USER"
mkdir -p "$TARGET_DIR" "$ENV_DIR" "$LOG_DIR"
chown -R "$SVC_USER:$SVC_USER" "$TARGET_DIR" "$LOG_DIR"
chmod 700 "$ENV_DIR"

# ---------------------------------------------------------------------------
# 2. 依赖
# ---------------------------------------------------------------------------
log "安装系统依赖"
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq python3 python3-pip python3-venv git curl ca-certificates
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q python3 python3-pip git curl ca-certificates
elif command -v yum >/dev/null 2>&1; then
    yum install -y -q python3 python3-pip git curl ca-certificates
else
    warn "未识别的包管理器，请自行确保 python3/pip/git/curl 已安装"
fi

# ---------------------------------------------------------------------------
# 3. 代码
# ---------------------------------------------------------------------------
if [ -f "$TARGET_DIR/scripts/api/rest_app.py" ]; then
    log "代码已存在，跳过拉取（如需更新：cd $TARGET_DIR && sudo -u $SVC_USER git pull）"
else
    if [ -d "$TARGET_DIR/.git" ]; then
        log "复用已有 git 仓库"
    else
        log "从 GitHub 拉取代码"
        sudo -u "$SVC_USER" git clone https://github.com/flytestai/tradeskill.git "$TARGET_DIR" \
            || die "拉取失败。可手动放置代码到 $TARGET_DIR 后重跑本脚本"
    fi
fi

log "安装 Python 依赖"
sudo -u "$SVC_USER" python3 -m pip install --user -q --upgrade pip || true
sudo -u "$SVC_USER" python3 -m pip install --user -q flask "mcp"

# ---------------------------------------------------------------------------
# 4. lark-cli（Node）
# ---------------------------------------------------------------------------
if [ "$INSTALL_LARK" = "1" ]; then
    if command -v lark-cli >/dev/null 2>&1; then
        log "lark-cli 已安装，跳过"
    else
        log "安装 lark-cli"
        if ! command -v npm >/dev/null 2>&1; then
            if command -v apt-get >/dev/null 2>&1; then
                curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null 2>&1 || true
                apt-get install -y -qq nodejs || warn "Node 安装失败，请手动安装后重跑"
            else
                warn "未安装 Node，请手动安装：https://nodejs.org"
            fi
        fi
        if command -v npm >/dev/null 2>&1; then
            npm install -g @larksuite/cli >/dev/null 2>&1 || warn "lark-cli 安装失败"
            command -v lark-cli >/dev/null 2>&1 && log "lark-cli 安装完成" \
                || warn "lark-cli 不在 PATH，请检查 npm 全局 bin 路径"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 5. 敏感配置
# ---------------------------------------------------------------------------
ENV_FILE="$ENV_DIR/env"
if [ -f "$ENV_FILE" ]; then
    log "配置已存在，保留：$ENV_FILE"
else
    log "生成配置：$ENV_FILE"
    API_KEY="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    cat > "$ENV_FILE" <<EOF
# KOL Skills Platform 配置（权限 600，勿提交到 git）
# 生成时间：$(date '+%Y-%m-%d %H:%M:%S')

# ---- 鉴权 ----
# 多个 Key 用逗号分隔；留空则不做校验（仅限本机绑定）
# 完整格式：key:tenant:scope1|scope2
PLATFORM_API_KEYS=$API_KEY

# ---- 监听 ----
PLATFORM_HOST=127.0.0.1
PLATFORM_PORT=8000
PLATFORM_MCP_PORT=8001

# ---- 数据源通道 ----
# http  = 蜜蜂技能网关（默认）
# local = 公开行情源（完全脱离蜜蜂）
# 蜜蜂不可达时是否自动降级到 local
BEE_CHANNEL=http
BEE_FALLBACK_LOCAL=0

# ---- 飞书（从 Windows 机器拷贝，或用 lark-cli auth login 重新授权）----
# WU2198_CHAT_ID=oc_xxx
# VIP_PUSH_CHAT_ID=oc_xxx
# REVIEW_CHAT_ID=oc_xxx
# USER_OPEN_ID=ou_xxx
EOF
    chmod 600 "$ENV_FILE"
    echo
    warn "已生成 API Key，请记录（仅显示一次）："
    echo "    $API_KEY"
    echo
fi

# 同步飞书配置（若 skill 目录内已有 local_config.env）
SRC_CONF="$TARGET_DIR/data/local_config.env"
if [ -f "$SRC_CONF" ]; then
    log "发现 data/local_config.env，合并飞书配置到 $ENV_FILE"
    chmod 600 "$SRC_CONF"
    # 仅追加尚未存在的键，避免覆盖已生成的 API Key
    while IFS='=' read -r k v; do
        case "$k" in ''|\#*) continue ;; esac
        grep -q "^${k}=" "$ENV_FILE" || echo "${k}=${v}" >> "$ENV_FILE"
    done < "$SRC_CONF"
fi

# ---------------------------------------------------------------------------
# 6. systemd
# ---------------------------------------------------------------------------
log "安装 systemd 单元"
for unit in kol-platform.service kol-platform-mcp.service; do
    src="$TARGET_DIR/scripts/deploy/systemd/$unit"
    [ -f "$src" ] || { warn "缺少 $unit，跳过"; continue; }
    sed "s|/opt/kol-opinion-analyzer|$TARGET_DIR|g" "$src" > "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now kol-platform kol-platform-mcp

# ---------------------------------------------------------------------------
# 6b. 日志轮转
#
# 为什么需要（实测结论）
#   平台日志此前**完全没有轮转**：_host_task.log / _qa_analyzer.log 等都是
#   >> 追加写入，而 supervisor.py 里那个 rotate_logs_if_needed() 只服务
#   **Windows 侧**的 supervisor —— Linux 上跑的是 _run_task.sh，不经过它。
#   实测 _backend.log 约 60MB/年、_host_task.log 约 18MB/年，不轮转会永久累积。
#   系统其它服务（nginx 等）都用 logrotate，故这里与之一致。
# ---------------------------------------------------------------------------
log "安装 logrotate 配置"
LR_SRC="$TARGET_DIR/scripts/deploy/logrotate-kol-platform"
if [ -f "$LR_SRC" ]; then
    # 路径里的部署目录按实际位置替换（默认即 /opt/kol-skills-platform）
    sed "s|/opt/kol-skills-platform|$TARGET_DIR|g" "$LR_SRC" > /etc/logrotate.d/kol-platform
    # 干跑验证（配置有语法错时 logrotate 会报出来）
    if logrotate -d /etc/logrotate.d/kol-platform >/dev/null 2>&1; then
        log "logrotate 配置已安装并验证 ✅"
    else
        warn "logrotate 配置校验失败，请检查 /etc/logrotate.d/kol-platform"
    fi
else
    warn "缺少 logrotate 配置模板，跳过"
fi

# ---------------------------------------------------------------------------
# 7. 自检
# ---------------------------------------------------------------------------
log "等待服务就绪..."
sleep 4
systemctl is-active --quiet kol-platform && log "kol-platform 运行中 ✅" \
    || { warn "kol-platform 未启动，请查看：journalctl -u kol-platform -n 50"; }

API_KEY_VAL="$(grep '^PLATFORM_API_KEYS=' "$ENV_FILE" | cut -d= -f2- | cut -d, -f1)"
log "REST 健康检查"
if command -v curl >/dev/null 2>&1; then
    curl -s -H "X-API-Key: $API_KEY_VAL" http://127.0.0.1:8000/healthz | head -c 600
    echo
fi

cat <<EOF

==================== 部署完成 ====================
代码目录 : $TARGET_DIR
配置     : $ENV_FILE  (chmod 600)
日志     : $LOG_DIR/
REST API : http://127.0.0.1:8000     (API Key 见 $ENV_FILE)
MCP      : http://127.0.0.1:8001/mcp

常用命令：
  systemctl status kol-platform kol-platform-mcp
  journalctl -u kol-platform -f
  curl -H "X-API-Key: \$KEY" http://127.0.0.1:8000/api/v1/kol/list

接入 Agent：
  蜜蜂      mcp_manage upsert name=kol-platform transport=streamable-http url=http://<host>:8001/mcp
  WorkBuddy 在 mcp.json 加 {"kol-platform":{"type":"streamableHttp","url":"http://<host>:8001/mcp"}}

⚠️ 跨机访问请务必用 Nginx/Caddy 反代 + TLS，不要直接暴露 8000/8001。
EOF

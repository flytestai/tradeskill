#!/usr/bin/env bash
# ============================================================
#  kol-platform MCP 一键安装脚本（所有平台统一命令）
#  -----------------------------------------------------------
#  把「财经大V + 行情数据平台」kol-platform 接入本机所有可用平台，
#  无需下载 zip、无需手动改文件、无需指定平台 —— 一条命令完成。
#
#  统一用法（只传密钥，脚本自动检测并安装到本机已安装的平台）：
#    bash install.sh <API_KEY>
#    KOL_API_KEY=<API_KEY> bash install.sh
#
#  推荐发给客户的「一键安装」方式（直接从 GitHub 拉取并运行）：
#    bash <(curl -fsSL https://raw.githubusercontent.com/flytestai/tradeskill/main/install.sh) <API_KEY>
#
#  说明：
#    - API_KEY 由平台管理员签发（见 README「多租户多密钥」章节）。
#    - 公网端点 https://skill.flytest.com.cn/mcp
#      （服务器本机可用 http://127.0.0.1:8021/mcp）
# ============================================================
set -euo pipefail

API_KEY="${1:-${KOL_API_KEY:-}}"
ENDPOINT="${KOL_ENDPOINT:-https://skill.flytest.com.cn/mcp}"

usage() {
  cat <<'EOF'
用法: bash install.sh <API_KEY>

说明:
  - 一条命令安装到本机「所有可用平台」：WorkBuddy / Hermes / Bee / Codex / Claude Code
  - 文件型平台（WorkBuddy/Hermes/Bee）直接写配置
  - CLI 型平台（Codex/Claude Code）检测到命令就自动注册，检测不到则打印手动片段

示例:
  bash install.sh kol-xxxxxxxxxxxxxxxx
  KOL_API_KEY=kol-xxx bash install.sh
EOF
}

[[ -z "$API_KEY" ]] && { usage; exit 1; }

echo "==> 端点: $ENDPOINT"
echo "==> 密钥: ${API_KEY:0:8}...（共 ${#API_KEY} 位）"
echo

install_workbuddy() {
  local dir="$HOME/.workbuddy/connectors/kol-platform"
  mkdir -p "$dir"
  cat > "$dir/mcp.json" <<EOF
{
  "kol-platform": {
    "type": "streamableHttp",
    "url": "$ENDPOINT",
    "timeout": 30000,
    "headers": { "X-API-Key": "$API_KEY" }
  }
}
EOF
  echo "✅ WorkBuddy  : 已写入 $dir/mcp.json"
}

install_hermes() {
  local dir="$HOME/.hermes"
  local cfg="$dir/config.yaml"
  mkdir -p "$dir"
  if [[ -f "$cfg" ]] && grep -q "name: kol-platform" "$cfg" 2>/dev/null; then
    echo "⚠️ Hermes     : $cfg 已存在 kol-platform，跳过（如需更换密钥请手动编辑）"
  else
    cat >> "$cfg" <<EOF

mcp_servers:
  - name: kol-platform
    transport: streamable-http
    url: $ENDPOINT
    headers:
      X-API-Key: $API_KEY
EOF
    echo "✅ Hermes     : 已追加到 $cfg"
  fi
}

install_bee() {
  local dir="$HOME/.bee-pc-agent"
  mkdir -p "$dir"
  cat > "$dir/mcp.json" <<EOF
{
  "mcpServers": {
    "kol-platform": {
      "transport": "streamable-http",
      "url": "$ENDPOINT",
      "headers": { "X-API-Key": "$API_KEY" }
    }
  }
}
EOF
  echo "✅ Bee        : 已写入 $dir/mcp.json"
}

install_codex() {
  if command -v codex >/dev/null 2>&1; then
    codex mcp add kol-platform --transport http --url "$ENDPOINT" --header "X-API-Key: $API_KEY" \
      && echo "✅ Codex      : 已注册（~/.codex/config.toml）" \
      || echo "⚠️ Codex      : codex mcp add 失败，请手动配置"
  else
    echo "⏭️  Codex      : 未检测到 codex 命令，跳过（安装后见 README 手动配置）"
  fi
}

install_claude() {
  if command -v claude >/dev/null 2>&1; then
    claude mcp add kol-platform --transport http --url "$ENDPOINT" --header "X-API-Key: $API_KEY" \
      && echo "✅ Claude Code: 已注册（全局）" \
      || echo "⚠️ Claude Code: claude mcp add 失败，请手动配置"
  else
    echo "⏭️  Claude Code: 未检测到 claude 命令，跳过（安装后见 README 手动配置）"
  fi
}

echo "==> 开始安装到本机所有可用平台 ..."
echo
install_workbuddy
install_hermes
install_bee
install_codex
install_claude

echo
echo "===== 验证密钥与连通性（返回 200 即成功；401 为 Key 错误） ====="
echo "curl -s -o /dev/null -w '%{http_code}\\n' -X POST $ENDPOINT \\"
echo "  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \\"
echo "  -H 'X-API-Key: $API_KEY' \\"
echo "  -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},\"clientInfo\":{\"name\":\"t\",\"version\":\"0\"}}}'"

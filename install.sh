#!/usr/bin/env bash
# ============================================================
#  kol-platform MCP 一键安装脚本
#  -----------------------------------------------------------
#  把「财经大V + 行情数据平台」kol-platform 接入指定 Agent 平台。
#  无需下载 zip、无需手动改配置文件 —— 一条命令完成。
#
#  用法：
#    bash install.sh <platform> <API_KEY>
#    platform: workbuddy | codex | hermes | claude-code | bee
#
#  推荐发给客户的「一键安装」方式（直接从 GitHub 拉取并运行）：
#    bash <(curl -fsSL https://raw.githubusercontent.com/flytestai/tradeskill/main/install.sh) workbuddy kol-xxxx
#
#  说明：
#    - API_KEY 由平台管理员签发（见 README「多租户多密钥」章节）。
#      当前管理员密钥见 README；建议一人/一平台一把独立密钥。
#    - 公网端点 https://skill.flytest.com.cn/mcp
#      （服务器本机可用 http://127.0.0.1:8021/mcp）
# ============================================================
set -euo pipefail

PLATFORM="${1:-}"
API_KEY="${2:-${KOL_API_KEY:-}}"
ENDPOINT="${KOL_ENDPOINT:-https://skill.flytest.com.cn/mcp}"

usage() {
  cat <<'EOF'
用法: bash install.sh <platform> <API_KEY>

platform:
  workbuddy    WorkBuddy        写入 ~/.workbuddy/connectors/kol-platform/mcp.json
  codex        OpenAI Codex CLI 执行 codex mcp add
  hermes       Hermes           追加 ~/.hermes/config.yaml 的 mcp_servers 段
  claude-code  Claude Code/Cursor 执行 claude mcp add（或写 .mcp.json）
  bee          Bee 蜜蜂         写入 ~/.bee-pc-agent/mcp.json

示例:
  bash install.sh workbuddy kol-xxxxxxxxxxxxxxxx
  KOL_API_KEY=kol-xxx bash install.sh codex
EOF
}

[[ -z "$PLATFORM" ]] && { usage; exit 1; }
[[ -z "$API_KEY" ]] && { echo "❌ 缺少 API_KEY：请用  bash install.sh $PLATFORM <API_KEY>"; echo; usage; exit 1; }

echo "==> 平台: $PLATFORM"
echo "==> 端点: $ENDPOINT"
echo "==> 密钥: ${API_KEY:0:8}...（共 ${#API_KEY} 位）"
echo

case "$PLATFORM" in
  workbuddy)
    DIR="$HOME/.workbuddy/connectors/kol-platform"
    mkdir -p "$DIR"
    cat > "$DIR/mcp.json" <<EOF
{
  "kol-platform": {
    "type": "streamableHttp",
    "url": "$ENDPOINT",
    "timeout": 30000,
    "headers": { "X-API-Key": "$API_KEY" }
  }
}
EOF
    echo "✅ 已写入 $DIR/mcp.json"
    echo "   重启 WorkBuddy 后即可看到 kol-platform 的 19 个工具。"
    ;;

  codex)
    if ! command -v codex >/dev/null 2>&1; then
      echo "⚠️ 未检测到 codex 命令，请先安装 Codex CLI（https://github.com/openai/codex）。"
    fi
    codex mcp add kol-platform --transport http --url "$ENDPOINT" --header "X-API-Key: $API_KEY" \
      && echo "✅ 已添加 kol-platform 到 Codex（~/.codex/config.toml），重启 codex 会话生效。" \
      || echo "⚠️ codex mcp add 失败；请按 README 手动配置 ~/.codex/config.toml"
    ;;

  hermes)
    DIR="$HOME/.hermes"
    mkdir -p "$DIR"
    CFG="$DIR/config.yaml"
    if [[ -f "$CFG" ]] && grep -q "name: kol-platform" "$CFG" 2>/dev/null; then
      echo "⚠️ $CFG 已存在 kol-platform，跳过写入（如需更换密钥请手动编辑）。"
    else
      cat >> "$CFG" <<EOF

mcp_servers:
  - name: kol-platform
    transport: streamable-http
    url: $ENDPOINT
    headers:
      X-API-Key: $API_KEY
EOF
      echo "✅ 已追加到 $CFG"
    fi
    echo "   重启 Hermes 后生效。"
    ;;

  claude-code)
    if command -v claude >/dev/null 2>&1; then
      claude mcp add kol-platform --transport http --url "$ENDPOINT" --header "X-API-Key: $API_KEY" \
        && echo "✅ 已通过 claude mcp add 添加 kol-platform（全局）。" \
        && { echo; echo "💡 项目级可把下面内容写入项目根目录 .mcp.json："; }
    else
      echo "⚠️ 未检测到 claude 命令，请把下面内容写入项目根目录 .mcp.json："
    fi
    cat <<EOF
{
  "mcpServers": {
    "kol-platform": {
      "type": "http",
      "url": "$ENDPOINT",
      "headers": { "X-API-Key": "$API_KEY" }
    }
  }
}
EOF
    ;;

  bee)
    DIR="$HOME/.bee-pc-agent"
    mkdir -p "$DIR"
    cat > "$DIR/mcp.json" <<EOF
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
    echo "✅ 已写入 $DIR/mcp.json"
    echo "   在 Bee 里刷新/重连 MCP 即可看到 kol-platform 的 19 个工具。"
    ;;

  *)
    echo "❌ 未知平台: $PLATFORM"
    usage
    exit 1
    ;;
esac

echo
echo "===== 验证密钥与连通性（返回 200 即成功；401 为 Key 错误） ====="
echo "curl -s -o /dev/null -w '%{http_code}\\n' -X POST $ENDPOINT \\"
echo "  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \\"
echo "  -H 'X-API-Key: $API_KEY' \\"
echo "  -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},\"clientInfo\":{\"name\":\"t\",\"version\":\"0\"}}}'"

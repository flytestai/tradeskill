"""Skills Platform API —— 跨 Agent 能力接口层。

设计原则
--------
1. **薄接口、厚脚本**：业务逻辑仍在 `scripts/*.py`，本层只做协议适配与编排，
   不复制任何分析逻辑（避免"改一处忘一处"）。
2. **协议双栈**：`rest_app.py`（Flask，通用/可脚本化）+ `mcp_server.py`
   （MCP，Agent 原生发现）。两者共用 `services.py` 门面。
3. **零侵入**：不修改被包装脚本的行为；平台层可随时停用，原系统照常运行。
4. **可降级**：蜜蜂网关不可达时可切公开行情源（见 `bee_client`）。

模块
----
    config     配置加载（环境变量优先，回退 data/local_config.env）
    auth       API Key 鉴权（单租户默认，预留 tenant 字段）
    services   业务门面：封装对现有 CLI 脚本的调用
    rest_app   Flask REST 接口
    mcp_server MCP 服务端（需 pip install mcp）
    run        统一启动入口
"""
__all__ = ["config", "auth", "services", "rest_app", "mcp_server"]

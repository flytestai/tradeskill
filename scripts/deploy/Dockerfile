# ============================================================================
# KOL Skills Platform —— 容器镜像
#
# 为什么用 python:3.11-slim 而不是宿主机的 Python 3.7.3：
#   - 项目代码使用 PEP 604（`str | None`）与 shlex.join()，需 3.8+
#   - MCP SDK 需 3.10+
#   容器化同时保证了与宿主机其他服务（Python 3.7 的 flytest 等）完全隔离。
#
# 构建：
#   docker build -f scripts/deploy/Dockerfile -t kol-skills-platform:latest .
# 或（如需 MCP 能力）：
#   docker build -f scripts/deploy/Dockerfile --build-arg INSTALL_MCP=1 -t kol-skills-platform:latest .
# ============================================================================
FROM python:3.11-slim-bookworm

ARG INSTALL_MCP=0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_PROGRESS_BAR=off \
    BEE_CHANNEL=http \
    BEE_FALLBACK_LOCAL=0 \
    PLATFORM_HOST=0.0.0.0 \
    PLATFORM_PORT=8000 \
    PLATFORM_MCP_PORT=8000

WORKDIR /app

# ⚠️ 刻意不使用 apt-get。
#    实测该服务器的 Docker 18.09 环境下 apt 不可用：
#      1) 删 docker-clean 钩子后仍报 APT::Update::Post-Invoke 错误
#      2) 改用 bookworm 后又遇 GPG 验签失败（NO_PUBKEY，源密钥不匹配）
#    时区需求改由 pip 的 tzdata 包满足（见 requirements-rest.txt），
#    Python 的 zoneinfo 会自动回退到该包，无需系统级 tzdata。
#    好处：构建更快、无外部 apt 依赖、不受镜像源状态影响。

# Python 依赖（先装依赖，利用层缓存）
#
# 说明：--progress-bar off + PIP_PROGRESS_BAR=off 是必需的。
# 目标服务器资源紧张时，pip 的 rich 进度条会尝试启动后台刷新线程并
# 抛 "RuntimeError: can't start new thread" 导致构建失败（已实测）。
# 关闭进度条后不再创建该线程。
COPY scripts/deploy/requirements-rest.txt /tmp/req-rest.txt
RUN pip install --no-cache-dir --progress-bar off -r /tmp/req-rest.txt

# 可选：MCP 依赖（体积较大，按需开启）
COPY scripts/deploy/requirements-mcp.txt /tmp/req-mcp.txt
RUN if [ "$INSTALL_MCP" = "1" ]; then \
      pip install --no-cache-dir --progress-bar off -r /tmp/req-mcp.txt; \
    fi

# 应用代码
COPY . /app

# 数据目录（挂载卷）
RUN mkdir -p /app/data /app/sync

EXPOSE 8000

# 健康检查：用 Python 标准库而非 curl，省掉一个系统依赖，
# 也避免老版 Docker 对 HEALTHCHECK 转义的处理差异。
#
# ⚠️ 仅对默认的 REST 镜像有意义。
#    MCP 模式下只暴露 /mcp（POST + SSE），探 /healthz 必然 404，
#    会导致容器被误标为 unhealthy —— 因此启动 MCP 时必须用
#    `--no-healthcheck` 覆盖（见 start.sh / 部署文档）。
HEALTHCHECK --interval=60s --timeout=15s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=10).status==200 else 1)"

# 默认启动 REST；MCP 由 compose/start.sh 覆盖 command
# 用脚本路径而非 -m 模块方式：
# api 包位于 scripts/ 下，不在仓库根，`-m api.rest_app` 会 ModuleNotFoundError。
# 直接执行脚本时，脚本内的 sys.path.insert 会正确完成自举。
CMD ["python", "scripts/api/rest_app.py"]

FROM python:3.12-slim

WORKDIR /app

ARG PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ENV PIP_INDEX_URL=${PYPI_INDEX_URL} \
    UV_DEFAULT_INDEX=${PYPI_INDEX_URL} \
    UV_INDEX_URL=${PYPI_INDEX_URL} \
    UV_CONCURRENT_DOWNLOADS=4 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH="/app/.venv/bin:$PATH"

# 安装必要系统依赖 (curl, ca-certificates)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv
RUN --mount=type=cache,target=/root/.cache/pip pip install uv

# 复制依赖描述，尽可能复用依赖安装层
COPY pyproject.toml uv.lock ./

# 安装项目依赖和测试依赖。
# uv.lock 固定了 PyPI 文件直链；默认中国镜像构建时在镜像层内重写前缀，
# 保持仓库锁文件不变，同时避免下载阶段绕回官方文件源。
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "${PYPI_INDEX_URL}" = "https://mirrors.aliyun.com/pypi/simple" ]; then \
      sed -i \
        -e 's#https://files.pythonhosted.org#https://mirrors.aliyun.com/pypi#g' \
        -e 's#https://pypi.org/simple#https://mirrors.aliyun.com/pypi/simple#g' \
        uv.lock; \
    fi \
    && uv sync --frozen --extra dev --no-install-project

# 创建挂载和运行时占位目录
RUN mkdir -p /app/src /app/scripts /app/web/dist /app/data /app/logs

# 默认启动行情服务；生产 Compose 会覆盖为具体服务
CMD ["python", "-m", "trading_platform.market"]

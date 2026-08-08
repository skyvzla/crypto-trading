# 账本 Web 面板构建阶段。产物只有静态文件，Node 不进运行镜像。
FROM node:22-alpine AS web

ARG NPM_REGISTRY=https://registry.npmjs.org
WORKDIR /web

# 先装依赖，锁文件不变时复用该层
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm config set registry "${NPM_REGISTRY}" && npm ci

COPY web/ ./
RUN npm run build


FROM python:3.12-slim

WORKDIR /app

ARG PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ENV PIP_INDEX_URL=${PYPI_INDEX_URL} \
    UV_DEFAULT_INDEX=${PYPI_INDEX_URL} \
    UV_INDEX_URL=${PYPI_INDEX_URL} \
    UV_CONCURRENT_DOWNLOADS=4 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 安装 uv
RUN --mount=type=cache,target=/root/.cache/pip pip install uv

# 先复制依赖描述，尽可能复用依赖安装层
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY tests/ ./tests/
COPY scripts/ ./scripts/

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
    && uv sync --frozen --extra dev

# Web 面板产物来自 web 阶段，与 Python 依赖层相互独立
COPY --from=web /web/dist ./web/dist

# 数据和日志由 Compose 挂载，镜像内只提供默认目录
RUN mkdir -p /app/data /app/logs

# 使用虚拟环境和 src 布局
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"

# 默认启动行情服务；生产 Compose 会覆盖为具体服务
CMD ["python", "-m", "trading_platform.market"]

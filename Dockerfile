# OpsPilot Next API 镜像（uv 管理依赖）
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# WITH_EMBEDDING=false 时跳过 sentence-transformers/torch（ops-action 等轻量组件）
ARG WITH_EMBEDDING=true
# 构建期 PyPI 镜像（默认清华源：主网络对 files.pythonhosted.org 不稳定；
# CI 测试 Job 不经 Docker 构建，不受影响）
ARG UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ENV UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX}

# 先装依赖（利用层缓存：仅 pyproject/lock 变化才重装；uv 缓存跨构建复用）
# D4：--group embedding（sentence-transformers，本地 e5 向量，RAG hybrid 运行时使用）
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$WITH_EMBEDDING" = "true" ]; then \
      uv sync --frozen --no-install-project --no-dev --group embedding; \
    else \
      uv sync --frozen --no-install-project --no-dev; \
    fi

# 再装项目本身
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$WITH_EMBEDDING" = "true" ]; then \
      uv sync --frozen --no-dev --group embedding; \
    else \
      uv sync --frozen --no-dev; \
    fi

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uv run uvicorn entrypoints.api:app --host 0.0.0.0 --port 8000"]

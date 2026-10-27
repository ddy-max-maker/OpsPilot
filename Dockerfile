# OpsPilot Next API 镜像（uv 管理依赖）
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 先装依赖（利用层缓存：仅 pyproject/lock 变化才重装）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# 再装项目本身
COPY . .
RUN uv sync --frozen --no-dev

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uv run uvicorn entrypoints.api:app --host 0.0.0.0 --port 8000"]

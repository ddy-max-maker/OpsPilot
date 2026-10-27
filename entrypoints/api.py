"""ops-api 入口：模块级 ASGI app 供 uvicorn 加载。

用法（Docker Compose / 本地开发）：
    uvicorn entrypoints.api:app --host 0.0.0.0 --port 8000
"""

from app.main import app

__all__ = ["app"]

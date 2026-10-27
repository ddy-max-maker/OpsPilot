"""健康检查 API（CONTRACTS §5）：

- GET /health/live   存活探针（不触达数据库）
- GET /health/ready  就绪探针（校验数据库连通）
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app.core.db import get_engine
from app.core.responses import Envelope, ok

router = APIRouter(prefix="/health", tags=["health"])


class HealthData(BaseModel):
    status: str


class ReadyData(BaseModel):
    status: str
    database: str


@router.get("/live", response_model=Envelope[HealthData])
async def live() -> Envelope[HealthData]:
    """存活：进程可响应即返回。"""
    return ok(HealthData(status="ok"))


@router.get("/ready", response_model=Envelope[ReadyData])
async def ready() -> Envelope[ReadyData]:
    """就绪：数据库连通方可返回 200，否则 503。"""
    from app.core.errors import ApiError, ErrorCode

    engine = get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise ApiError(
            status_code=503,
            code=ErrorCode.INTERNAL,
            message="database unavailable",
        ) from exc
    return ok(ReadyData(status="ok", database="up"))

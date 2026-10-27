"""告警仓储（SQLAlchemy 查询封装，不含事务提交）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert


async def get_by_delivery(
    session: AsyncSession, connector_id: str, external_event_id: str
) -> Alert | None:
    """delivery 去重查询（uq_alerts_delivery 兜底并发竞态）。"""
    stmt = select(Alert).where(
        Alert.connector_id == connector_id, Alert.external_event_id == external_event_id
    )
    result = await session.execute(stmt)
    return result.scalars().first()

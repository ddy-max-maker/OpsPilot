"""审计事件仓储（只读查询封装，不含事务提交）。"""

from __future__ import annotations

from sqlalchemy import ColumnExpressionArgument, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import Cursor
from app.models import AuditEvent


async def list_events(
    session: AsyncSession,
    *,
    actor_type: str | None = None,
    action: str | None = None,
    limit: int,
    cursor: Cursor | None = None,
) -> list[AuditEvent]:
    """条件过滤 + cursor 分页（created_at DESC, id DESC），取 limit+1 判 has_more。

    id 为 BigInteger，cursor 中以字符串表示，比较时转回 int。
    """
    conditions: list[ColumnExpressionArgument[bool]] = []
    if actor_type is not None:
        conditions.append(AuditEvent.actor_type == actor_type)
    if action is not None:
        conditions.append(AuditEvent.action == action)
    if cursor is not None:
        conditions.append(
            tuple_(AuditEvent.created_at, AuditEvent.id) < tuple_(cursor.created_at, int(cursor.id))
        )
    stmt = (
        select(AuditEvent)
        .where(*conditions)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit + 1)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())

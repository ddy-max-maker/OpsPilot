"""审计服务：audit_events 写入（只追加，随调用方事务提交）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.responses import current_request_id
from app.models import AuditEvent


async def record(
    session: AsyncSession,
    *,
    actor_type: str,
    actor_id: str,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    environment: str | None = None,
    detail: dict[str, Any] | None = None,
    commit: bool = False,
) -> None:
    """写一条审计事件（敏感信息不得进入 detail；PRD §8）。

    默认不提交：与调用方业务变更同事务落库（事务性审计）；
    commit=True 用于无业务写操作的独立动作（如登录）。
    """
    event = AuditEvent(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        environment=environment,
        detail=detail,
        request_id=current_request_id(),
    )
    session.add(event)
    if commit:
        await session.commit()

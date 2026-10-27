"""审计服务：audit_events 写入（只追加，随调用方事务提交）+ 管理查询。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import validation_error
from app.core.pagination import Cursor, decode_cursor, encode_cursor
from app.core.responses import current_request_id
from app.domains.audit import repository
from app.domains.audit.schemas import AuditEventResponse, AuditListData
from app.models import AuditEvent

if TYPE_CHECKING:
    from app.core.config import Settings


def _to_response(event: AuditEvent) -> AuditEventResponse:
    return AuditEventResponse(
        id=int(event.id),
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        action=event.action,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        environment=event.environment,
        detail=event.detail,
        request_id=event.request_id,
        created_at=event.created_at,
    )


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


async def list_events(
    session: AsyncSession,
    settings: Settings,
    *,
    actor_type: str | None = None,
    action: str | None = None,
    limit: int,
    cursor: str | None = None,
) -> AuditListData:
    """审计事件列表（Admin/Viewer；cursor 分页）。

    id 为 BigInteger：编码为字符串，比较时转回 int。
    """
    if limit < 1:
        raise validation_error("limit must be >= 1")
    if limit > settings.max_page_size:
        raise validation_error(f"limit must be <= {settings.max_page_size}")
    decoded: Cursor | None = None
    if cursor:
        decoded = decode_cursor(cursor, settings.signing_secret)
    rows = await repository.list_events(
        session,
        actor_type=actor_type,
        action=action,
        limit=limit,
        cursor=decoded,
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(
            Cursor(created_at=last.created_at, id=str(int(last.id))), settings.signing_secret
        )
    return AuditListData(
        items=[_to_response(e) for e in items],
        next_cursor=next_cursor,
        has_more=has_more,
    )

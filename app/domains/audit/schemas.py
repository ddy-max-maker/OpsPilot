"""审计查询 Schema（对齐 CONTRACTS §2.15 / §1.3）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditEventResponse(BaseModel):
    """审计事件响应（id 为数据库自增主键）。"""

    id: int
    actor_type: str
    actor_id: str
    action: str
    resource_type: str | None
    resource_id: str | None
    environment: str | None
    detail: dict[str, Any] | None
    request_id: str
    created_at: datetime


class AuditListData(BaseModel):
    """cursor 分页列表（契约 §1.3：items + next_cursor + has_more）。"""

    items: list[AuditEventResponse]
    next_cursor: str | None
    has_more: bool

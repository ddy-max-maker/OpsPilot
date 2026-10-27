"""审计查询 API（CONTRACTS §5：GET /audit-logs，D1 起逐步丰富）。

角色矩阵（§7.1）：Admin 与 Viewer/Auditor 可查询；Operator/Approver 拒绝。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUser, PageLimitDep, SessionDep, SettingsDep, require_roles
from app.core.enums import ActorType
from app.core.responses import Envelope, ok
from app.domains.audit import service as audit_service
from app.domains.audit.schemas import AuditListData

router = APIRouter(prefix="/audit-logs", tags=["audit"])

AuditReaderDep = Annotated[CurrentUser, Depends(require_roles("admin", "viewer"))]


@router.get("", response_model=Envelope[AuditListData])
async def list_audit_logs(
    session: SessionDep,
    settings: SettingsDep,
    _reader: AuditReaderDep,
    actor_type: Annotated[ActorType | None, Query()] = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
    limit: PageLimitDep = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> Envelope[AuditListData]:
    """审计事件列表（cursor 分页；按 actor_type/action 过滤）。"""
    data = await audit_service.list_events(
        session,
        settings,
        actor_type=actor_type.value if actor_type else None,
        action=action,
        limit=limit,
        cursor=cursor,
    )
    return ok(data)

"""事件 API（CONTRACTS §5）：

- POST  /incidents                    创建（幂等：必须携带 Idempotency-Key）
- GET   /incidents                    列表（cursor 分页；状态/严重度/环境/资产过滤；read 范围）
- GET   /incidents/{id}               详情（read 范围；越权 404 伪装）
- POST  /incidents/{id}/acknowledge   故障确认（OPEN→ACKNOWLEDGED；investigate 范围）
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.deps import (
    CurrentUser,
    InvestigateScopeDep,
    PageLimitDep,
    ReadScopeDep,
    SessionDep,
    SettingsDep,
    require_roles,
)
from app.core.enums import Environment, IncidentStatus, Severity
from app.core.errors import idempotency_key_required
from app.core.responses import Envelope, ok
from app.domains.idempotency import service as idempotency_service
from app.domains.incidents import service as incident_service
from app.domains.incidents.schemas import (
    IncidentCreateRequest,
    IncidentListData,
    IncidentResponse,
)

router = APIRouter(prefix="/incidents", tags=["incidents"])

# CONTRACTS §7.1：创建/确认事件 = Operator/Approver/Admin（Viewer 拒绝）
IncidentActorDep = Annotated[CurrentUser, Depends(require_roles("operator", "approver", "admin"))]


def idempotency_key_header(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    return idempotency_key


IdempotencyKeyDep = Annotated[str | None, Depends(idempotency_key_header)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_incident(
    body: IncidentCreateRequest,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    current: IncidentActorDep,
    scope: InvestigateScopeDep,
    idempotency_key: IdempotencyKeyDep,
) -> JSONResponse:
    """创建事件（幂等端点）：无 key → 400；同 key 异 body → 409；同 key 同 body → 重放。"""
    if idempotency_key is None:
        raise idempotency_key_required()
    raw_body = await request.body()
    endpoint = idempotency_service.endpoint_tag("POST", request.url.path)

    async def execute() -> tuple[int, dict[str, object]]:
        data = await incident_service.create_incident(
            session, body, scope, current.user.id, commit=False
        )
        envelope: Envelope[IncidentResponse] = ok(data)
        return status.HTTP_201_CREATED, envelope.model_dump(mode="json")

    status_code, resp_body, replayed = await idempotency_service.run_idempotent(
        session,
        settings,
        user_id=current.user.id,
        endpoint=endpoint,
        key=idempotency_key,
        request_hash=idempotency_service.hash_request_body(raw_body),
        execute=execute,
    )
    headers = {"Idempotency-Replayed": "true"} if replayed else None
    return JSONResponse(status_code=status_code, content=resp_body, headers=headers)


@router.get("", response_model=Envelope[IncidentListData])
async def list_incidents(
    session: SessionDep,
    settings: SettingsDep,
    scope: ReadScopeDep,
    incident_status: Annotated[IncidentStatus | None, Query(alias="status")] = None,
    severity: Annotated[Severity | None, Query()] = None,
    environment: Annotated[Environment | None, Query()] = None,
    primary_asset_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: PageLimitDep = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> Envelope[IncidentListData]:
    """事件列表：read 范围过滤（默认拒绝），支持状态/严重度/环境/主资产筛选。"""
    data = await incident_service.list_incidents(
        session,
        settings,
        scope,
        environment=environment.value if environment else None,
        status=incident_status.value if incident_status else None,
        severity=severity.value if severity else None,
        primary_asset_id=primary_asset_id,
        limit=limit,
        cursor=cursor,
    )
    return ok(data)


@router.get("/{incident_id}", response_model=Envelope[IncidentResponse])
async def get_incident(
    incident_id: str,
    session: SessionDep,
    scope: ReadScopeDep,
) -> Envelope[IncidentResponse]:
    """事件详情：不存在或越权统一 404（CONTRACTS §7.2）。"""
    data = await incident_service.get_incident_detail(session, incident_id, scope)
    return ok(data)


@router.post("/{incident_id}/acknowledge", response_model=Envelope[IncidentResponse])
async def acknowledge_incident(
    incident_id: str,
    session: SessionDep,
    current: IncidentActorDep,
    scope: InvestigateScopeDep,
) -> Envelope[IncidentResponse]:
    """故障确认：状态机 OPEN→ACKNOWLEDGED（非法转换 409 CONFLICT_STATE）。"""
    data = await incident_service.acknowledge_incident(session, incident_id, scope, current.user.id)
    return ok(data)

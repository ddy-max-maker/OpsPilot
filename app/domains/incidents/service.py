"""事件服务：用例编排 + 显式事务边界（CONTRACTS §2.3 / §7）。

事务边界约定与资产领域一致：校验 → flush → 审计 → commit；
唯一约束竞态由 IntegrityError 捕获回滚后映射 409。
范围语义：查看 = read 范围；创建/确认 = investigate 范围（PolicyPermission 维度），
越权一律 404 伪装（CONTRACTS §7.2，不暴露资源存在性）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Environment, IncidentSource, IncidentStatus, Severity
from app.core.errors import conflict_state, not_found, validation_error
from app.core.ids import new_id
from app.core.pagination import Cursor, decode_cursor, encode_cursor
from app.domains.access.scope import AssetScope
from app.domains.audit import service as audit_service
from app.domains.incidents import repository
from app.domains.incidents.schemas import (
    IncidentCreateRequest,
    IncidentListData,
    IncidentResponse,
)
from app.domains.incidents.state_machine import require_transition
from app.models import Asset, Incident

if TYPE_CHECKING:
    from app.core.config import Settings


def _to_response(incident: Incident) -> IncidentResponse:
    return IncidentResponse(
        id=incident.id,
        title=incident.title,
        description=incident.description,
        environment=Environment(str(incident.environment)),
        primary_asset_id=incident.primary_asset_id,
        source=IncidentSource(str(incident.source)),
        alert_id=incident.alert_id,
        fingerprint=incident.fingerprint,
        severity=Severity(str(incident.severity)),
        status=IncidentStatus(str(incident.status)),
        created_by=incident.created_by,
        resolved_at=incident.resolved_at,
        created_at=incident.created_at,
        updated_at=incident.updated_at,
    )


def _ensure_in_scope(scope: AssetScope, asset_id: str, environment: str) -> None:
    """范围判定：不在范围按 404 伪装（不暴露资源存在性）。"""
    if not scope.allows(asset_id, environment):
        raise not_found("incident not found")


async def create_incident(
    session: AsyncSession,
    req: IncidentCreateRequest,
    scope: AssetScope,
    actor_id: str,
    *,
    commit: bool = True,
) -> IncidentResponse:
    """手工创建事件（OPEN）。事务：资产校验 → 范围校验 → 指纹去重 → insert → 审计 → commit。

    commit=False 供幂等包装在同事务内存储响应（幂等服务负责最终提交）。
    """
    asset = await session.get(Asset, req.primary_asset_id)
    if asset is None:
        raise not_found("asset not found")
    _ensure_in_scope(scope, asset.id, str(asset.environment))
    if str(req.environment.value) != str(asset.environment):
        raise validation_error(
            "incident environment must match the primary asset environment",
            details={"asset_environment": str(asset.environment)},
        )
    if req.fingerprint is not None and (
        await repository.find_active_by_fingerprint(session, asset.id, req.fingerprint)
    ):
        raise conflict_state(
            "an active incident with the same asset and fingerprint already exists",
            details={"primary_asset_id": asset.id, "fingerprint": req.fingerprint},
        )
    incident = Incident(
        id=new_id("inc_"),
        title=req.title,
        description=req.description,
        environment=str(asset.environment),
        primary_asset_id=asset.id,
        source=IncidentSource.MANUAL.value,
        alert_id=None,
        fingerprint=req.fingerprint,
        severity=req.severity.value,
        status=IncidentStatus.OPEN.value,
        created_by=actor_id,
    )
    session.add(incident)
    await session.flush()
    await audit_service.record(
        session,
        actor_type="user",
        actor_id=actor_id,
        action="incident.create",
        resource_type="incident",
        resource_id=incident.id,
        environment=incident.environment,
        detail={
            "title": incident.title,
            "severity": incident.severity,
            "source": incident.source,
            "fingerprint": incident.fingerprint,
        },
    )
    if commit:
        try:
            await session.commit()
        except IntegrityError as exc:
            # 并发竞态：uq_incidents_active_fingerprint 由数据库兜底
            await session.rollback()
            raise conflict_state(
                "an active incident with the same asset and fingerprint already exists",
                details={"primary_asset_id": asset.id, "fingerprint": req.fingerprint},
            ) from exc
        await session.refresh(incident)
    return _to_response(incident)


async def get_incident_detail(
    session: AsyncSession, incident_id: str, scope: AssetScope
) -> IncidentResponse:
    """事件详情（read 范围；不存在或越权统一 404）。"""
    incident = await repository.get_incident(session, incident_id)
    if incident is None:
        raise not_found("incident not found")
    _ensure_in_scope(scope, incident.primary_asset_id, str(incident.environment))
    return _to_response(incident)


async def acknowledge_incident(
    session: AsyncSession,
    incident_id: str,
    scope: AssetScope,
    actor_id: str,
) -> IncidentResponse:
    """故障确认：OPEN → ACKNOWLEDGED（状态机服务端强制；非法转换 409）。"""
    incident = await repository.get_incident(session, incident_id)
    if incident is None:
        raise not_found("incident not found")
    _ensure_in_scope(scope, incident.primary_asset_id, str(incident.environment))
    current_status = IncidentStatus(str(incident.status))
    require_transition(current_status, IncidentStatus.ACKNOWLEDGED)
    incident.status = IncidentStatus.ACKNOWLEDGED.value
    session.add(incident)
    await audit_service.record(
        session,
        actor_type="user",
        actor_id=actor_id,
        action="incident.acknowledge",
        resource_type="incident",
        resource_id=incident.id,
        environment=incident.environment,
        detail={"from": current_status.value, "to": IncidentStatus.ACKNOWLEDGED.value},
    )
    try:
        await session.commit()
    except IntegrityError as exc:  # 理论不可达（状态变更不触唯一索引），保持事务边界一致
        await session.rollback()
        raise conflict_state("incident status update conflict") from exc
    await session.refresh(incident)
    return _to_response(incident)


async def list_incidents(
    session: AsyncSession,
    settings: Settings,
    scope: AssetScope,
    *,
    environment: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    primary_asset_id: str | None = None,
    limit: int,
    cursor: str | None = None,
) -> IncidentListData:
    """范围过滤的事件列表（cursor 分页；非法/篡改 cursor → 400）。"""
    if limit < 1:
        raise validation_error("limit must be >= 1")
    if limit > settings.max_page_size:
        raise validation_error(f"limit must be <= {settings.max_page_size}")
    decoded: Cursor | None = None
    if cursor:
        decoded = decode_cursor(cursor, settings.signing_secret)
    rows = await repository.list_incidents(
        session,
        scope,
        environment=environment,
        status=status,
        severity=severity,
        primary_asset_id=primary_asset_id,
        limit=limit,
        cursor=decoded,
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(
            Cursor(created_at=last.created_at, id=last.id), settings.signing_secret
        )
    return IncidentListData(
        items=[_to_response(i) for i in items],
        next_cursor=next_cursor,
        has_more=has_more,
    )

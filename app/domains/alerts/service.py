"""告警接入服务：事务内 告警落库 → 去重 → 关联/新建 incident → 回写 → 审计。

约定（CONTRACTS §2.2 / §5 / IMPLEMENTATION_PLAN §1.2）：
- 幂等主手段 = (connector_id, external_event_id) 唯一约束，不依赖 Idempotency-Key；
- 自动创建 incident 的主体 = 预置系统账号 usr_system_alerts（禁止登录），
  审计 actor_type=system；
- 重复 delivery：409 DUPLICATE_ALERT，details 携带既有 alert/incident 引用，
  不产生任何新的业务行（重放无副作用）；
- 同资产+指纹存在活跃单 → 告警映射到该单（不新建）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AlertStatus, IncidentSource, IncidentStatus
from app.core.errors import conflict_state, duplicate_alert, validation_error
from app.core.ids import new_id
from app.domains.alerts import repository
from app.domains.alerts.schemas import AlertIngestData, AlertIngestRequest
from app.domains.audit import service as audit_service
from app.domains.incidents import repository as incident_repository
from app.models import SYSTEM_USER_ID, Alert, Asset, Incident


async def ingest_alert(
    session: AsyncSession,
    connector_id: str,
    req: AlertIngestRequest,
) -> AlertIngestData:
    """告警接入（单事务）：落库 → 去重 → 关联/建单 → 审计 → commit。"""
    existing = await repository.get_by_delivery(session, connector_id, req.delivery_id)
    if existing is not None:
        raise duplicate_alert(
            details={"alert_id": existing.id, "incident_id": existing.incident_id}
        )
    asset = await session.get(Asset, req.asset_id)
    if asset is None:
        raise validation_error("unknown asset_id", details={"asset_id": req.asset_id})

    alert = Alert(
        id=new_id("alr_"),
        connector_id=connector_id,
        external_event_id=req.delivery_id,
        fingerprint=req.fingerprint,
        title=req.title,
        severity=req.severity.value,
        payload=req.payload,
        status=AlertStatus.RECEIVED.value,
        received_at=datetime.now(UTC),
    )
    session.add(alert)
    await session.flush()

    incident = await incident_repository.find_active_by_fingerprint(
        session, asset.id, req.fingerprint
    )
    incident_created = False
    if incident is None:
        incident = Incident(
            id=new_id("inc_"),
            title=req.title,
            description=req.description,
            environment=str(asset.environment),
            primary_asset_id=asset.id,
            source=IncidentSource.ALERT.value,
            alert_id=alert.id,
            fingerprint=req.fingerprint,
            severity=req.severity.value,
            status=IncidentStatus.OPEN.value,
            created_by=SYSTEM_USER_ID,
        )
        session.add(incident)
        await session.flush()
        incident_created = True
        await audit_service.record(
            session,
            actor_type="system",
            actor_id=SYSTEM_USER_ID,
            action="incident.create",
            resource_type="incident",
            resource_id=incident.id,
            environment=incident.environment,
            detail={
                "source": IncidentSource.ALERT.value,
                "alert_id": alert.id,
                "fingerprint": req.fingerprint,
            },
        )

    alert.status = AlertStatus.MAPPED.value
    alert.incident_id = incident.id
    await audit_service.record(
        session,
        actor_type="system",
        actor_id=SYSTEM_USER_ID,
        action="alert.ingest",
        resource_type="alert",
        resource_id=alert.id,
        environment=str(asset.environment),
        detail={
            "connector_id": connector_id,
            "delivery_id": req.delivery_id,
            "incident_id": incident.id,
            "incident_created": incident_created,
        },
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        # 并发竞态兜底：同 delivery 并发投递 → uq_alerts_delivery 唯一约束
        await session.rollback()
        race = await repository.get_by_delivery(session, connector_id, req.delivery_id)
        if race is not None:
            raise duplicate_alert(
                details={"alert_id": race.id, "incident_id": race.incident_id}
            ) from exc
        raise conflict_state("concurrent alert ingestion conflict") from exc
    await session.refresh(alert)
    return AlertIngestData(
        alert_id=alert.id, incident_id=incident.id, incident_created=incident_created
    )

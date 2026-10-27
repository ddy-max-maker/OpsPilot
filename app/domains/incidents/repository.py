"""事件仓储（SQLAlchemy 查询封装，不含事务提交）。"""

from __future__ import annotations

from sqlalchemy import ColumnExpressionArgument, and_, false, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import Cursor
from app.domains.access.scope import AssetScope
from app.models import Incident

# 活跃单状态（与局部唯一索引 uq_incidents_active_fingerprint 保持一致）
ACTIVE_STATUSES: tuple[str, ...] = (
    "open",
    "acknowledged",
    "investigating",
    "mitigating",
    "monitoring",
)


def _scope_condition(scope: AssetScope) -> ColumnExpressionArgument[bool] | None:
    """按主资产/环境做范围过滤；默认拒绝：空集合返回恒 False。"""
    if scope.is_admin or scope.all_assets:
        return None
    if not scope.asset_ids and not scope.environments:
        return false()
    conditions: list[ColumnExpressionArgument[bool]] = []
    if scope.asset_ids:
        conditions.append(Incident.primary_asset_id.in_(scope.asset_ids))
    if scope.environments:
        conditions.append(Incident.environment.in_(scope.environments))
    return conditions[0] if len(conditions) == 1 else and_(*conditions)


async def get_incident(session: AsyncSession, incident_id: str) -> Incident | None:
    return await session.get(Incident, incident_id)


async def find_active_by_fingerprint(
    session: AsyncSession, asset_id: str, fingerprint: str
) -> Incident | None:
    """同资产+指纹的活跃单（告警关联/建单去重依据）。"""
    stmt = select(Incident).where(
        Incident.primary_asset_id == asset_id,
        Incident.fingerprint == fingerprint,
        Incident.status.in_(ACTIVE_STATUSES),
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def list_incidents(
    session: AsyncSession,
    scope: AssetScope,
    *,
    environment: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    primary_asset_id: str | None = None,
    limit: int,
    cursor: Cursor | None = None,
) -> list[Incident]:
    """范围过滤 + 条件过滤 + cursor 分页（created_at DESC, id DESC），取 limit+1 判 has_more。"""
    conditions: list[ColumnExpressionArgument[bool]] = []
    scope_cond = _scope_condition(scope)
    if scope_cond is not None:
        conditions.append(scope_cond)
    if environment is not None:
        conditions.append(Incident.environment == environment)
    if status is not None:
        conditions.append(Incident.status == status)
    if severity is not None:
        conditions.append(Incident.severity == severity)
    if primary_asset_id is not None:
        conditions.append(Incident.primary_asset_id == primary_asset_id)
    if cursor is not None:
        conditions.append(
            tuple_(Incident.created_at, Incident.id) < tuple_(cursor.created_at, cursor.id)
        )
    stmt = (
        select(Incident)
        .where(*conditions)
        .order_by(Incident.created_at.desc(), Incident.id.desc())
        .limit(limit + 1)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def is_active_status(status: str) -> bool:
    return status in ACTIVE_STATUSES

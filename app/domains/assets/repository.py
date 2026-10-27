"""资产仓储（SQLAlchemy 查询封装，不含事务提交）。"""

from __future__ import annotations

from sqlalchemy import ColumnExpressionArgument, and_, false, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import Cursor
from app.domains.access.scope import AssetScope
from app.models import Asset


def _scope_condition(scope: AssetScope) -> ColumnExpressionArgument[bool] | None:
    """范围过滤条件；默认拒绝：空集合范围直接返回恒 False。"""
    if scope.is_admin or scope.all_assets:
        return None
    if not scope.asset_ids and not scope.environments:
        # sqlalchemy.false() 在 PostgreSQL 渲染为 false 关键字（func.false() 是非法函数调用）
        return false()
    conditions: list[ColumnExpressionArgument[bool]] = []
    if scope.asset_ids:
        conditions.append(Asset.id.in_(scope.asset_ids))
    if scope.environments:
        conditions.append(Asset.environment.in_(scope.environments))
    return conditions[0] if len(conditions) == 1 else and_(*conditions)


async def get_asset(session: AsyncSession, asset_id: str) -> Asset | None:
    return await session.get(Asset, asset_id)


async def name_environment_exists(
    session: AsyncSession,
    name: str,
    environment: str,
    exclude_id: str | None = None,
) -> bool:
    """UNIQUE(name, environment) 预检查（竞态由 DB 约束兜底）。"""
    stmt = select(Asset.id).where(Asset.name == name, Asset.environment == environment)
    if exclude_id is not None:
        stmt = stmt.where(Asset.id != exclude_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def list_assets(
    session: AsyncSession,
    scope: AssetScope,
    *,
    environment: str | None = None,
    asset_type: str | None = None,
    status: str | None = None,
    limit: int,
    cursor: Cursor | None = None,
) -> list[Asset]:
    """范围过滤 + 条件过滤 + cursor 分页（created_at DESC, id DESC），取 limit+1 判 has_more。"""
    conditions: list[ColumnExpressionArgument[bool]] = []
    scope_cond = _scope_condition(scope)
    if scope_cond is not None:
        conditions.append(scope_cond)
    if environment is not None:
        conditions.append(Asset.environment == environment)
    if asset_type is not None:
        conditions.append(Asset.asset_type == asset_type)
    if status is not None:
        conditions.append(Asset.status == status)
    if cursor is not None:
        conditions.append(tuple_(Asset.created_at, Asset.id) < tuple_(cursor.created_at, cursor.id))
    stmt = (
        select(Asset)
        .where(*conditions)
        .order_by(Asset.created_at.desc(), Asset.id.desc())
        .limit(limit + 1)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def count_all(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(Asset))
    return int(result.scalar_one())


async def count_by_name_environment(session: AsyncSession, name: str, environment: str) -> int:
    stmt = (
        select(func.count())
        .select_from(Asset)
        .where(Asset.name == name, Asset.environment == environment)
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())

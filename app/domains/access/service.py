"""访问控制服务：加载策略并解析范围（依赖 scope.py 纯函数）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.access.scope import AssetScope, PolicyRecord, resolve_scope
from app.models import AssetAccessPolicy, Role


async def get_effective_scope(
    session: AsyncSession,
    user_id: str,
    role_codes: list[str],
    permission: str = "read",
) -> AssetScope:
    """加载用户级 + 所属角色级策略，解析为资产范围（并集；默认拒绝）。"""
    stmt = select(AssetAccessPolicy).where(
        (AssetAccessPolicy.user_id == user_id)
        | (AssetAccessPolicy.role_id.in_(select(Role.id).where(Role.code.in_(role_codes))))
    )
    result = await session.execute(stmt)
    policies = [
        PolicyRecord(
            user_id=row.user_id,
            role_id=row.role_id,
            asset_id=row.asset_id,
            environment=row.environment,
            permission=row.permission,
        )
        for row in result.scalars().all()
    ]
    return resolve_scope(role_codes, policies, permission)

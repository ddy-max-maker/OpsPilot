"""用户/角色仓储查询（SQLAlchemy 只读封装）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Role, User, UserRole


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    stmt = select(User).where(User.username == username)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: str) -> User | None:
    return await session.get(User, user_id)


async def get_role_by_code(session: AsyncSession, code: str) -> Role | None:
    stmt = select(Role).where(Role.code == code)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def user_role_exists(session: AsyncSession, user_id: str, role_id: str) -> bool:
    stmt = select(UserRole.user_id).where(UserRole.user_id == user_id, UserRole.role_id == role_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None

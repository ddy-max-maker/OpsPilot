"""E2E 测试账号种子（幂等；CI 与本地共用）。

创建 4 个固定账号（admin/operator/approver/viewer），密码统一 E2E_PASSWORD。
仅用于测试/演示库；禁止在生产库执行。

用法：
    DATABASE_URL=... uv run python scripts/seed_e2e_users.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://opspilot:opspilot@127.0.0.1:5432/opspilot_test"
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.models import AssetAccessPolicy, Role, User, UserRole  # noqa: E402

E2E_PASSWORD = "E2ePassw0rd!2026"
ROLES = [
    ("rol_operator", "operator", "运维操作员"),
    ("rol_approver", "approver", "审批人"),
    ("rol_admin", "admin", "管理员"),
    ("rol_viewer", "viewer", "只读查看/审计"),
]
USERS = [
    ("usr_e2e_admin", "e2e_admin", "rol_admin"),
    ("usr_e2e_operator", "e2e_operator", "rol_operator"),
    ("usr_e2e_approver", "e2e_approver", "rol_approver"),
    ("usr_e2e_viewer", "e2e_viewer", "rol_viewer"),
]
# 资产范围授权（asset_id/environment 均空 = 全部资产；scope.py 规则）
PERMISSIONS = {
    "usr_e2e_operator": ["read", "investigate", "execute"],
    "usr_e2e_approver": ["read", "approve", "execute"],
    "usr_e2e_viewer": ["read"],
}


async def main() -> int:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    settings_db = os.environ["DATABASE_URL"]
    engine = create_async_engine(settings_db)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    pwd_hash = hash_password(E2E_PASSWORD)
    async with sf() as session:
        # 角色幂等补种（评估脚本会 TRUNCATE 全表，CI 顺序执行时需自愈）
        for rid, code, name in ROLES:
            existing = (
                await session.execute(select(Role).where(Role.id == rid))
            ).scalar_one_or_none()
            if existing is None:
                session.add(Role(id=rid, code=code, name=name))
        await session.flush()
        for uid, username, role_id in USERS:
            user = (
                await session.execute(select(User).where(User.username == username))
            ).scalar_one_or_none()
            if user is None:
                user = User(
                    id=uid,
                    username=username,
                    email=f"{username}@opspilot.test",
                    password_hash=pwd_hash,
                    display_name=username,
                    is_active=True,
                    must_change_password=False,
                )
                session.add(user)
            else:
                user.password_hash = pwd_hash
                uid = user.id
            role = (
                await session.execute(select(Role).where(Role.id == role_id))
            ).scalar_one_or_none()
            assert role is not None, f"role {role_id} missing (run migrations first)"
            existing_ur = (
                await session.execute(
                    select(UserRole).where(UserRole.user_id == uid, UserRole.role_id == role_id)
                )
            ).scalar_one_or_none()
            if existing_ur is None:
                session.add(UserRole(user_id=uid, role_id=role_id))
            # 资产范围授权（全资产；幂等）
            for perm in PERMISSIONS.get(uid, []):
                pol_id = f"pol_e2e_{username}_{perm}"
                existing_pol = (
                    await session.execute(
                        select(AssetAccessPolicy).where(AssetAccessPolicy.id == pol_id)
                    )
                ).scalar_one_or_none()
                if existing_pol is None:
                    session.add(
                        AssetAccessPolicy(
                            id=pol_id, user_id=uid, permission=perm,
                            asset_id=None, environment=None,
                        )
                    )
        await session.commit()
    await engine.dispose()
    print(f"e2e users ready: {[u[1] for u in USERS]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

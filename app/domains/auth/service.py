"""认证服务：登录、修改密码、Admin 引导（幂等 bootstrap）。

安全语义：
- 登录失败不区分"用户不存在/密码错误/账号禁用"，统一 401（不泄漏存在性）；
- 系统账号 usr_system_alerts 永远拒绝登录；
- 引导创建的账号 must_change_password=true，业务接口由依赖层强制 403。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import RoleCode
from app.core.errors import conflict_state, unauthenticated, validation_error
from app.core.ids import new_id
from app.core.security import create_access_token, hash_password, verify_password
from app.domains.audit import service as audit_service
from app.domains.auth import repository
from app.models import Role, User, UserRole
from app.models.user import SYSTEM_USER_ID, SYSTEM_USER_USERNAME

logger = logging.getLogger(__name__)

# 预置角色（DATA_MODEL §3.1）
_SEED_ROLES: list[tuple[str, str]] = [
    (RoleCode.OPERATOR, "运维操作员"),
    (RoleCode.APPROVER, "审批人"),
    (RoleCode.ADMIN, "管理员"),
    (RoleCode.VIEWER, "只读查看/审计"),
]


class LoginResult:
    """登录成功产物。"""

    def __init__(self, user: User, access_token: str, expires_in: int) -> None:
        self.user = user
        self.access_token = access_token
        self.expires_in = expires_in


async def login(
    session: AsyncSession, settings: Settings, username: str, password: str
) -> LoginResult:
    """校验用户名/密码并签发 30 分钟 access token。

    失败一律 ApiError(401)，message 恒定，不泄漏账号存在性。
    """
    user = await repository.get_user_by_username(session, username)
    invalid = unauthenticated("invalid username or password")
    if user is None or user.id == SYSTEM_USER_ID or not user.is_active:
        # 系统账号/禁用账号/不存在账号与密码错误不可区分
        raise invalid
    if not verify_password(user.password_hash, password):
        raise invalid
    token, expires_in, _jti = create_access_token(
        user.id, settings.jwt_secret, settings.access_token_expire_minutes
    )
    await audit_service.record(
        session,
        actor_type="user",
        actor_id=user.id,
        action="auth.login",
        resource_type="user",
        resource_id=user.id,
        detail={"username": user.username},
        commit=True,
    )
    return LoginResult(user=user, access_token=token, expires_in=expires_in)


async def change_password(
    session: AsyncSession, settings: Settings, user: User, old_password: str, new_password: str
) -> tuple[User, str, int]:
    """修改自身密码；成功后清除 must_change_password 并签发新 token（旧 token 失效）。

    事务边界：校验 + 更新 + 审计 + 提交在本函数内完成。
    """
    if not verify_password(user.password_hash, old_password):
        raise validation_error("old password is incorrect")
    if len(new_password) < settings.password_min_length:
        raise validation_error(
            f"new password must be at least {settings.password_min_length} characters"
        )
    if verify_password(user.password_hash, new_password):
        raise validation_error("new password must differ from the old password")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    # D6：JWT 服务端失效——改密时绑定新 token 的 jti；旧 token（jti 不匹配）即刻作废。
    new_password_hash = hash_password(new_password)
    user.password_hash = new_password_hash
    access_token, expires_in, new_jti = create_access_token(
        user.id, settings.jwt_secret, settings.access_token_expire_minutes
    )
    user.token_jti = new_jti
    session.add(user)
    await audit_service.record(
        session,
        actor_type="user",
        actor_id=user.id,
        action="auth.change_password",
        resource_type="user",
        resource_id=user.id,
    )
    try:
        await session.commit()
    except IntegrityError as exc:  # pragma: no cover - 防御路径
        await session.rollback()
        raise conflict_state("password change failed due to state conflict") from exc
    await session.refresh(user)
    return user, access_token, expires_in


async def ensure_roles(session: AsyncSession) -> None:
    """幂等播种四个预置角色。"""
    for code, name in _SEED_ROLES:
        if await repository.get_role_by_code(session, code) is None:
            session.add(Role(id=f"rol_{code}", code=code, name=name))


async def ensure_system_account(session: AsyncSession) -> None:
    """幂等预置系统账号 usr_system_alerts（Webhook 事件创建主体，禁止登录）。"""
    if await repository.get_user_by_id(session, SYSTEM_USER_ID) is None:
        session.add(
            User(
                id=SYSTEM_USER_ID,
                username=SYSTEM_USER_USERNAME,
                email="system-alerts@opspilot.internal",
                password_hash="!",  # 非法占位哈希：任何密码校验恒失败
                display_name="系统告警账号",
                is_active=False,  # 禁止登录
                must_change_password=False,
            )
        )


async def ensure_bootstrap_admin(session: AsyncSession, settings: Settings) -> None:
    """按环境变量引导初始 Admin（不硬编码密码；幂等）。

      - 三个变量必须同时提供（部分提供视为配置错误，启动失败）；
    - 已存在同用户名用户则跳过（重启安全）。
    """
    if not settings.bootstrap_admin_configured:
        logger.warning("BOOTSTRAP_ADMIN_* 未配置：跳过初始 Admin 引导（仅播种角色/系统账号）")
        return
    username = settings.bootstrap_admin_username
    email = settings.bootstrap_admin_email
    password = settings.bootstrap_admin_password
    assert username is not None and email is not None and password is not None
    existing = await repository.get_user_by_username(session, username)
    if existing is not None:
        logger.info("bootstrap admin %r 已存在，跳过创建", username)
        return
    admin_role = await repository.get_role_by_code(session, RoleCode.ADMIN)
    if admin_role is None:  # pragma: no cover - ensure_roles 先行保证
        msg = "roles not seeded before bootstrap admin"
        raise RuntimeError(msg)
    user = User(
        id=new_id("usr_"),
        username=username,
        email=email,
        password_hash=hash_password(password),
        display_name="Initial Admin",
        is_active=True,
        must_change_password=True,  # 首登强制改密（D0 评审确认）
    )
    session.add(user)
    session.add(UserRole(user_id=user.id, role_id=admin_role.id))
    logger.info("bootstrap admin %r created (must_change_password=True)", username)


async def bootstrap(session: AsyncSession, settings: Settings) -> None:
    """应用启动引导：角色 + 系统账号 + 初始 Admin，单事务提交。"""
    await ensure_roles(session)
    await ensure_system_account(session)
    await ensure_bootstrap_admin(session, settings)
    await session.commit()


async def assign_role(session: AsyncSession, user_id: str, role_code: str) -> None:
    """给用户挂角色（不提交；由调用方控制事务边界）。"""
    role = await repository.get_role_by_code(session, role_code)
    if role is None:
        msg = f"role {role_code!r} not found (bootstrap not run?)"
        raise RuntimeError(msg)
    if not await repository.user_role_exists(session, user_id, role.id):
        session.add(UserRole(user_id=user_id, role_id=role.id))


async def user_role_codes(session: AsyncSession, user_id: str) -> list[str]:
    stmt = (
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    result = await session.execute(stmt)
    return [row[0] for row in result.all()]

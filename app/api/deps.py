"""API 依赖注入：Session、当前用户（JWT）、RBAC、首登改密拦截。

依赖链：get_db -> get_current_user(认证) -> get_ready_user(改密拦截)
        -> require_roles(RBAC)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.errors import forbidden, unauthenticated
from app.core.security import TokenValidationError, decode_access_token
from app.domains.access import service as access_service
from app.domains.access.scope import AssetScope
from app.domains.auth import repository
from app.domains.auth import service as auth_service
from app.domains.auth.schemas import AssetScopeData
from app.models import User


@dataclass(frozen=True)
class CurrentUser:
    """当前请求的用户上下文（含角色 code）。"""

    user: User
    role_codes: list[str]


SessionDep = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_current_user(
    request: Request, session: SessionDep, settings: SettingsDep
) -> CurrentUser:
    """解析 Bearer JWT 并加载用户（含 is_active 实时校验）。

    任何失败（缺 Token/非法/过期/用户不存在或禁用）统一 401，不泄漏原因。
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise unauthenticated("missing bearer token")
    token = auth_header.removeprefix("Bearer ").strip()
    try:
        payload = decode_access_token(token, settings.jwt_secret)
    except TokenValidationError as exc:
        raise unauthenticated("invalid or expired token") from exc
    user = await repository.get_user_by_id(session, str(payload.get("sub", "")))
    if user is None or not user.is_active:
        raise unauthenticated("invalid or expired token")
    role_codes = await auth_service.user_role_codes(session, user.id)
    return CurrentUser(user=user, role_codes=role_codes)


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


async def get_ready_user(current: CurrentUserDep) -> CurrentUser:
    """业务接口前置：引导账号未改密时拦截（403 PASSWORD_CHANGE_REQUIRED）。

    /auth/me 与 /auth/change-password 不受此拦截（客户端需要看到状态并完成改密）。
    """
    if current.user.must_change_password:
        from app.core.errors import password_change_required

        raise password_change_required()
    return current


ReadyUserDep = Annotated[CurrentUser, Depends(get_ready_user)]


def require_roles(*required: str) -> Any:
    """RBAC 角色依赖工厂：当前用户角色不含任一要求角色 -> 403 FORBIDDEN。"""

    async def _checker(current: ReadyUserDep) -> CurrentUser:
        if not set(current.role_codes) & set(required):
            raise forbidden(
                "role not allowed for this operation",
                details={"required_roles": list(required)},
            )
        return current

    return _checker


async def get_asset_scope(session: SessionDep, current: ReadyUserDep) -> AssetScope:
    """加载当前用户的资产范围（read 维度；默认拒绝）。"""
    return await access_service.get_effective_scope(
        session, current.user.id, current.role_codes, permission="read"
    )


AssetScopeDep = Annotated[AssetScope, Depends(get_asset_scope)]


def get_asset_scope_for(permission: str) -> Any:
    """按权限维度解析资产范围的依赖工厂（PolicyPermission：read/investigate/...）。

    D1 语义（CONTRACTS §7.1）：查看事件 = read；创建/确认事件 = investigate。
    """

    async def _resolver(session: SessionDep, current: ReadyUserDep) -> AssetScope:
        return await access_service.get_effective_scope(
            session, current.user.id, current.role_codes, permission=permission
        )

    return _resolver


InvestigateScopeDep = Annotated[AssetScope, Depends(get_asset_scope_for("investigate"))]

ReadScopeDep = Annotated[AssetScope, Depends(get_asset_scope_for("read"))]


def scope_summary(scope: AssetScope) -> AssetScopeData:
    """GET /auth/me 的资产范围摘要。"""
    from app.core.enums import Environment

    environments: list[str]
    if scope.is_admin or scope.all_assets:
        environments = [e.value for e in Environment]
        all_assets = True
    else:
        environments = sorted(scope.environments)
        all_assets = False
    return AssetScopeData(
        is_admin=scope.is_admin,
        all_assets=all_assets,
        environments=environments,
        asset_ids=sorted(scope.asset_ids),
    )


def page_limit(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> int:
    """分页 limit 参数（契约：默认 50，上限 200）。"""
    return limit


PageLimitDep = Annotated[int, Depends(page_limit)]

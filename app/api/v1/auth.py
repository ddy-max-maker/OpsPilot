"""认证 API（CONTRACTS §5 / §8）：

- POST /auth/login           登录签发 30 分钟 access token
- GET  /auth/me              当前用户、角色、资产范围摘要
- POST /auth/change-password 修改自身密码（首登强制）
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import CurrentUserDep, SessionDep, SettingsDep, scope_summary
from app.core.responses import Envelope, ok
from app.domains.access import service as access_service
from app.domains.auth import service as auth_service
from app.domains.auth.schemas import (
    ChangePasswordData,
    ChangePasswordRequest,
    LoginRequest,
    MeData,
    TokenData,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=Envelope[TokenData])
async def login(
    body: LoginRequest, session: SessionDep, settings: SettingsDep
) -> Envelope[TokenData]:
    """登录。失败统一 401，不区分用户不存在/密码错误/账号禁用。"""
    result = await auth_service.login(session, settings, body.username, body.password)
    return ok(
        TokenData(
            access_token=result.access_token,
            expires_in=result.expires_in,
            user_id=result.user.id,
            username=result.user.username,
            must_change_password=result.user.must_change_password,
        )
    )


@router.get("/me", response_model=Envelope[MeData])
async def me(
    session: SessionDep,
    current: CurrentUserDep,
) -> Envelope[MeData]:
    """当前用户信息。must_change_password=true 亦可访问（客户端据此引导改密）。"""
    scope = await access_service.get_effective_scope(
        session, current.user.id, current.role_codes, permission="read"
    )
    return ok(
        MeData(
            id=current.user.id,
            username=current.user.username,
            email=current.user.email,
            display_name=current.user.display_name,
            is_active=current.user.is_active,
            must_change_password=current.user.must_change_password,
            roles=sorted(current.role_codes),
            asset_scope=scope_summary(scope),
        )
    )


@router.post(
    "/change-password",
    response_model=Envelope[ChangePasswordData],
    status_code=status.HTTP_200_OK,
)
async def change_password(
    body: ChangePasswordRequest,
    session: SessionDep,
    settings: SettingsDep,
    current: CurrentUserDep,
) -> Envelope[ChangePasswordData]:
    """修改自身密码：校验旧密码，成功后清除 must_change_password。"""
    await auth_service.change_password(
        session,
        settings,
        current.user,
        body.old_password,
        body.new_password,
    )
    return ok(ChangePasswordData())

"""认证领域 Schema（Pydantic v2）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domains.access.schemas import AssetScopeData


class LoginRequest(BaseModel):
    """登录请求。"""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class TokenData(BaseModel):
    """登录成功响应（契约：{access_token, token_type, expires_in}）。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int  # 秒（30 分钟 = 1800）
    user_id: str
    username: str
    must_change_password: bool


class ChangePasswordRequest(BaseModel):
    """修改自身密码（引导账号首登强制调用）。"""

    model_config = ConfigDict(extra="forbid")

    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class ChangePasswordData(BaseModel):
    status: str = "ok"


class MeData(BaseModel):
    """GET /auth/me：当前用户、角色、资产范围摘要。"""

    id: str
    username: str
    email: str
    display_name: str | None
    is_active: bool
    must_change_password: bool
    roles: list[str]
    asset_scope: AssetScopeData

"""用户与权限表（DATA_MODEL §3.1）：users / roles / user_roles / asset_access_policies。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# 预置系统账号（Webhook 事件创建主体；bootstrap 创建、禁止登录，CONTRACTS §2.3）
SYSTEM_USER_ID = "usr_system_alerts"
SYSTEM_USER_USERNAME = "system_alerts"


class User(Base, TimestampMixin):
    """平台用户。"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # D6：JWT 服务端失效——改密时更新 token_jti，仅匹配 jti 的 token 有效
    token_jti: Mapped[str | None] = mapped_column(String, nullable=True)


class Role(Base):
    """角色（operator / approver / admin / viewer）。"""

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserRole(Base):
    """用户-角色关联。"""

    __tablename__ = "user_roles"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, nullable=False
    )
    role_id: Mapped[str] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True, nullable=False
    )


class AssetAccessPolicy(Base):
    """资产范围策略：user_id 与 role_id 互斥（CHECK），默认拒绝由判定函数实现。"""

    __tablename__ = "asset_access_policies"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL)::int + (role_id IS NOT NULL)::int = 1",
            name="subject_exclusive",
        ),
        CheckConstraint(
            "environment IN ('sandbox','staging','production') OR environment IS NULL",
            name="environment_valid",
        ),
        CheckConstraint(
            "permission IN ('read','investigate','approve','execute')",
            name="permission_valid",
        ),
        Index("ix_asset_access_policies_user_id", "user_id"),
        Index("ix_asset_access_policies_role_id", "role_id"),
        Index("ix_asset_access_policies_asset_id", "asset_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    role_id: Mapped[str | None] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), nullable=True
    )
    asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=True
    )
    environment: Mapped[str | None] = mapped_column(Text, nullable=True)
    permission: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

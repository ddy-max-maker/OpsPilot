"""连接器配置表族（DATA_MODEL §3.5，D2 最小集）。

connector_instances 即 REST 资源 integrations 的存储实体（CONTRACTS §2.8）。
Docker 实例的 endpoint 必须指向 docker-ro-proxy 白名单端点（ADR-11）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ConnectorType(Base):
    __tablename__ = "connector_types"
    __table_args__ = (UniqueConstraint("code", name="uq_connector_types_code"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ConnectorInstance(Base):
    __tablename__ = "connector_instances"
    __table_args__ = (
        UniqueConstraint("name", name="uq_connector_instances_name"),
        CheckConstraint("status IN ('enabled','disabled')", name="status_valid"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    connector_type_id: Mapped[str] = mapped_column(ForeignKey("connector_types.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # 非敏感配置（如 docker-ro-proxy base_url、redis dsn 主机名）；密钥只存 secret_ref 引用
    config: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    secret_ref_ids: Mapped[list[object] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="enabled")
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SecretRef(Base):
    """密钥引用：只存环境变量名，不存密文（PRD §8）。"""

    __tablename__ = "secret_refs"
    __table_args__ = (UniqueConstraint("alias", name="uq_secret_refs_alias"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="env")
    ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

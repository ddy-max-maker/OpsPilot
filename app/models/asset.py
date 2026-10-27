"""资产与拓扑表（DATA_MODEL §3.2）：assets / asset_edges。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Asset(Base, TimestampMixin):
    """受管资产（服务/容器/数据库/缓存/网关/主机）。UNIQUE(name, environment)。"""

    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("name", "environment", name="uq_assets_name_environment"),
        CheckConstraint(
            "environment IN ('sandbox','staging','production')", name="environment_valid"
        ),
        CheckConstraint(
            "asset_type IN ('service','container','database','cache','gateway','host')",
            name="asset_type_valid",
        ),
        CheckConstraint("status IN ('active','retired')", name="status_valid"),
        CheckConstraint("criticality IN ('low','medium','high')", name="criticality_valid"),
        # environment/status 检索索引（DATA_MODEL）
        Index("ix_assets_environment_status", "environment", "status"),
        # cursor 分页排序键索引（CONTRACTS §1.3：排序键 created_at,id）
        Index("ix_assets_created_at_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    asset_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    criticality: Mapped[str] = mapped_column(Text, nullable=False, default="medium")
    endpoints: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)


class AssetEdge(Base):
    """资产依赖边：UNIQUE(from, to, relation)，禁止自环。"""

    __tablename__ = "asset_edges"
    __table_args__ = (
        UniqueConstraint("from_asset_id", "to_asset_id", "relation", name="uq_asset_edges_edge"),
        CheckConstraint("from_asset_id <> to_asset_id", name="no_self_loop"),
        CheckConstraint("relation IN ('depends_on','runs_on','serves')", name="relation_valid"),
        Index("ix_asset_edges_to_asset_id", "to_asset_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    from_asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    to_asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    relation: Mapped[str] = mapped_column(Text, nullable=False)

"""业务事故表（DATA_MODEL §3.3）：incidents。

D1-A 仅建表；Incident 业务接口（创建/确认/状态机）属于 D1-B 范围。
告警→事件的创建顺序约束见 DATA_MODEL §3.3（本阶段不实现 Webhook）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# 活跃单去重：局部唯一索引（RESOLVED/CLOSED 后同指纹可再建单）
_ACTIVE_INCIDENT_STATUSES = "'open','acknowledged','investigating','mitigating','monitoring'"


class Incident(Base, TimestampMixin):
    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(
            "environment IN ('sandbox','staging','production')", name="environment_valid"
        ),
        CheckConstraint("source IN ('manual','alert')", name="source_valid"),
        CheckConstraint("severity IN ('low','medium','high','critical')", name="severity_valid"),
        CheckConstraint(
            "status IN ('open','acknowledged','investigating','mitigating','monitoring',"
            "'resolved','closed','needs_human')",
            name="status_valid",
        ),
        Index("ix_incidents_environment_status", "environment", "status"),
        Index("ix_incidents_primary_asset_id_status", "primary_asset_id", "status"),
        Index("ix_incidents_created_at", "created_at"),
        Index(
            "uq_incidents_active_fingerprint",
            "primary_asset_id",
            "fingerprint",
            unique=True,
            postgresql_where=text(f"status IN ({_ACTIVE_INCIDENT_STATUSES})"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    primary_asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    # alert_id 为逻辑引用（不加物理外键，避免与 alerts.incident_id 循环依赖）
    alert_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

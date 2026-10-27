"""告警表（DATA_MODEL §3.3）：alerts。

D1-A 仅建表；Webhook（HMAC 签名 + delivery_id 去重）属于 D1-B 范围。
唯一物理外键方向 alerts.incident_id -> incidents（DATA_MODEL）。
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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("connector_id", "external_event_id", name="uq_alerts_delivery"),
        CheckConstraint("severity IN ('low','medium','high','critical')", name="severity_valid"),
        CheckConstraint("status IN ('received','mapped','ignored')", name="status_valid"),
        Index("ix_alerts_fingerprint", "fingerprint"),
        Index("ix_alerts_incident_id", "incident_id"),
        Index("ix_alerts_received_at", "received_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    connector_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    incident_id: Mapped[str | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="SET NULL"), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

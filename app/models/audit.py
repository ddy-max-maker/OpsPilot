"""审计事件表（DATA_MODEL / CONTRACTS §2.15）：只追加，无 UPDATE/DELETE 路径。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedOnlyMixin


class AuditEvent(Base, CreatedOnlyMixin):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('user','agent','system','action_service')",
            name="actor_type_valid",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(BigInteger, "postgresql"),
        primary_key=True,
        autoincrement=True,
    )
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String, nullable=True)
    environment: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    request_id: Mapped[str] = mapped_column(String, nullable=False)

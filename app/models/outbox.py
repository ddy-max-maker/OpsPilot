"""异步基础设施表（DATA_MODEL §3.8）：api_idempotency_keys / outbox_events。

D1 仅建表：api_idempotency_keys 随 D1-B 事件接口启用；
outbox_events 建表不启用（D5 才由 dispatcher 消费推送 RabbitMQ）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedOnlyMixin


class ApiIdempotencyKey(Base, CreatedOnlyMixin):
    """API 幂等键账本：PK(user_id, endpoint, key) 按调用者+端点+键隔离（D0 评审确认）。"""

    __tablename__ = "api_idempotency_keys"
    __table_args__ = (
        CheckConstraint("state IN ('received','completed')", name="state_valid"),
        Index("ix_api_idempotency_keys_expires_at", "expires_at"),
    )

    key: Mapped[str] = mapped_column(String, primary_key=True)
    endpoint: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxEvent(Base, CreatedOnlyMixin):
    """Outbox 事件表：与业务事务同库同事务写入；D1 建表，D5 启用消费。"""

    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("status IN ('pending','dispatched','failed')", name="status_valid"),
        Index("ix_outbox_events_status_next_attempt_at", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

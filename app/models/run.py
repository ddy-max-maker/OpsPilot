"""诊断运行表族（DATA_MODEL §3.4）：investigation_runs + run_events。

run 业务状态以 DB 为权威（CONTRACTS §8）；langgraph_thread_id = run_id（ADR-5）。
claim_expires_at/lease_owner/fence_token 为 DB 认领租约字段（ADR-12），
cancel_requested 为合作式取消标志（ARCHITECTURE §5.1）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedOnlyMixin, TimestampMixin


class InvestigationRun(Base, TimestampMixin):
    __tablename__ = "investigation_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created','queued','running','completed','waiting_approval',"
            "'needs_human','failed','canceled')",
            name="status_valid",
        ),
        CheckConstraint(
            "stop_reason IS NULL OR stop_reason IN ('completed','budget_exhausted',"
            "'no_new_evidence','unknown_exit','canceled','error')",
            name="stop_reason_valid",
        ),
        Index("ix_runs_status_created_at", "status", "created_at"),
        Index("ix_runs_incident_id_created_at", "incident_id", "created_at"),
        UniqueConstraint("langgraph_thread_id", name="uq_runs_thread_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    model_config_json: Mapped[dict[str, object]] = mapped_column(
        "model_config", JSONB, nullable=False
    )
    budgets: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    usage: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    authorization_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    langgraph_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 取消请求标志：API 置位，Worker 在步进循环与工具调用前轮询（合作式取消）
    cancel_requested: Mapped[bool] = mapped_column(
        "cancel_requested", nullable=False, server_default=text("false")
    )
    # DB 任务认领（ADR-4/ADR-12）
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    fence_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunEvent(Base, CreatedOnlyMixin):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_run_events_run_seq"),
        Index("ix_run_events_run_id_id", "run_id", "id"),
        CheckConstraint(
            "type IN ('run_started','node_entered','node_exited','tool_called',"
            "'tool_returned','evidence_added','hypothesis_added','budget_warning',"
            "'error','run_finished')",
            name="type_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("investigation_runs.id"), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)

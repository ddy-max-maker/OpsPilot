"""统一 Evidence / 假设 / 诊断报告快照（DATA_MODEL §3.4，CONTRACTS §3/§2.7/§2.16）。

三张表均只追加（CONTRACTS：Evidence 不可变；报告更正以新版本追加）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedOnlyMixin


class Evidence(Base, CreatedOnlyMixin):
    """统一 Evidence（落库后不可更新；失败不伪装为空数据）。"""

    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('metric','log','trace','health','infra','knowledge')",
            name="source_type_valid",
        ),
        CheckConstraint("status IN ('ok','error','timeout','unavailable')", name="status_valid"),
        Index("ix_evidence_run_id_source_type", "run_id", "source_type"),
        Index("ix_evidence_asset_id_observed_at", "asset_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("investigation_runs.id"), nullable=True)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    collector: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    time_range: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    data_ref: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    summary_redacted: Mapped[str] = mapped_column(Text, nullable=False)
    correlation: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    truncated: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    schema_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="1")


class Hypothesis(Base, CreatedOnlyMixin):
    """候选根因假设（supported 至少 1 条实时证据；knowledge 不得直接支撑）。"""

    __tablename__ = "hypotheses"
    __table_args__ = (
        CheckConstraint(
            "status IN ('candidate','supported','refuted','insufficient_data')",
            name="status_valid",
        ),
        CheckConstraint("confidence IN ('low','medium','high')", name="confidence_valid"),
        Index("ix_hypotheses_run_id_rank", "run_id", "rank"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("investigation_runs.id"), nullable=False)
    rank: Mapped[int] = mapped_column(nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_evidence_ids: Mapped[list[object] | None] = mapped_column(JSONB, nullable=True)
    contradicting_evidence_ids: Mapped[list[object] | None] = mapped_column(JSONB, nullable=True)
    missing_information: Mapped[list[object] | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[str] = mapped_column(Text, nullable=False)


class DiagnosticReport(Base, CreatedOnlyMixin):
    """诊断报告快照（ADR-13）：GET /runs/{id}/report 返回最新版本。"""

    __tablename__ = "diagnostic_reports"
    __table_args__ = (
        UniqueConstraint("run_id", "version_no", name="uq_reports_run_version"),
        CheckConstraint("status IN ('final','amended')", name="status_valid"),
        Index("ix_reports_run_id", "run_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("investigation_runs.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    conclusion: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    hypothesis_ids: Mapped[list[object] | None] = mapped_column(JSONB, nullable=True)
    evidence_refs: Mapped[list[object] | None] = mapped_column(JSONB, nullable=True)
    unknown_note: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    generator: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

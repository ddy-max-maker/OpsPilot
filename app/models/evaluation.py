"""评估体系表（DATA_MODEL §3.9，D6）：suites / cases / runs / results。

可复现锚点（PRD §13）：evaluation_runs.config 记录 git commit、数据集版本、
model provider/版本、prompt 版本、RAG 模式与 embedding 版本、预算与随机性；
真实模型未返回的成本字段标记 unavailable（不编造）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
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

from app.models.base import Base, CreatedOnlyMixin


class EvaluationSuite(Base, CreatedOnlyMixin):
    """评估套件（fault / security / rag_ablation / reliability）。"""

    __tablename__ = "evaluation_suites"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('fault','security','rag_ablation','reliability')", name="kind_valid"
        ),
        UniqueConstraint("name", name="uq_evaluation_suites_name"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)


class EvaluationCase(Base, CreatedOnlyMixin):
    """评估案例：scenario（注入/输入/授权范围/副作用允许）+ expected（可判定预期）。"""

    __tablename__ = "evaluation_cases"
    __table_args__ = (
        Index("ix_evaluation_cases_suite_id", "suite_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    suite_id: Mapped[str] = mapped_column(ForeignKey("evaluation_suites.id"), nullable=False)
    case_key: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    scenario: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    expected: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    holdout: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class EvaluationRun(Base, CreatedOnlyMixin):
    """评估运行：config 为可复现锚点快照；status ∈ pending/running/completed/failed。"""

    __tablename__ = "evaluation_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed')", name="status_valid"
        ),
        Index("ix_evaluation_runs_suite_id", "suite_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    suite_id: Mapped[str] = mapped_column(ForeignKey("evaluation_suites.id"), nullable=False)
    config: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class EvaluationResult(Base):
    """单案例结果：metrics（可计算指标 JSON）+ passed + detail（失败案例原始基线）。"""

    __tablename__ = "evaluation_results"
    __table_args__ = (
        Index("ix_evaluation_results_run_id", "evaluation_run_id"),
        UniqueConstraint("evaluation_run_id", "case_id", name="uq_eval_result_run_case"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    evaluation_run_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_runs.id"), nullable=False
    )
    case_id: Mapped[str] = mapped_column(ForeignKey("evaluation_cases.id"), nullable=False)
    incident_id: Mapped[str | None] = mapped_column(ForeignKey("incidents.id"), nullable=True)
    metrics: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class EvaluationCaseKind:
    """套件类型常量（PRD §13 验证矩阵分组）。"""

    FAULT = "fault"
    SECURITY = "security"
    RAG_ABLATION = "rag_ablation"
    RELIABILITY = "reliability"

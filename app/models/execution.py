"""审批与受控执行表（DATA_MODEL §3.7，D5）：proposals / approvals /
action_executions / action_idempotency / verification_results。

状态机权威定义见 CONTRACTS §2.11；本模块同时提供 ProposalStatus /
ActionExecutionStatus / IdempotencyState / VerificationStatus 与合法转换表
（服务层 require_transition 强制）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedOnlyMixin, TimestampMixin


class Proposal(Base, TimestampMixin):
    """处置提案：production 由数据库 CHECK 硬排除；r2/r3 首版不存在。"""

    __tablename__ = "proposals"
    __table_args__ = (
        CheckConstraint("environment IN ('sandbox','staging')", name="env_no_production"),
        CheckConstraint("risk_level IN ('r0','r1')", name="risk_valid"),
        CheckConstraint(
            "status IN ('draft','pending_approval','approved','rejected','expired',"
            "'executing','executed','execution_failed','execution_unknown',"
            "'verified','verify_failed','verify_unknown','canceled')",
            name="status_valid",
        ),
        Index("ix_proposals_run_id", "run_id"),
        Index("ix_proposals_incident_id", "incident_id"),
        Index("ix_proposals_status", "status"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("investigation_runs.id"), nullable=False)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), nullable=False)
    runbook_version_id: Mapped[str] = mapped_column(
        ForeignKey("runbook_versions.id"), nullable=False
    )
    target_asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    parameters: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    parameter_hash: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(Text, nullable=False)
    preconditions_result: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Approval(Base):
    """审批记录：只追加；四眼原则（decided_by ≠ created_by_user）由服务层强制。"""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint("decision IN ('approved','rejected')", name="decision_valid"),
        Index("ix_approvals_proposal_id", "proposal_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    proposal_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class ActionExecution(Base, CreatedOnlyMixin):
    """动作执行记录：unknown = 外部效果不可观测（禁止盲目重试）。"""

    __tablename__ = "action_executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','pre_check_failed','running','succeeded',"
            "'failed','unknown','skipped')",
            name="status_valid",
        ),
        Index("ix_action_executions_proposal_id", "proposal_id"),
        Index("ix_action_executions_status", "status"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), nullable=False)
    action_key: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    executor: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_detail: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ActionIdempotency(Base):
    """幂等账本：执行前抢占（claimed）→ 回写 completed/unknown；PK 即幂等键。"""

    __tablename__ = "action_idempotency"
    __table_args__ = (
        CheckConstraint("state IN ('claimed','completed','unknown')", name="state_valid"),
    )

    idempotency_key: Mapped[str] = mapped_column(Text, primary_key=True)
    action_execution_id: Mapped[str] = mapped_column(
        ForeignKey("action_executions.id"), nullable=False
    )
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'claimed'"))
    external_evidence: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VerificationResult(Base, CreatedOnlyMixin):
    """恢复验证结果：PASSED 才允许进入 RESOLVED 状态路径（S9 红线）。"""

    __tablename__ = "verification_results"
    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress','passed','failed','unknown')", name="status_valid"
        ),
        Index("ix_verification_results_execution_id", "execution_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("action_executions.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'in_progress'"))
    checks: Mapped[list[object] | None] = mapped_column(JSONB, nullable=True)
    observation_window: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    concluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProposalStatus:
    """Proposal 状态机（CONTRACTS §2.11 冻结）。"""

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTING = "executing"
    EXECUTED = "executed"
    EXECUTION_FAILED = "execution_failed"
    EXECUTION_UNKNOWN = "execution_unknown"
    VERIFIED = "verified"
    VERIFY_FAILED = "verify_failed"
    VERIFY_UNKNOWN = "verify_unknown"
    CANCELED = "canceled"

    TRANSITIONS: dict[str, frozenset[str]] = {
        DRAFT: frozenset({PENDING_APPROVAL, CANCELED}),
        PENDING_APPROVAL: frozenset({APPROVED, REJECTED, EXPIRED, CANCELED}),
        APPROVED: frozenset({EXECUTING, EXPIRED, CANCELED}),
        REJECTED: frozenset(),
        EXPIRED: frozenset(),
        EXECUTING: frozenset({EXECUTED, EXECUTION_FAILED, EXECUTION_UNKNOWN}),
        EXECUTED: frozenset({VERIFIED, VERIFY_FAILED, VERIFY_UNKNOWN}),
        EXECUTION_FAILED: frozenset(),
        EXECUTION_UNKNOWN: frozenset(),
        VERIFIED: frozenset(),
        VERIFY_FAILED: frozenset(),
        VERIFY_UNKNOWN: frozenset(),
        CANCELED: frozenset(),
    }


class ActionExecutionStatus:
    """动作执行状态（action_executions.status）。"""

    PENDING = "pending"
    PRE_CHECK_FAILED = "pre_check_failed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


class IdempotencyState:
    """幂等账本状态（action_idempotency.state）。"""

    CLAIMED = "claimed"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class VerificationStatus:
    """验证结果状态（verification_results.status）。"""

    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"

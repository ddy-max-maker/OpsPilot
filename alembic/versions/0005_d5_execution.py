"""D5 审批与受控执行迁移：5 张表（DATA_MODEL §3.7，CONTRACTS §2.11）。

- proposals：处置提案（environment CHECK 硬排除 production；risk_level 仅 r0/r1；
  状态机含 draft→…→verified 全部分支）；
- approvals：审批记录（只追加；四眼原则由服务层强制）；
- action_executions：动作执行记录（status 含 unknown——外部效果不可观测时）；
- action_idempotency：幂等账本（执行前 INSERT…ON CONFLICT 抢占；崩溃收敛依据）；
- verification_results：恢复验证结果（观察窗口 + checks 快照）。

Revision ID: 0005_d5_execution
Revises: 0004_d4_knowledge
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_d5_execution"
down_revision: str | None = "0004_d4_knowledge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")

_PROPOSAL_STATUS = (
    "'draft','pending_approval','approved','rejected','expired','executing','executed',"
    "'execution_failed','execution_unknown','verified','verify_failed','verify_unknown','canceled'"
)


def upgrade() -> None:
    # ---- proposals（处置提案；production 硬排除由数据库 CHECK 兜底）----
    op.create_table(
        "proposals",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("investigation_runs.id"), nullable=False),
        sa.Column("incident_id", sa.Text(), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column(
            "runbook_version_id",
            sa.Text(),
            sa.ForeignKey("runbook_versions.id"),
            nullable=False,
        ),
        sa.Column("target_asset_id", sa.Text(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column(
            "environment",
            sa.Text(),
            sa.CheckConstraint("environment IN ('sandbox','staging')", name="env_no_production"),
            nullable=False,
        ),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("parameter_hash", sa.Text(), nullable=False),
        sa.Column(
            "risk_level",
            sa.Text(),
            sa.CheckConstraint("risk_level IN ('r0','r1')", name="risk_valid"),
            nullable=False,
        ),
        sa.Column("preconditions_result", postgresql.JSONB(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            sa.CheckConstraint(f"status IN ({_PROPOSAL_STATUS})", name="status_valid"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_by_user", sa.Text(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_proposals"),
    )
    op.create_index("ix_proposals_run_id", "proposals", ["run_id"])
    op.create_index("ix_proposals_incident_id", "proposals", ["incident_id"])
    op.create_index("ix_proposals_status", "proposals", ["status"])

    # ---- approvals（只追加；服务层强制四眼 decided_by ≠ created_by_user）----
    op.create_table(
        "approvals",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), sa.ForeignKey("proposals.id"), nullable=False),
        sa.Column(
            "decision",
            sa.Text(),
            sa.CheckConstraint("decision IN ('approved','rejected')", name="decision_valid"),
            nullable=False,
        ),
        sa.Column("decided_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("proposal_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
    )
    op.create_index("ix_approvals_proposal_id", "approvals", ["proposal_id"])

    # ---- action_executions（动作执行；unknown = 外部效果不可观测）----
    op.create_table(
        "action_executions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.Text(), sa.ForeignKey("proposals.id"), nullable=False),
        sa.Column("action_key", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            sa.CheckConstraint(
                "status IN ('pending','pre_check_failed','running','succeeded',"
                "'failed','unknown','skipped')",
                name="status_valid",
            ),
            nullable=False,
        ),
        sa.Column("executor", sa.Text(), nullable=True),
        sa.Column("external_ref", sa.Text(), nullable=True),
        sa.Column("result_detail", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_action_executions"),
    )
    op.create_index("ix_action_executions_proposal_id", "action_executions", ["proposal_id"])
    op.create_index("ix_action_executions_status", "action_executions", ["status"])

    # ---- action_idempotency（幂等账本；默认零重复提交，UNKNOWN 禁止盲目重试）----
    op.create_table(
        "action_idempotency",
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "action_execution_id",
            sa.Text(),
            sa.ForeignKey("action_executions.id"),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Text(),
            sa.CheckConstraint("state IN ('claimed','completed','unknown')", name="state_valid"),
            nullable=False,
        ),
        sa.Column("external_evidence", postgresql.JSONB(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("idempotency_key", name="pk_action_idempotency"),
    )

    # ---- verification_results（恢复验证；PASSED 才允许 RESOLVED 路径）----
    op.create_table(
        "verification_results",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("execution_id", sa.Text(), sa.ForeignKey("action_executions.id"), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            sa.CheckConstraint(
                "status IN ('in_progress','passed','failed','unknown')", name="status_valid"
            ),
            nullable=False,
        ),
        sa.Column("checks", postgresql.JSONB(), nullable=True),
        sa.Column("observation_window", postgresql.JSONB(), nullable=True),
        sa.Column("concluded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_verification_results"),
    )
    op.create_index(
        "ix_verification_results_execution_id", "verification_results", ["execution_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_verification_results_execution_id", table_name="verification_results")
    op.drop_table("verification_results")
    op.drop_table("action_idempotency")
    op.drop_index("ix_action_executions_status", table_name="action_executions")
    op.drop_index("ix_action_executions_proposal_id", table_name="action_executions")
    op.drop_table("action_executions")
    op.drop_index("ix_approvals_proposal_id", table_name="approvals")
    op.drop_table("approvals")
    op.drop_index("ix_proposals_status", table_name="proposals")
    op.drop_index("ix_proposals_incident_id", table_name="proposals")
    op.drop_index("ix_proposals_run_id", table_name="proposals")
    op.drop_table("proposals")

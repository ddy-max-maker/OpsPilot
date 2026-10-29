"""D6 迁移：评估体系 4 表（DATA_MODEL §3.9）+ 验证记录幂等约束。

- evaluation_suites / evaluation_cases / evaluation_runs / evaluation_results
  （PRD §13 验证矩阵：开发集/保留集、可复现评估、强门禁交叉核验）；
- verification_results.execution_id 唯一约束：同一执行至多一条验证记录
  （D6 加固：重启重入/多消费者不重复写入；D5 数据无重复，直接加约束）。

Revision ID: 0006_d6_evaluation
Revises: 0005_d5_execution
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_d6_evaluation"
down_revision: str | None = "0005_d5_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")


def upgrade() -> None:
    # ---- D6 加固：验证记录幂等（每 execution 至多一条）----
    op.create_unique_constraint(
        "uq_verification_execution", "verification_results", ["execution_id"]
    )

    # ---- D6 加固：JWT 服务端失效（改密后旧 token 不可用；按 jti 精确匹配）----
    op.add_column("users", sa.Column("token_jti", sa.Text(), nullable=True))

    # ---- 评估套件 ----
    op.create_table(
        "evaluation_suites",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "kind",
            sa.Text(),
            sa.CheckConstraint(
                "kind IN ('fault','security','rag_ablation','reliability')",
                name="kind_valid",
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_suites"),
        sa.UniqueConstraint("name", name="uq_evaluation_suites_name"),
    )

    # ---- 评估案例（开发集/保留集；expected 可判定预期）----
    op.create_table(
        "evaluation_cases",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("suite_id", sa.Text(), sa.ForeignKey("evaluation_suites.id"), nullable=False),
        sa.Column("case_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("scenario", postgresql.JSONB(), nullable=False),
        sa.Column("expected", postgresql.JSONB(), nullable=False),
        sa.Column("holdout", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_cases"),
        sa.UniqueConstraint("case_key", name="uq_evaluation_cases_case_key"),
    )
    op.create_index("ix_evaluation_cases_suite_id", "evaluation_cases", ["suite_id"])

    # ---- 评估运行（可复现锚点：commit/模型/prompt/RAG/数据集版本）----
    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("suite_id", sa.Text(), sa.ForeignKey("evaluation_suites.id"), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            sa.CheckConstraint(
                "status IN ('pending','running','completed','failed')", name="status_valid"
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_runs"),
    )
    op.create_index("ix_evaluation_runs_suite_id", "evaluation_runs", ["suite_id"])

    # ---- 评估结果（指标 + 判定；UNIQUE(run, case) 幂等）----
    op.create_table(
        "evaluation_results",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "evaluation_run_id",
            sa.Text(),
            sa.ForeignKey("evaluation_runs.id"),
            nullable=False,
        ),
        sa.Column("case_id", sa.Text(), sa.ForeignKey("evaluation_cases.id"), nullable=False),
        sa.Column("incident_id", sa.Text(), sa.ForeignKey("incidents.id"), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_results"),
        sa.UniqueConstraint("evaluation_run_id", "case_id", name="uq_eval_result_run_case"),
    )
    op.create_index("ix_evaluation_results_run_id", "evaluation_results", ["evaluation_run_id"])


def downgrade() -> None:
    op.drop_index("ix_evaluation_results_run_id", table_name="evaluation_results")
    op.drop_table("evaluation_results")
    op.drop_index("ix_evaluation_runs_suite_id", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_index("ix_evaluation_cases_suite_id", table_name="evaluation_cases")
    op.drop_table("evaluation_cases")
    op.drop_table("evaluation_suites")
    op.drop_column("users", "token_jti")
    op.drop_constraint("uq_verification_execution", "verification_results", type_="unique")

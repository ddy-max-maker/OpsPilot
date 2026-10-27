"""D2 诊断纵切迁移：运行/事件/证据/假设/报告 + 连接器配置 + langgraph schema。

依据 docs/DATA_MODEL.md v1.1 §3.4/§3.5（D2 建表清单）与 ADR-5：
- investigation_runs / run_events / evidence / hypotheses / diagnostic_reports（§3.4）
- connector_types / connector_instances / secret_refs（§3.5，D2 最小集）
- `langgraph` schema（ADR-5：LangGraph checkpoint 独立命名空间，
  由 PostgresSaver 在 Worker 首次启动 setup() 时在 search_path=langgraph 下自建表）
- investigation_runs.cancel_requested 为合作式取消标志（ARCHITECTURE §5.1），
  属 DATA_MODEL 冻结列之外的实现补充列（只增不改）。

Revision ID: 0002_d2_diagnosis
Revises: 0001_d1_foundation
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_d2_diagnosis"
down_revision: str | None = "0001_d1_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")


def upgrade() -> None:
    # ---- ADR-5：LangGraph checkpoint 独立 schema（表由 PostgresSaver.setup() 自建）----
    op.execute("CREATE SCHEMA IF NOT EXISTS langgraph")

    # ---- §3.5 连接器（D2 最小集：先建，runs 表外键不依赖）----
    op.create_table(
        "connector_types",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("read_only", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_connector_types"),
        sa.UniqueConstraint("code", name="uq_connector_types_code"),
    )

    op.create_table(
        "secret_refs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), server_default="env", nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_secret_refs"),
        sa.UniqueConstraint("alias", name="uq_secret_refs_alias"),
    )

    op.create_table(
        "connector_instances",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column(
            "connector_type_id",
            sa.Text(),
            sa.ForeignKey(
                "connector_types.id",
                name="fk_connector_instances_connector_type_id_connector_types",
            ),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=True),
        sa.Column("secret_ref_ids", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), server_default="enabled", nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_connector_instances"),
        sa.UniqueConstraint("name", name="uq_connector_instances_name"),
        sa.CheckConstraint(
            "status IN ('enabled','disabled')", name="ck_connector_instances_status_valid"
        ),
    )

    # ---- §3.4 investigation_runs ----
    op.create_table(
        "investigation_runs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column(
            "incident_id",
            sa.Text(),
            sa.ForeignKey("incidents.id", name="fk_investigation_runs_incident_id_incidents"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "triggered_by",
            sa.Text(),
            sa.ForeignKey("users.id", name="fk_investigation_runs_triggered_by_users"),
            nullable=False,
        ),
        sa.Column("model_config", postgresql.JSONB(), nullable=False),
        sa.Column("budgets", postgresql.JSONB(), nullable=False),
        sa.Column(
            "usage", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("authorization_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("langgraph_thread_id", sa.Text(), nullable=False),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("fence_token", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_runs"),
        sa.UniqueConstraint("langgraph_thread_id", name="uq_runs_thread_id"),
        sa.CheckConstraint(
            "status IN ('created','queued','running','completed','waiting_approval',"
            "'needs_human','failed','canceled')",
            name="ck_investigation_runs_status_valid",
        ),
        sa.CheckConstraint(
            "stop_reason IS NULL OR stop_reason IN ('completed','budget_exhausted',"
            "'no_new_evidence','unknown_exit','canceled','error')",
            name="ck_investigation_runs_stop_reason_valid",
        ),
    )
    op.create_index(
        "ix_investigation_runs_status_created_at",
        "investigation_runs",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_investigation_runs_incident_id_created_at",
        "investigation_runs",
        ["incident_id", "created_at"],
    )

    # ---- §3.4 run_events ----
    op.create_table(
        "run_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("investigation_runs.id", name="fk_run_events_run_id_investigation_runs"),
            nullable=False,
        ),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_run_events"),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_events_run_seq"),
        sa.CheckConstraint(
            "type IN ('run_started','node_entered','node_exited','tool_called',"
            "'tool_returned','evidence_added','hypothesis_added','budget_warning',"
            "'error','run_finished')",
            name="ck_run_events_type_valid",
        ),
    )
    op.create_index("ix_run_events_run_id_id", "run_events", ["run_id", "id"])

    # ---- §3.4 evidence ----
    op.create_table(
        "evidence",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("investigation_runs.id", name="fk_evidence_run_id_investigation_runs"),
            nullable=True,
        ),
        sa.Column(
            "asset_id",
            sa.Text(),
            sa.ForeignKey("assets.id", name="fk_evidence_asset_id_assets"),
            nullable=True,
        ),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("collector", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("time_range", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("data_ref", postgresql.JSONB(), nullable=True),
        sa.Column("summary_redacted", sa.Text(), nullable=False),
        sa.Column("correlation", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("truncated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("schema_version", sa.Text(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_evidence"),
        sa.CheckConstraint(
            "source_type IN ('metric','log','trace','health','infra','knowledge')",
            name="ck_evidence_source_type_valid",
        ),
        sa.CheckConstraint(
            "status IN ('ok','error','timeout','unavailable')",
            name="ck_evidence_status_valid",
        ),
    )
    op.create_index("ix_evidence_run_id_source_type", "evidence", ["run_id", "source_type"])
    op.create_index("ix_evidence_asset_id_observed_at", "evidence", ["asset_id", "observed_at"])

    # ---- §3.4 hypotheses ----
    op.create_table(
        "hypotheses",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("investigation_runs.id", name="fk_hypotheses_run_id_investigation_runs"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("supporting_evidence_ids", postgresql.JSONB(), nullable=True),
        sa.Column("contradicting_evidence_ids", postgresql.JSONB(), nullable=True),
        sa.Column("missing_information", postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_hypotheses"),
        sa.CheckConstraint(
            "status IN ('candidate','supported','refuted','insufficient_data')",
            name="ck_hypotheses_status_valid",
        ),
        sa.CheckConstraint(
            "confidence IN ('low','medium','high')", name="ck_hypotheses_confidence_valid"
        ),
    )
    op.create_index("ix_hypotheses_run_id_rank", "hypotheses", ["run_id", "rank"])

    # ---- §3.4 diagnostic_reports（ADR-13 快照表）----
    op.create_table(
        "diagnostic_reports",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey(
                "investigation_runs.id",
                name="fk_diagnostic_reports_run_id_investigation_runs",
            ),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("conclusion", postgresql.JSONB(), nullable=False),
        sa.Column("hypothesis_ids", postgresql.JSONB(), nullable=True),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=True),
        sa.Column("unknown_note", postgresql.JSONB(), nullable=True),
        sa.Column("generator", postgresql.JSONB(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_diagnostic_reports"),
        sa.UniqueConstraint("run_id", "version_no", name="uq_reports_run_version"),
        sa.CheckConstraint(
            "status IN ('final','amended')", name="ck_diagnostic_reports_status_valid"
        ),
    )
    op.create_index("ix_diagnostic_reports_run_id", "diagnostic_reports", ["run_id"])


def downgrade() -> None:
    op.drop_table("diagnostic_reports")
    op.drop_table("hypotheses")
    op.drop_table("evidence")
    op.drop_table("run_events")
    op.drop_table("investigation_runs")
    op.drop_table("connector_instances")
    op.drop_table("secret_refs")
    op.drop_table("connector_types")
    op.execute("DROP SCHEMA IF EXISTS langgraph CASCADE")

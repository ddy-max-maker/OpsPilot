"""D4 知识库与 RAG 迁移：pgvector + 七张知识表 + 执行快照（DATA_MODEL §3.6）。

- 启用 pgvector 扩展（镜像 pgvector/pgvector:pg16；CI service 同镜像）；
- knowledge_documents / knowledge_versions / knowledge_chunks / knowledge_embeddings
  / retrieval_events / runbooks / runbook_versions（CONTRACTS §2.9/§2.10）；
- investigation_runs.execution_snapshot：实际执行快照（Worker 认领时写入真实
  provider/model/prompt 版本/RAG 模式，D4 安全修复③：不能只依赖创建时配置）。

向量维度按部署的 Embedding 模型在迁移时固化为 384（intfloat/multilingual-e5-small）；
换模型 = knowledge_versions 新增 embedding_model_version 行 + 检索过滤，不覆盖旧向量。

Revision ID: 0004_d4_knowledge
Revises: 0003_d3_registry
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_d4_knowledge"
down_revision: str | None = "0003_d3_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")


def upgrade() -> None:
    # pgvector（官方镜像内置扩展文件；opspilot 用户为超级用户可创建）
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ---- D4 安全修复③：实际执行快照（可审计）----
    op.add_column(
        "investigation_runs",
        sa.Column("execution_snapshot", postgresql.JSONB(), nullable=True),
    )

    # ---- 知识文档 / 版本（CONTRACTS §2.9）----
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column(
            "doc_type",
            sa.Text(),
            sa.CheckConstraint(
                "doc_type IN ('runbook','postmortem','guide')", name="doc_type_valid"
            ),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_incident_id", sa.Text(), sa.ForeignKey("incidents.id"), nullable=True),
        sa.Column("owner", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tags", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_documents"),
    )
    op.create_index("ix_knowledge_documents_doc_type", "knowledge_documents", ["doc_type"])

    op.create_table(
        "knowledge_versions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column(
            "document_id",
            sa.Text(),
            sa.ForeignKey("knowledge_documents.id"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            sa.CheckConstraint(
                "status IN ('draft','reviewed','published','superseded','retired')",
                name="status_valid",
            ),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reviewed_by", sa.Text(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "sensitivity",
            sa.Text(),
            sa.CheckConstraint("sensitivity IN ('internal','restricted')", name="sens_valid"),
            server_default="internal",
            nullable=False,
        ),
        # applies_to: {asset_ids[], environments[], asset_tags[], service_versions[]}
        # 检索权限过滤必须下沉到 asset_id 粒度（D0 评审修正）
        sa.Column("applies_to", postgresql.JSONB(), nullable=True),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("embedding_model_version", sa.Text(), nullable=True),
        sa.Column("vector_dimension", sa.Integer(), nullable=True),
        sa.Column("chunking_version", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_versions"),
        sa.UniqueConstraint("document_id", "version_no", name="uq_kb_version_no"),
    )
    op.create_index("ix_kb_versions_doc_status", "knowledge_versions", ["document_id", "status"])

    # ---- 知识分块（tsvector 生成列 + GIN：文本检索路）----
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column(
            "version_id",
            sa.Text(),
            sa.ForeignKey("knowledge_versions.id"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_chunks"),
        sa.UniqueConstraint("version_id", "chunk_index", name="uq_kb_chunk_index"),
    )
    op.execute(
        "ALTER TABLE knowledge_chunks ADD COLUMN content_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED"
    )
    op.execute("CREATE INDEX ix_kb_chunks_tsv ON knowledge_chunks USING gin (content_tsv)")
    op.create_index("ix_kb_chunks_version_id", "knowledge_chunks", ["version_id"])

    # ---- 向量（pgvector；HNSW 按契约建立，检索默认精确排序保证过滤召回正确性）----
    op.create_table(
        "knowledge_embeddings",
        sa.Column("chunk_id", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("chunk_id", "model_version", name="pk_knowledge_embeddings"),
        sa.ForeignKeyConstraint(("chunk_id",), ["knowledge_chunks.id"], name="fk_kb_emb_chunk"),
    )
    op.execute("ALTER TABLE knowledge_embeddings ADD COLUMN vector VECTOR(384) NOT NULL")
    op.execute(
        "CREATE INDEX ix_knowledge_embeddings_hnsw ON knowledge_embeddings "
        "USING hnsw (vector vector_cosine_ops)"
    )
    op.create_index("ix_kb_emb_model_version", "knowledge_embeddings", ["model_version"])

    # ---- 检索事件（RAG 消融评估，DATA_MODEL §3.6）----
    op.create_table(
        "retrieval_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("investigation_runs.id"), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("filters", postgresql.JSONB(), nullable=True),
        sa.Column("result_version_ids", postgresql.JSONB(), nullable=True),
        sa.Column("ranks", postgresql.JSONB(), nullable=True),
        sa.Column("used_by_agent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_retrieval_events"),
    )
    op.create_index("ix_retrieval_events_run_id", "retrieval_events", ["run_id"])

    # ---- Runbook（CONTRACTS §2.10；草稿不可执行，D5 才有执行链路）----
    op.create_table(
        "runbooks",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "risk_level",
            sa.Text(),
            sa.CheckConstraint("risk_level IN ('r0','r1','r2','r3')", name="rb_risk_valid"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            sa.CheckConstraint("status IN ('draft','published','retired')", name="rb_status_valid"),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_runbooks"),
        sa.UniqueConstraint("name", name="uq_runbooks_name"),
    )
    op.create_table(
        "runbook_versions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("runbook_id", sa.Text(), sa.ForeignKey("runbooks.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            sa.CheckConstraint(
                "status IN ('draft','published','retired')", name="rbv_status_valid"
            ),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("steps", postgresql.JSONB(), nullable=True),
        sa.Column("preconditions", postgresql.JSONB(), nullable=True),
        sa.Column("parameters_schema", postgresql.JSONB(), nullable=True),
        sa.Column("allowed_environments", postgresql.JSONB(), nullable=True),
        sa.Column("allowlist_action_keys", postgresql.JSONB(), nullable=True),
        sa.Column(
            "risk_level",
            sa.Text(),
            sa.CheckConstraint("risk_level IN ('r0','r1','r2','r3')", name="rbv_risk_valid"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_runbook_versions"),
        sa.UniqueConstraint("runbook_id", "version_no", name="uq_runbook_version_no"),
    )
    op.create_index("ix_runbook_versions_runbook_id", "runbook_versions", ["runbook_id"])


def downgrade() -> None:
    op.drop_index("ix_runbook_versions_runbook_id", table_name="runbook_versions")
    op.drop_table("runbook_versions")
    op.drop_table("runbooks")
    op.drop_index("ix_retrieval_events_run_id", table_name="retrieval_events")
    op.drop_table("retrieval_events")
    op.drop_index("ix_kb_emb_model_version", table_name="knowledge_embeddings")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_embeddings_hnsw")
    op.drop_table("knowledge_embeddings")
    op.drop_index("ix_kb_chunks_version_id", table_name="knowledge_chunks")
    op.execute("DROP INDEX IF EXISTS ix_kb_chunks_tsv")
    op.drop_table("knowledge_chunks")
    op.drop_index("ix_kb_versions_doc_status", table_name="knowledge_versions")
    op.drop_table("knowledge_versions")
    op.drop_index("ix_knowledge_documents_doc_type", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
    op.drop_column("investigation_runs", "execution_snapshot")
    op.execute("DROP EXTENSION IF EXISTS vector")

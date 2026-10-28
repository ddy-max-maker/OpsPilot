"""D4 知识库与 RAG 表族（DATA_MODEL §3.6，CONTRACTS §2.9/§2.10）。

knowledge_embeddings（pgvector）不经 ORM 映射——向量读写使用原生 SQL
（VECTOR 列类型由迁移固化 384 维；检索过滤 model_version 防止版本混用）。
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
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class KnowledgeDocument(Base, TimestampMixin):
    """知识文档（CONTRACTS §2.9）：postmortem / guide / runbook 类型，来源可溯。"""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint("doc_type IN ('runbook','postmortem','guide')", name="doc_type_valid"),
        Index("ix_knowledge_documents_doc_type", "doc_type"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    doc_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_incident_id: Mapped[str | None] = mapped_column(
        ForeignKey("incidents.id"), nullable=True
    )
    owner: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    tags: Mapped[list[object] | None] = mapped_column(JSONB, nullable=True)


class KnowledgeVersion(Base):
    """知识版本（状态机：draft → reviewed → published → superseded/retired）。

    四眼原则：reviewed_by ≠ created_by（服务端强制，CONTRACTS §2.9）。
    未发布/已过期/已退役版本对 Agent 检索不可见（检索前置过滤）。
    """

    __tablename__ = "knowledge_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_kb_version_no"),
        CheckConstraint(
            "status IN ('draft','reviewed','published','superseded','retired')",
            name="status_valid",
        ),
        CheckConstraint("sensitivity IN ('internal','restricted')", name="sens_valid"),
        Index("ix_kb_versions_doc_status", "document_id", "status"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sensitivity: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'internal'")
    )
    # {asset_ids[], environments[], asset_tags[], service_versions[]}
    applies_to: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    # Embedding 版本快照（检索兼容判定；换模型 = 新版本向量，不覆盖旧索引）
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    vector_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunking_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class KnowledgeChunk(Base):
    """知识分块（tsvector 生成列由迁移创建，ORM 不映射——全文检索走原生 SQL）。"""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (UniqueConstraint("version_id", "chunk_index", name="uq_kb_chunk_index"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("knowledge_versions.id"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class RetrievalEvent(Base):
    """检索事件（RAG 消融评估锚点，PRD §6/§13）：模式/过滤/命中/排序/耗时。"""

    __tablename__ = "retrieval_events"
    __table_args__ = (Index("ix_retrieval_events_run_id", "run_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("investigation_runs.id"), nullable=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    filters: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    result_version_ids: Mapped[list[object] | None] = mapped_column(JSONB, nullable=True)
    ranks: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    used_by_agent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class Runbook(Base, TimestampMixin):
    """Runbook（受控操作模板，CONTRACTS §2.10）：D4 仅可检索，无执行链路。"""

    __tablename__ = "runbooks"
    __table_args__ = (
        UniqueConstraint("name", name="uq_runbooks_name"),
        CheckConstraint("risk_level IN ('r0','r1','r2','r3')", name="rb_risk_valid"),
        CheckConstraint("status IN ('draft','published','retired')", name="rb_status_valid"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_level: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)


class RunbookVersion(Base):
    """Runbook 版本：steps 结构化动作；allowed_environments 硬排除 production。"""

    __tablename__ = "runbook_versions"
    __table_args__ = (
        UniqueConstraint("runbook_id", "version_no", name="uq_runbook_version_no"),
        CheckConstraint("status IN ('draft','published','retired')", name="rbv_status_valid"),
        CheckConstraint("risk_level IN ('r0','r1','r2','r3')", name="rbv_risk_valid"),
        Index("ix_runbook_versions_runbook_id", "runbook_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    runbook_id: Mapped[str] = mapped_column(ForeignKey("runbooks.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    steps: Mapped[list[object] | None] = mapped_column(JSONB, nullable=True)
    preconditions: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    parameters_schema: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    allowed_environments: Mapped[list[object] | None] = mapped_column(JSONB, nullable=True)
    allowlist_action_keys: Mapped[list[object] | None] = mapped_column(JSONB, nullable=True)
    risk_level: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class KnowledgeStatus:
    """知识版本状态机合法转换（CONTRACTS §2.9；draft 不可跳过人工复核）。"""

    DRAFT = "draft"
    REVIEWED = "reviewed"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    RETIRED = "retired"

    TRANSITIONS: dict[str, frozenset[str]] = {
        DRAFT: frozenset({REVIEWED, RETIRED}),
        REVIEWED: frozenset({PUBLISHED, RETIRED}),
        PUBLISHED: frozenset({SUPERSEDED, RETIRED}),
        SUPERSEDED: frozenset(),
        RETIRED: frozenset(),
    }


class RunbookStatus:
    """Runbook/版本状态机（draft 不直接可执行；published 供检索）。"""

    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"

    TRANSITIONS: dict[str, frozenset[str]] = {
        DRAFT: frozenset({PUBLISHED, RETIRED}),
        PUBLISHED: frozenset({RETIRED}),
        RETIRED: frozenset(),
    }

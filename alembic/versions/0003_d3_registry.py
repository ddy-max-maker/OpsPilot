"""D3 扩展接入迁移：Capability Registry 三张表（DATA_MODEL §3.5 D3）。

依据 docs/DATA_MODEL.md v1.1 §2（D3 建表清单）与 CONTRACTS §2.8/§4：
- capability_manifests：声明式连接器能力清单（CONTRACTS §4 全文 JSONB）
- capability_test_runs：能力/连通性受控测试记录（POST /capabilities/{id}/tests）
- asset_observability_bindings：资产 ↔ 连接器实例观测绑定
  （UNIQUE(asset_id, binding_kind)；D3 起实例解析依据授权资产逐资产进行，
  修复 D2 按 connector_type 聚合时同类多实例相互覆盖的问题）

Revision ID: 0003_d3_registry
Revises: 0002_d2_diagnosis
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_d3_registry"
down_revision: str | None = "0002_d2_diagnosis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "capability_manifests",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column(
            "connector_type_id",
            sa.Text(),
            sa.ForeignKey("connector_types.id"),
            nullable=False,
        ),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("manifest_version", sa.Text(), nullable=False),
        sa.Column("compatibility", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_capability_manifests"),
    )
    op.create_index(
        "ix_capability_manifests_connector_type_id",
        "capability_manifests",
        ["connector_type_id"],
    )

    op.create_table(
        "capability_test_runs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column(
            "manifest_id",
            sa.Text(),
            sa.ForeignKey("capability_manifests.id"),
            nullable=False,
        ),
        sa.Column("triggered_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "result",
            sa.Text(),
            sa.CheckConstraint("result IN ('passed','failed')", name="result_valid"),
            nullable=False,
        ),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_capability_test_runs"),
    )

    op.create_table(
        "asset_observability_bindings",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("asset_id", sa.Text(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column(
            "instance_id",
            sa.Text(),
            sa.ForeignKey("connector_instances.id"),
            nullable=False,
        ),
        sa.Column(
            "binding_kind",
            sa.Text(),
            sa.CheckConstraint(
                "binding_kind IN ('metrics','logs','traces','health')", name="binding_kind_valid"
            ),
            nullable=False,
        ),
        sa.Column("config", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_asset_observability_bindings"),
        sa.UniqueConstraint("asset_id", "binding_kind", name="uq_asset_binding_kind"),
    )
    op.create_index(
        "ix_asset_bindings_instance_id", "asset_observability_bindings", ["instance_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_asset_bindings_instance_id", table_name="asset_observability_bindings")
    op.drop_table("asset_observability_bindings")
    op.drop_table("capability_test_runs")
    op.drop_index("ix_capability_manifests_connector_type_id", table_name="capability_manifests")
    op.drop_table("capability_manifests")

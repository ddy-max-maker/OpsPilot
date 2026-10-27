"""D1 基础工程初始迁移：用户/权限/资产/事件/审计/幂等/Outbox 共 11 张表。

依据 docs/DATA_MODEL.md v1.1 §3（D1 建表清单）：
- users / roles / user_roles / asset_access_policies（§3.1）
- assets / asset_edges（§3.2）
- incidents / alerts（§3.3，D1-A 仅建表，不实现业务接口）
- audit_events（§3.6）
- api_idempotency_keys / outbox_events（§3.8，建表暂不启用）

Revision ID: 0001_d1_foundation
Revises:
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_d1_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")


def upgrade() -> None:
    # ---- §3.1 用户与权限 ----
    op.create_table(
        "users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "roles",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("code", name="uq_roles_code"),
    )

    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("role_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_roles_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name="fk_user_roles_role_id_roles", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "role_id", name="pk_user_roles"),
    )

    # ---- §3.2 资产与拓扑 ----
    op.create_table(
        "assets",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("asset_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("criticality", sa.Text(), nullable=False),
        sa.Column("endpoints", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_assets"),
        sa.UniqueConstraint("name", "environment", name="uq_assets_name_environment"),
        sa.CheckConstraint(
            "environment IN ('sandbox','staging','production')", name="ck_assets_environment_valid"
        ),
        sa.CheckConstraint(
            "asset_type IN ('service','container','database','cache','gateway','host')",
            name="ck_assets_asset_type_valid",
        ),
        sa.CheckConstraint("status IN ('active','retired')", name="ck_assets_status_valid"),
        sa.CheckConstraint(
            "criticality IN ('low','medium','high')", name="ck_assets_criticality_valid"
        ),
    )
    op.create_index(
        "ix_assets_environment_status", "assets", ["environment", "status"], unique=False
    )
    op.create_index("ix_assets_created_at_id", "assets", ["created_at", "id"], unique=False)

    op.create_table(
        "asset_edges",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("from_asset_id", sa.String(), nullable=False),
        sa.Column("to_asset_id", sa.String(), nullable=False),
        sa.Column("relation", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["from_asset_id"],
            ["assets.id"],
            name="fk_asset_edges_from_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_asset_id"],
            ["assets.id"],
            name="fk_asset_edges_to_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_asset_edges"),
        sa.UniqueConstraint("from_asset_id", "to_asset_id", "relation", name="uq_asset_edges_edge"),
        sa.CheckConstraint("from_asset_id <> to_asset_id", name="ck_asset_edges_no_self_loop"),
        sa.CheckConstraint(
            "relation IN ('depends_on','runs_on','serves')", name="ck_asset_edges_relation_valid"
        ),
    )
    op.create_index("ix_asset_edges_to_asset_id", "asset_edges", ["to_asset_id"], unique=False)

    op.create_table(
        "asset_access_policies",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("role_id", sa.String(), nullable=True),
        sa.Column("asset_id", sa.String(), nullable=True),
        sa.Column("environment", sa.Text(), nullable=True),
        sa.Column("permission", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_asset_access_policies_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name="fk_asset_access_policies_role_id_roles",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_asset_access_policies_asset_id_assets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_asset_access_policies"),
        sa.CheckConstraint(
            "(user_id IS NOT NULL)::int + (role_id IS NOT NULL)::int = 1",
            name="ck_asset_access_policies_subject_exclusive",
        ),
        sa.CheckConstraint(
            "environment IN ('sandbox','staging','production') OR environment IS NULL",
            name="ck_asset_access_policies_environment_valid",
        ),
        sa.CheckConstraint(
            "permission IN ('read','investigate','approve','execute')",
            name="ck_asset_access_policies_permission_valid",
        ),
    )
    op.create_index(
        "ix_asset_access_policies_user_id", "asset_access_policies", ["user_id"], unique=False
    )
    op.create_index(
        "ix_asset_access_policies_role_id", "asset_access_policies", ["role_id"], unique=False
    )
    op.create_index(
        "ix_asset_access_policies_asset_id", "asset_access_policies", ["asset_id"], unique=False
    )

    # ---- §3.3 告警与事件（D1-A 仅建表）----
    op.create_table(
        "incidents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("primary_asset_id", sa.String(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("alert_id", sa.String(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["primary_asset_id"], ["assets.id"], name="fk_incidents_primary_asset_id_assets"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_incidents_created_by_users"),
        sa.PrimaryKeyConstraint("id", name="pk_incidents"),
        sa.CheckConstraint(
            "environment IN ('sandbox','staging','production')",
            name="ck_incidents_environment_valid",
        ),
        sa.CheckConstraint("source IN ('manual','alert')", name="ck_incidents_source_valid"),
        sa.CheckConstraint(
            "severity IN ('low','medium','high','critical')", name="ck_incidents_severity_valid"
        ),
        sa.CheckConstraint(
            "status IN ('open','acknowledged','investigating','mitigating','monitoring',"
            "'resolved','closed','needs_human')",
            name="ck_incidents_status_valid",
        ),
    )
    op.create_index(
        "ix_incidents_environment_status", "incidents", ["environment", "status"], unique=False
    )
    op.create_index(
        "ix_incidents_primary_asset_id_status",
        "incidents",
        ["primary_asset_id", "status"],
        unique=False,
    )
    op.create_index("ix_incidents_created_at", "incidents", ["created_at"], unique=False)
    # 局部唯一：活跃单去重（open~monitoring 状态下同 primary_asset+fingerprint 不重复建单）
    op.create_index(
        "uq_incidents_active_fingerprint",
        "incidents",
        ["primary_asset_id", "fingerprint"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('open','acknowledged','investigating','mitigating','monitoring')"
        ),
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("external_event_id", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("incident_id", sa.String(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_alerts_incident_id_incidents",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_alerts"),
        sa.UniqueConstraint("connector_id", "external_event_id", name="uq_alerts_delivery"),
        sa.CheckConstraint(
            "severity IN ('low','medium','high','critical')", name="ck_alerts_severity_valid"
        ),
        sa.CheckConstraint(
            "status IN ('received','mapped','ignored')", name="ck_alerts_status_valid"
        ),
    )
    op.create_index("ix_alerts_fingerprint", "alerts", ["fingerprint"], unique=False)
    op.create_index("ix_alerts_incident_id", "alerts", ["incident_id"], unique=False)
    op.create_index("ix_alerts_received_at", "alerts", ["received_at"], unique=False)

    # ---- §3.6 审计（只追加）----
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=True),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("environment", sa.Text(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        sa.CheckConstraint(
            "actor_type IN ('user','agent','system','action_service')",
            name="ck_audit_events_actor_type_valid",
        ),
    )

    # ---- §3.8 幂等键与 Outbox（建表；D1-A 不启用消费逻辑）----
    op.create_table(
        "api_idempotency_keys",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_api_idempotency_keys_user_id_users"
        ),
        sa.PrimaryKeyConstraint("key", "endpoint", "user_id", name="pk_api_idempotency_keys"),
        sa.CheckConstraint(
            "state IN ('received','completed')", name="ck_api_idempotency_keys_state_valid"
        ),
    )
    op.create_index(
        "ix_api_idempotency_keys_expires_at", "api_idempotency_keys", ["expires_at"], unique=False
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        sa.CheckConstraint(
            "status IN ('pending','dispatched','failed')", name="ck_outbox_events_status_valid"
        ),
    )
    op.create_index(
        "ix_outbox_events_status_next_attempt_at",
        "outbox_events",
        ["status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    """按依赖逆序完整回滚（迁移测试覆盖 downgrade base）。"""
    op.drop_table("outbox_events")
    op.drop_table("api_idempotency_keys")
    op.drop_table("audit_events")
    op.drop_table("alerts")
    op.drop_table("incidents")
    op.drop_table("asset_access_policies")
    op.drop_table("asset_edges")
    op.drop_table("assets")
    op.drop_table("user_roles")
    op.drop_table("roles")
    op.drop_table("users")

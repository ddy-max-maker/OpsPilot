"""D3 Capability Registry 表族（DATA_MODEL §3.5 D3）。

capability_manifests / capability_test_runs / asset_observability_bindings。
asset_observability_bindings 是"依据授权资产解析连接器实例"的解析依据
（D3 修复同类实例按 connector_type 聚合相互覆盖的问题）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CapabilityManifest(Base):
    """连接器能力清单快照（CONTRACTS §4 全文；经 Registry 兼容校验后落库）。"""

    __tablename__ = "capability_manifests"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    connector_type_id: Mapped[str] = mapped_column(ForeignKey("connector_types.id"), nullable=False)
    manifest: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    manifest_version: Mapped[str] = mapped_column(Text, nullable=False)
    compatibility: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CapabilityTestRun(Base):
    """受控能力/连通性测试记录（POST /capabilities/{id}/tests；只追加）。"""

    __tablename__ = "capability_test_runs"
    __table_args__ = (CheckConstraint("result IN ('passed','failed')", name="result_valid"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    manifest_id: Mapped[str] = mapped_column(ForeignKey("capability_manifests.id"), nullable=False)
    triggered_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AssetObservabilityBinding(Base):
    """资产 ↔ 连接器实例观测绑定（metrics/logs/traces/health）。

    解析规则（D3）：工具调用按 (授权资产, connector_type) 解析实例——
    1) 命中本表绑定 → 使用该实例配置；
    2) 未绑定且该类型仅一个 enabled 实例 → 使用默认实例（D2 兼容）；
    3) 未绑定且该类型存在多个 enabled 实例 → 拒绝（防止串用其他资产的凭据/数据）。
    """

    __tablename__ = "asset_observability_bindings"
    __table_args__ = (
        UniqueConstraint("asset_id", "binding_kind", name="uq_asset_binding_kind"),
        CheckConstraint(
            "binding_kind IN ('metrics','logs','traces','health')", name="binding_kind_valid"
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), nullable=False)
    instance_id: Mapped[str] = mapped_column(ForeignKey("connector_instances.id"), nullable=False)
    binding_kind: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

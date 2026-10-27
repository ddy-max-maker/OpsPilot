"""事件领域 Schema（Pydantic v2，对齐 CONTRACTS §2.3）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import Environment, IncidentSource, IncidentStatus, Severity


class IncidentCreateRequest(BaseModel):
    """POST /incidents 请求体（手工建单；Webhook 建单走 alerts 领域）。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    environment: Environment
    primary_asset_id: str = Field(min_length=1, max_length=64)
    severity: Severity = Severity.MEDIUM
    # 活跃单去重键：同 asset+fingerprint 活跃单唯一（DB 局部唯一索引兜底）；
    # NULL 不参与唯一索引（PG 默认 NULL distinct），手工建单可不提供
    fingerprint: str | None = Field(default=None, max_length=200)


class IncidentResponse(BaseModel):
    """事件响应（含状态机字段与关联引用）。"""

    id: str
    title: str
    description: str | None
    environment: Environment
    primary_asset_id: str
    source: IncidentSource
    alert_id: str | None
    fingerprint: str | None
    severity: Severity
    status: IncidentStatus
    created_by: str
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class IncidentListData(BaseModel):
    """cursor 分页列表（契约 §1.3）。"""

    items: list[IncidentResponse]
    next_cursor: str | None
    has_more: bool

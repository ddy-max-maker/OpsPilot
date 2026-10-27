"""告警接入 Schema（对齐 CONTRACTS §2.2 Alert 实体）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import Severity


class AlertIngestRequest(BaseModel):
    """POST /webhooks/alerts/{connector_id} 请求体（可信来源签名校验后解析）。

    delivery_id 即外部投递 ID（external_event_id），与 connector_id 组成
    去重主手段（uq_alerts_delivery）；fingerprint 为归一化症状指纹。
    """

    model_config = ConfigDict(extra="forbid")

    delivery_id: str = Field(min_length=1, max_length=200)
    fingerprint: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    severity: Severity
    asset_id: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=5000)
    # 原始告警体（JSONB 落库；D1 骨架原样存储，脱敏属 D2+ Tool Gateway 范围）
    payload: dict[str, object] | None = None


class AlertIngestData(BaseModel):
    """告警接入结果：关联既有 incident 或新建 incident。"""

    alert_id: str
    incident_id: str
    incident_created: bool

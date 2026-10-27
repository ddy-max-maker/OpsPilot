"""告警 Webhook API（CONTRACTS §5）：POST /webhooks/alerts/{connector_id}。

可信来源接入：HMAC-SHA256(raw body, WEBHOOK_ALERTS_SECRET) 签名 + X-Timestamp
（unix 秒，容差 ±webhook_timestamp_tolerance_seconds，默认 ±5min）防重放；
幂等主手段 = delivery_id 去重（uq_alerts_delivery），不依赖 Idempotency-Key。
本端点为未认证端点（外部系统集成），不做 JWT/RBAC 校验。
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Path, Request
from pydantic import ValidationError

from app.api.deps import SessionDep, SettingsDep
from app.core.errors import unauthenticated, validation_error
from app.core.responses import Envelope, ok
from app.domains.alerts import service as alert_service
from app.domains.alerts.schemas import AlertIngestData, AlertIngestRequest

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def verify_webhook_signature(
    raw_body: bytes,
    signature: str,
    timestamp: str,
    secret: str,
    tolerance_seconds: int,
) -> None:
    """校验签名与时间戳；任何失败统一 401（不区分失败原因，防探测）。"""
    if not signature or not timestamp:
        raise unauthenticated("invalid webhook signature or expired timestamp")
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.strip().lower(), expected):
        raise unauthenticated("invalid webhook signature or expired timestamp")
    try:
        ts = int(timestamp.strip())
    except ValueError as exc:
        raise unauthenticated("invalid webhook signature or expired timestamp") from exc
    now = datetime.now(UTC).timestamp()
    if abs(now - ts) > tolerance_seconds:
        raise unauthenticated("invalid webhook signature or expired timestamp")


@router.post("/alerts/{connector_id}", response_model=Envelope[AlertIngestData])
async def ingest_alert(
    connector_id: Annotated[str, Path(min_length=1, max_length=100)],
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> Envelope[AlertIngestData]:
    """接收外部告警：签名校验 → 去重 → 关联既有 incident 或新建（主体=系统账号）。"""
    raw_body = await request.body()
    verify_webhook_signature(
        raw_body,
        request.headers.get("X-Signature", ""),
        request.headers.get("X-Timestamp", ""),
        settings.webhook_alerts_secret,
        settings.webhook_timestamp_tolerance_seconds,
    )
    try:
        body = AlertIngestRequest.model_validate_json(raw_body)
    except ValidationError as exc:
        raise validation_error("webhook payload validation failed") from exc
    data = await alert_service.ingest_alert(session, connector_id, body)
    return ok(data)

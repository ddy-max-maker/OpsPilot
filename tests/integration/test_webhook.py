"""告警 Webhook 集成测试：HMAC 签名/时间戳窗口/delivery 去重/系统身份/重复告警不产生多事故。"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
from sqlalchemy import func, select

from app.models import Alert, AuditEvent, Incident

WEBHOOK = "/api/v1/webhooks/alerts/webhook_default"
WEBHOOK_SECRET = "test-webhook-secret-0123456789abcdef"  # 与 conftest 注入一致


def _sign_headers(raw_body: bytes, *, timestamp: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if timestamp is None else timestamp
    signature = hmac.new(WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return {"X-Signature": signature, "X-Timestamp": str(ts), "Content-Type": "application/json"}


def _alert_body(
    asset_id: str,
    delivery_id: str = "delivery-001",
    fingerprint: str = "cpu-saturation@v1",
    severity: str = "high",
) -> bytes:
    return json.dumps(
        {
            "delivery_id": delivery_id,
            "fingerprint": fingerprint,
            "title": "CPU 持续饱和",
            "severity": severity,
            "asset_id": asset_id,
            "description": "container cpu usage > 95% for 5m",
            "payload": {"alertname": "CPUSaturation", "value": 96.2},
        },
        separators=(",", ":"),
    ).encode("utf-8")


async def _create_asset(client: httpx.AsyncClient, admin_headers: dict, name: str) -> str:
    resp = await client.post(
        "/api/v1/assets",
        json={"name": name, "environment": "sandbox", "asset_type": "container"},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


# ---- 正常接入：新建 incident ----


async def test_alert_creates_incident_with_system_identity(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    asset_id = await _create_asset(client, ready_admin, "wh-svc-1")
    raw = _alert_body(asset_id)
    resp = await client.post(WEBHOOK, content=raw, headers=_sign_headers(raw))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["alert_id"].startswith("alr_")
    assert data["incident_id"].startswith("inc_")
    assert data["incident_created"] is True

    async with db_session_factory() as session:
        incident = await session.get(Incident, data["incident_id"])
        alert = await session.get(Alert, data["alert_id"])
        assert incident is not None
        assert incident.created_by == "usr_system_alerts"  # 系统身份建单
        assert incident.source == "alert"
        assert incident.status == "open"
        assert incident.alert_id == data["alert_id"]
        assert incident.environment == "sandbox"
        assert alert is not None
        assert alert.status == "mapped"
        assert alert.external_event_id == "delivery-001"
        assert alert.incident_id == data["incident_id"]
        # 系统身份审计：incident.create + alert.ingest
        actions = (
            await session.execute(
                select(AuditEvent.action, AuditEvent.actor_type)
                .where(AuditEvent.actor_id == "usr_system_alerts")
                .order_by(AuditEvent.id)
            )
        ).all()
    assert ("incident.create", "system") in actions
    assert ("alert.ingest", "system") in actions


# ---- 去重：重复 delivery 不产生多个事故（核心验收）----


async def test_duplicate_delivery_does_not_create_second_incident(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    asset_id = await _create_asset(client, ready_admin, "wh-svc-2")
    raw = _alert_body(asset_id, delivery_id="delivery-dup-1")

    first = await client.post(WEBHOOK, content=raw, headers=_sign_headers(raw))
    assert first.status_code == 200
    incident_id = first.json()["data"]["incident_id"]

    # 同一 delivery 重复投递（不同的签名/时间戳，模拟第三方重试）
    second = await client.post(WEBHOOK, content=raw, headers=_sign_headers(raw))
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "DUPLICATE_ALERT"
    details = second.json()["error"]["details"]
    assert details["incident_id"] == incident_id

    async with db_session_factory() as session:
        incident_count = (
            await session.execute(
                select(func.count())
                .select_from(Incident)
                .where(Incident.primary_asset_id == asset_id)
            )
        ).scalar_one()
        alert_count = (
            await session.execute(
                select(func.count())
                .select_from(Alert)
                .where(
                    Alert.connector_id == "webhook_default",
                    Alert.external_event_id == "delivery-dup-1",
                )
            )
        ).scalar_one()
    assert incident_count == 1  # 重复告警未产生第二个事故
    assert alert_count == 1


async def test_same_fingerprint_different_delivery_maps_to_existing_incident(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    """同资产+同指纹的后续告警映射到既有活跃单，不新建事故。"""
    asset_id = await _create_asset(client, ready_admin, "wh-svc-3")
    raw1 = _alert_body(asset_id, delivery_id="delivery-map-1")
    raw2 = _alert_body(asset_id, delivery_id="delivery-map-2")

    first = await client.post(WEBHOOK, content=raw1, headers=_sign_headers(raw1))
    assert first.status_code == 200
    assert first.json()["data"]["incident_created"] is True
    incident_id = first.json()["data"]["incident_id"]

    second = await client.post(WEBHOOK, content=raw2, headers=_sign_headers(raw2))
    assert second.status_code == 200
    data = second.json()["data"]
    assert data["incident_created"] is False
    assert data["incident_id"] == incident_id  # 关联既有事故

    async with db_session_factory() as session:
        incident_count = (
            await session.execute(select(func.count()).select_from(Incident))
        ).scalar_one()
    assert incident_count == 1


# ---- 签名与时间戳防重放 ----


async def test_invalid_signature_rejected_401(client: httpx.AsyncClient, ready_admin: dict) -> None:
    asset_id = await _create_asset(client, ready_admin, "wh-svc-4")
    raw = _alert_body(asset_id)
    headers = _sign_headers(raw)
    headers["X-Signature"] = "0" * 64
    resp = await client.post(WEBHOOK, content=raw, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_signature_over_wrong_body_rejected(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    asset_id = await _create_asset(client, ready_admin, "wh-svc-5")
    raw = _alert_body(asset_id)
    headers = _sign_headers(raw)
    tampered = raw.replace(b"CPUSaturation", b"CPUSaturatioO")  # 字节级篡改
    resp = await client.post(WEBHOOK, content=tampered, headers=headers)
    assert resp.status_code == 401


async def test_missing_headers_rejected(client: httpx.AsyncClient) -> None:
    resp = await client.post(WEBHOOK, content=b"{}", headers={"Content-Type": "application/json"})
    assert resp.status_code == 401


async def test_stale_timestamp_rejected_401(client: httpx.AsyncClient, ready_admin: dict) -> None:
    """超出 ±300s 容差的时间戳拒绝（防重放窗口）。"""
    asset_id = await _create_asset(client, ready_admin, "wh-svc-6")
    raw = _alert_body(asset_id)
    stale = _sign_headers(raw, timestamp=int(time.time()) - 3600)
    resp = await client.post(WEBHOOK, content=raw, headers=stale)
    assert resp.status_code == 401
    future = _sign_headers(raw, timestamp=int(time.time()) + 3600)
    resp2 = await client.post(WEBHOOK, content=raw, headers=future)
    assert resp2.status_code == 401


# ---- 业务校验 ----


async def test_unknown_asset_rejected_400_no_alert_persisted(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    raw = _alert_body("as_unknown_asset")
    resp = await client.post(WEBHOOK, content=raw, headers=_sign_headers(raw))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    async with db_session_factory() as session:
        count = (await session.execute(select(func.count()).select_from(Alert))).scalar_one()
    assert count == 0  # 失败整体回滚，无残留


async def test_invalid_payload_rejected_400(client: httpx.AsyncClient, ready_admin: dict) -> None:
    asset_id = await _create_asset(client, ready_admin, "wh-svc-7")
    raw = json.dumps(
        {
            "delivery_id": "delivery-bad",
            "fingerprint": "fp",
            "title": "t",
            "severity": "urgent",  # 非法枚举
            "asset_id": asset_id,
        }
    ).encode()
    resp = await client.post(WEBHOOK, content=raw, headers=_sign_headers(raw))
    assert resp.status_code == 400

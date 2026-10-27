"""故障事件 API 集成测试：创建（幂等）/列表筛选/详情/确认/RBAC/范围/审计。"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import func, select

from app.models import AuditEvent, Incident
from tests.conftest import api_login, auth_headers, create_test_user

ASSETS = "/api/v1/assets"
INCIDENTS = "/api/v1/incidents"


async def _create_asset(
    client: httpx.AsyncClient, admin_headers: dict, name: str, environment: str = "sandbox"
) -> str:
    resp = await client.post(
        ASSETS,
        json={"name": name, "environment": environment, "asset_type": "service"},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


def _incident_payload(asset_id: str, environment: str = "sandbox", **extra: object) -> dict:
    payload: dict[str, object] = {
        "title": "CPU 飙高",
        "description": "order-api CPU 使用率超过 95%",
        "environment": environment,
        "primary_asset_id": asset_id,
        "severity": "high",
    }
    payload.update(extra)
    return payload


async def _create_incident(
    client: httpx.AsyncClient,
    headers: dict,
    payload: dict,
    idempotency_key: str | None = None,
) -> httpx.Response:
    request_headers = dict(headers)
    if idempotency_key is not None:
        request_headers["Idempotency-Key"] = idempotency_key
    return await client.post(INCIDENTS, json=payload, headers=request_headers)


# ---- 创建（幂等端点契约）----


async def test_create_requires_idempotency_key(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    asset_id = await _create_asset(client, ready_admin, "inc-nokey-svc")
    resp = await _create_incident(
        client, ready_admin, _incident_payload(asset_id), idempotency_key=None
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_create_incident_success_and_shape(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    asset_id = await _create_asset(client, ready_admin, "inc-create-svc")
    resp = await _create_incident(
        client, ready_admin, _incident_payload(asset_id), idempotency_key=str(uuid.uuid4())
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["id"].startswith("inc_")
    assert data["title"] == "CPU 飙高"
    assert data["environment"] == "sandbox"
    assert data["primary_asset_id"] == asset_id
    assert data["source"] == "manual"
    assert data["alert_id"] is None
    assert data["status"] == "open"
    assert data["severity"] == "high"
    assert data["created_by"].startswith("usr_")
    assert data["resolved_at"] is None
    # 响应包裹含 request_id，error 为 null
    envelope = resp.json()
    assert envelope["error"] is None
    assert envelope["request_id"]


async def test_create_environment_must_match_asset(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    asset_id = await _create_asset(client, ready_admin, "inc-env-svc", "production")
    resp = await _create_incident(
        client,
        ready_admin,
        _incident_payload(asset_id, "sandbox"),
        idempotency_key=str(uuid.uuid4()),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_create_unknown_asset_404(client: httpx.AsyncClient, ready_admin: dict) -> None:
    resp = await _create_incident(
        client,
        ready_admin,
        _incident_payload("as_doesnotexist"),
        idempotency_key=str(uuid.uuid4()),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


async def test_active_fingerprint_conflict_409(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    """同资产+同指纹存在活跃单时拒绝新建（uq_incidents_active_fingerprint 语义）。"""
    asset_id = await _create_asset(client, ready_admin, "inc-fp-svc")
    first = await _create_incident(
        client,
        ready_admin,
        _incident_payload(asset_id, fingerprint="disk-full@v1"),
        idempotency_key=str(uuid.uuid4()),
    )
    assert first.status_code == 201
    second = await _create_incident(
        client,
        ready_admin,
        _incident_payload(asset_id, fingerprint="disk-full@v1"),
        idempotency_key=str(uuid.uuid4()),
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT_STATE"
    # 指纹不同不受影响
    third = await _create_incident(
        client,
        ready_admin,
        _incident_payload(asset_id, fingerprint="oom-kill@v1"),
        idempotency_key=str(uuid.uuid4()),
    )
    assert third.status_code == 201


async def test_validation_rejects_unknown_fields(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    asset_id = await _create_asset(client, ready_admin, "inc-extra-svc")
    payload = _incident_payload(asset_id)
    payload["hacker_field"] = "x"
    resp = await _create_incident(client, ready_admin, payload, idempotency_key=str(uuid.uuid4()))
    assert resp.status_code == 400


# ---- 详情 ----


async def test_get_incident_detail(client: httpx.AsyncClient, ready_admin: dict) -> None:
    asset_id = await _create_asset(client, ready_admin, "inc-detail-svc")
    created = await _create_incident(
        client, ready_admin, _incident_payload(asset_id), idempotency_key=str(uuid.uuid4())
    )
    incident_id = created.json()["data"]["id"]
    resp = await client.get(f"{INCIDENTS}/{incident_id}", headers=ready_admin)
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == incident_id


async def test_get_nonexistent_incident_404(client: httpx.AsyncClient, ready_admin: dict) -> None:
    resp = await client.get(f"{INCIDENTS}/inc_doesnotexist", headers=ready_admin)
    assert resp.status_code == 404


# ---- 故障确认（状态机）----


async def test_acknowledge_open_to_acknowledged(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    asset_id = await _create_asset(client, ready_admin, "inc-ack-svc")
    created = await _create_incident(
        client, ready_admin, _incident_payload(asset_id), idempotency_key=str(uuid.uuid4())
    )
    incident_id = created.json()["data"]["id"]
    resp = await client.post(f"{INCIDENTS}/{incident_id}/acknowledge", headers=ready_admin)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "acknowledged"
    assert data["id"] == incident_id
    # 重复确认：acknowledged -> acknowledged 非法 → 409
    again = await client.post(f"{INCIDENTS}/{incident_id}/acknowledge", headers=ready_admin)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CONFLICT_STATE"
    assert again.json()["error"]["details"] == {"from": "acknowledged", "to": "acknowledged"}


# ---- 列表与筛选 ----


async def test_list_filters_by_status_severity_environment_asset(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    sb = await _create_asset(client, ready_admin, "inc-lf-sb")
    pd = await _create_asset(client, ready_admin, "inc-lf-pd", "production")

    r1 = await _create_incident(
        client,
        ready_admin,
        _incident_payload(sb, severity="high"),
        idempotency_key=str(uuid.uuid4()),
    )
    r2 = await _create_incident(
        client,
        ready_admin,
        _incident_payload(pd, "production", severity="low", title="磁盘告警"),
        idempotency_key=str(uuid.uuid4()),
    )
    assert r1.status_code == 201 and r2.status_code == 201
    i1, i2 = r1.json()["data"], r2.json()["data"]

    # 确认 r1 使状态过滤可区分
    ack = await client.post(f"{INCIDENTS}/{i1['id']}/acknowledge", headers=ready_admin)
    assert ack.status_code == 200

    by_status = await client.get(INCIDENTS + "?status=open", headers=ready_admin)
    assert [i["id"] for i in by_status.json()["data"]["items"]] == [i2["id"]]

    by_severity = await client.get(INCIDENTS + "?severity=low", headers=ready_admin)
    assert [i["id"] for i in by_severity.json()["data"]["items"]] == [i2["id"]]

    by_env = await client.get(INCIDENTS + "?environment=production", headers=ready_admin)
    assert [i["id"] for i in by_env.json()["data"]["items"]] == [i2["id"]]

    by_asset = await client.get(INCIDENTS + f"?primary_asset_id={sb}", headers=ready_admin)
    assert [i["id"] for i in by_asset.json()["data"]["items"]] == [i1["id"]]

    combined = await client.get(
        INCIDENTS + "?status=open&environment=production&severity=low", headers=ready_admin
    )
    assert [i["id"] for i in combined.json()["data"]["items"]] == [i2["id"]]


async def test_list_scope_for_read_only_operator(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    """read 范围过滤：sandbox 只读操作员只见 sandbox 事件；production 详情 404 伪装。"""
    sb = await _create_asset(client, ready_admin, "inc-scope-sb")
    pd = await _create_asset(client, ready_admin, "inc-scope-pd", "production")
    created_sb = await _create_incident(
        client, ready_admin, _incident_payload(sb), idempotency_key=str(uuid.uuid4())
    )
    created_pd = await _create_incident(
        client,
        ready_admin,
        _incident_payload(pd, "production"),
        idempotency_key=str(uuid.uuid4()),
    )
    pd_incident_id = created_pd.json()["data"]["id"]

    await create_test_user(
        db_session_factory,
        username="inc_reader",
        password="IncReader123!",
        role_codes=["operator"],
        policies=[{"environment": "sandbox", "permission": "read"}],
    )
    login = await api_login(client, "inc_reader", "IncReader123!")
    headers = auth_headers(login.json()["data"]["access_token"])

    listed = await client.get(INCIDENTS, headers=headers)
    assert listed.status_code == 200
    ids = [i["id"] for i in listed.json()["data"]["items"]]
    assert created_sb.json()["data"]["id"] in ids
    assert pd_incident_id not in ids

    masked = await client.get(f"{INCIDENTS}/{pd_incident_id}", headers=headers)
    assert masked.status_code == 404  # 越权伪装，不暴露存在性


# ---- RBAC（角色矩阵 CONTRACTS §7.1）----


async def test_viewer_cannot_create_or_acknowledge(
    client: httpx.AsyncClient, ready_admin: dict, viewer_headers: dict
) -> None:
    asset_id = await _create_asset(client, ready_admin, "inc-viewer-svc")
    created = await _create_incident(
        client, ready_admin, _incident_payload(asset_id), idempotency_key=str(uuid.uuid4())
    )
    incident_id = created.json()["data"]["id"]

    denied = await _create_incident(
        client, viewer_headers, _incident_payload(asset_id), idempotency_key=str(uuid.uuid4())
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "FORBIDDEN"

    ack_denied = await client.post(f"{INCIDENTS}/{incident_id}/acknowledge", headers=viewer_headers)
    assert ack_denied.status_code == 403


async def test_investigate_scope_gates_create_and_acknowledge(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    """investigate 范围语义：仅有 read 策略的 operator 创建/确认 → 404 伪装；
    授予 investigate 策略后可用。"""
    asset_id = await _create_asset(client, ready_admin, "inc-inv-svc")
    created = await _create_incident(
        client, ready_admin, _incident_payload(asset_id), idempotency_key=str(uuid.uuid4())
    )
    incident_id = created.json()["data"]["id"]

    await create_test_user(
        db_session_factory,
        username="inv_only_read",
        password="InvRead123!",
        role_codes=["operator"],
        policies=[{"environment": "sandbox", "permission": "read"}],
    )
    login = await api_login(client, "inv_only_read", "InvRead123!")
    read_only = auth_headers(login.json()["data"]["access_token"])

    create_denied = await _create_incident(
        client,
        read_only,
        _incident_payload(asset_id, title="越权创建"),
        idempotency_key=str(uuid.uuid4()),
    )
    assert create_denied.status_code == 404  # investigate 范围为空 → 伪装

    ack_denied = await client.post(f"{INCIDENTS}/{incident_id}/acknowledge", headers=read_only)
    assert ack_denied.status_code == 404

    # 授予 investigate 范围后可用
    await create_test_user(
        db_session_factory,
        username="inv_granted",
        password="InvGrant123!",
        role_codes=["operator"],
        policies=[{"environment": "sandbox", "permission": "investigate"}],
    )
    login2 = await api_login(client, "inv_granted", "InvGrant123!")
    granted = auth_headers(login2.json()["data"]["access_token"])

    ok_created = await _create_incident(
        client,
        granted,
        _incident_payload(asset_id, title="授权创建"),
        idempotency_key=str(uuid.uuid4()),
    )
    assert ok_created.status_code == 201
    ack_ok = await client.post(
        f"{INCIDENTS}/{ok_created.json()['data']['id']}/acknowledge", headers=granted
    )
    assert ack_ok.status_code == 200
    assert ack_ok.json()["data"]["status"] == "acknowledged"


# ---- 审计 ----


async def test_incident_lifecycle_audited(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    asset_id = await _create_asset(client, ready_admin, "inc-audit-svc")
    created = await _create_incident(
        client, ready_admin, _incident_payload(asset_id), idempotency_key=str(uuid.uuid4())
    )
    incident_id = created.json()["data"]["id"]
    await client.post(f"{INCIDENTS}/{incident_id}/acknowledge", headers=ready_admin)

    async with db_session_factory() as session:
        rows = (
            await session.execute(
                select(AuditEvent.action, AuditEvent.actor_type)
                .where(AuditEvent.resource_id == incident_id)
                .order_by(AuditEvent.id)
            )
        ).all()
    assert rows == [
        ("incident.create", "user"),
        ("incident.acknowledge", "user"),
    ]


async def test_incident_count_in_database(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    """持久化证据：PostgreSQL 中真实存在 incident 行。"""
    asset_id = await _create_asset(client, ready_admin, "inc-db-svc")
    before = await _create_incident(
        client, ready_admin, _incident_payload(asset_id), idempotency_key=str(uuid.uuid4())
    )
    _ = before
    async with db_session_factory() as session:
        total = (await session.execute(select(func.count()).select_from(Incident))).scalar_one()
    assert total >= 1

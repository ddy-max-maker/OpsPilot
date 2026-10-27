"""API 幂等集成测试（CONTRACTS §1.3）：重放/异体冲突/并发/失败不烧 key/跨用户隔离。"""

from __future__ import annotations

import asyncio
import uuid

import httpx
from sqlalchemy import func, select

from app.domains.idempotency.service import endpoint_tag, hash_request_body
from app.models import ApiIdempotencyKey, Incident
from tests.conftest import api_login, auth_headers, create_test_user

ASSETS = "/api/v1/assets"
INCIDENTS = "/api/v1/incidents"


async def _create_asset(client: httpx.AsyncClient, admin_headers: dict, name: str) -> str:
    resp = await client.post(
        ASSETS,
        json={"name": name, "environment": "sandbox", "asset_type": "service"},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


def _payload(asset_id: str, title: str = "幂等测试事件", **extra: object) -> dict:
    payload: dict[str, object] = {
        "title": title,
        "environment": "sandbox",
        "primary_asset_id": asset_id,
        "severity": "medium",
    }
    payload.update(extra)
    return payload


async def _post_incident(
    client: httpx.AsyncClient, headers: dict, payload: dict, key: str
) -> httpx.Response:
    return await client.post(INCIDENTS, json=payload, headers={**headers, "Idempotency-Key": key})


# ---- 重放语义 ----


async def test_replay_returns_first_response_with_header(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    asset_id = await _create_asset(client, ready_admin, "idem-svc-1")
    key = str(uuid.uuid4())
    payload = _payload(asset_id)

    first = await _post_incident(client, ready_admin, payload, key)
    assert first.status_code == 201, first.text
    assert first.headers.get("Idempotency-Replayed") is None
    incident_id = first.json()["data"]["id"]

    second = await _post_incident(client, ready_admin, payload, key)
    assert second.status_code == 201
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert second.json()["data"]["id"] == incident_id  # 与首次响应一致

    async with db_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Incident)
                .where(Incident.primary_asset_id == asset_id)
            )
        ).scalar_one()
    assert count == 1  # 重复提交未产生第二个事故


async def test_same_key_different_body_conflict(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    asset_id = await _create_asset(client, ready_admin, "idem-svc-2")
    key = str(uuid.uuid4())

    first = await _post_incident(client, ready_admin, _payload(asset_id), key)
    assert first.status_code == 201

    conflict = await _post_incident(
        client, ready_admin, _payload(asset_id, title="不同的请求体"), key
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"


async def test_same_body_different_key_creates_new_incident(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    """不同 key 之间互不影响（同 key 才有幂等语义）。"""
    asset_id = await _create_asset(client, ready_admin, "idem-svc-3")
    payload = _payload(asset_id)
    r1 = await _post_incident(client, ready_admin, payload, str(uuid.uuid4()))
    r2 = await _post_incident(client, ready_admin, payload, str(uuid.uuid4()))
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["data"]["id"] != r2.json()["data"]["id"]
    assert r2.headers.get("Idempotency-Replayed") is None


async def test_key_length_bounds(client: httpx.AsyncClient, ready_admin: dict) -> None:
    asset_id = await _create_asset(client, ready_admin, "idem-svc-4")
    resp = await _post_incident(client, ready_admin, _payload(asset_id), "")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    resp2 = await _post_incident(client, ready_admin, _payload(asset_id), "k" * 201)
    assert resp2.status_code == 400


# ---- 跨用户隔离 ----


async def test_same_key_across_users_is_isolated(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    """隔离维度 = (user_id, endpoint, key)：不同用户使用相同 key 互不冲突。"""
    asset_id = await _create_asset(client, ready_admin, "idem-svc-5")
    await create_test_user(
        db_session_factory,
        username="idem_operator",
        password="IdemOp123!",
        role_codes=["operator"],
        policies=[{"environment": "sandbox", "permission": "investigate"}],
    )
    login = await api_login(client, "idem_operator", "IdemOp123!")
    operator = auth_headers(login.json()["data"]["access_token"])

    shared_key = str(uuid.uuid4())
    admin_resp = await _post_incident(
        client, ready_admin, _payload(asset_id, title="admin 的事件"), shared_key
    )
    op_resp = await _post_incident(
        client, operator, _payload(asset_id, title="operator 的事件"), shared_key
    )
    assert admin_resp.status_code == 201, admin_resp.text
    assert op_resp.status_code == 201, op_resp.text
    assert admin_resp.json()["data"]["id"] != op_resp.json()["data"]["id"]


# ---- 并发 ----


async def test_concurrent_same_key_yields_single_incident(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    """N 个同 key 同 body 并发请求：只创建 1 个事故，其余重放或安全冲突。"""
    asset_id = await _create_asset(client, ready_admin, "idem-svc-6")
    key = str(uuid.uuid4())
    payload = _payload(asset_id, title="并发幂等")

    responses = await asyncio.gather(
        *[_post_incident(client, ready_admin, payload, key) for _ in range(5)]
    )
    created = [r for r in responses if r.status_code == 201]
    replayed = [r for r in created if r.headers.get("Idempotency-Replayed") == "true"]
    assert len(created) >= 1
    assert len(created) + len([r for r in responses if r.status_code == 409]) == 5

    incident_ids = {r.json()["data"]["id"] for r in created}
    assert len(incident_ids) == 1  # 全部返回同一个事故

    async with db_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Incident)
                .where(Incident.primary_asset_id == asset_id)
            )
        ).scalar_one()
    assert count == 1
    # 至少一个"真创建"（无重放头），其余为重放
    assert len(replayed) == len(created) - 1


# ---- 失败不烧 key ----


async def test_failed_request_does_not_burn_key(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    """业务失败（404）整体回滚：同一 key 之后可用于成功请求。"""
    key = str(uuid.uuid4())

    failed = await _post_incident(client, ready_admin, _payload("as_doesnotexist"), key)
    assert failed.status_code == 404

    # 同 key + 成功语义的 body（与失败 body 不同，哈希不同）→ 正常创建
    asset_id = await _create_asset(client, ready_admin, "idem-svc-7")
    ok = await _post_incident(client, ready_admin, _payload(asset_id), key)
    assert ok.status_code == 201, ok.text
    assert ok.headers.get("Idempotency-Replayed") is None

    # 再用与成功请求一致的 body 重放 → 命中
    replay = await _post_incident(client, ready_admin, _payload(asset_id), key)
    assert replay.status_code == 201
    assert replay.headers.get("Idempotency-Replayed") == "true"
    assert replay.json()["data"]["id"] == ok.json()["data"]["id"]


async def test_only_completed_rows_persist_after_success(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    """成功请求后幂等记录 state=completed 且响应体可重放；received 为瞬态不留存。"""
    asset_id = await _create_asset(client, ready_admin, "idem-svc-8")
    key = str(uuid.uuid4())
    resp = await _post_incident(client, ready_admin, _payload(asset_id), key)
    assert resp.status_code == 201

    async with db_session_factory() as session:
        rows = (
            (await session.execute(select(ApiIdempotencyKey).where(ApiIdempotencyKey.key == key)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.state == "completed"
    assert row.response_status == 201
    assert row.response_body is not None
    assert row.response_body["data"]["id"] == resp.json()["data"]["id"]
    assert row.endpoint == endpoint_tag("POST", "/api/v1/incidents")


def test_request_hash_is_byte_level() -> None:
    assert hash_request_body(b"a=1") == hash_request_body(b"a=1")
    assert hash_request_body(b"a=1") != hash_request_body(b"a=2")

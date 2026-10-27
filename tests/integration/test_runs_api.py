"""诊断运行 API 集成测试（CONTRACTS §5 Runs / IMPLEMENTATION_PLAN §2.5 API 用例）。

覆盖：生命周期状态、202 受理、幂等（重放/异体冲突/缺 key）、
open 事件拒绝发起、取消语义、events JSON 分页（seq 连续）、
evidence/report 查询、越权 404 伪装、viewer 403。
"""

from __future__ import annotations

import uuid

import httpx

INCIDENTS = "/api/v1/incidents"
RUNS = "/api/v1/runs"


async def _create_asset(
    client: httpx.AsyncClient, headers: dict, name: str, env: str = "sandbox"
) -> str:
    resp = await client.post(
        "/api/v1/assets",
        json={"name": name, "environment": env, "asset_type": "service"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _create_incident(
    client: httpx.AsyncClient, headers: dict, asset_id: str, env: str = "sandbox"
) -> tuple[str, str]:
    resp = await client.post(
        INCIDENTS,
        json={
            "title": "run-api 测试事件",
            "environment": env,
            "primary_asset_id": asset_id,
            "severity": "medium",
        },
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 201, resp.text
    incident = resp.json()["data"]
    return incident["id"], incident["status"]


async def _ack(client: httpx.AsyncClient, headers: dict, incident_id: str) -> None:
    resp = await client.post(f"{INCIDENTS}/{incident_id}/acknowledge", headers=headers)
    assert resp.status_code == 200, resp.text


# ---- 创建 ----


async def test_create_run_requires_acknowledged_incident(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    asset_id = await _create_asset(client, ready_admin, "run-open-svc")
    incident_id, status = await _create_incident(client, ready_admin, asset_id)
    _ = status
    resp = await client.post(
        f"{INCIDENTS}/{incident_id}/investigations",
        headers={**ready_admin, "Idempotency-Key": str(uuid.uuid4())},
        json={},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT_STATE"


async def test_create_run_requires_idempotency_key(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    asset_id = await _create_asset(client, ready_admin, "run-nokey-svc")
    incident_id, _ = await _create_incident(client, ready_admin, asset_id)
    await _ack(client, ready_admin, incident_id)
    resp = await client.post(
        f"{INCIDENTS}/{incident_id}/investigations", headers=ready_admin, json={}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_create_run_202_and_incident_transitions(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    asset_id = await _create_asset(client, ready_admin, "run-202-svc")
    incident_id, _ = await _create_incident(client, ready_admin, asset_id)
    await _ack(client, ready_admin, incident_id)
    resp = await client.post(
        f"{INCIDENTS}/{incident_id}/investigations",
        headers={**ready_admin, "Idempotency-Key": str(uuid.uuid4())},
        json={},
    )
    assert resp.status_code == 202, resp.text
    data = resp.json()["data"]
    assert data["run_id"].startswith("run_")
    assert data["status"] == "queued"
    # incident: acknowledged → investigating
    detail = await client.get(f"{INCIDENTS}/{incident_id}", headers=ready_admin)
    assert detail.json()["data"]["status"] == "investigating"


async def test_create_run_idempotent_replay_and_mismatch(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    asset_id = await _create_asset(client, ready_admin, "run-idem-svc")
    incident_id, _ = await _create_incident(client, ready_admin, asset_id)
    await _ack(client, ready_admin, incident_id)
    key = str(uuid.uuid4())
    first = await client.post(
        f"{INCIDENTS}/{incident_id}/investigations",
        headers={**ready_admin, "Idempotency-Key": key},
        json={},
    )
    assert first.status_code == 202
    run_id = first.json()["data"]["run_id"]

    replay = await client.post(
        f"{INCIDENTS}/{incident_id}/investigations",
        headers={**ready_admin, "Idempotency-Key": key},
        json={},
    )
    assert replay.status_code == 202
    assert replay.headers.get("Idempotency-Replayed") == "true"
    assert replay.json()["data"]["run_id"] == run_id

    mismatch = await client.post(
        f"{INCIDENTS}/{incident_id}/investigations",
        headers={**ready_admin, "Idempotency-Key": key},
        json={"max_tool_calls": 5},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"


# ---- 查询 ----


async def test_get_run_shape(client: httpx.AsyncClient, ready_admin: dict) -> None:
    asset_id = await _create_asset(client, ready_admin, "run-shape-svc")
    incident_id, _ = await _create_incident(client, ready_admin, asset_id)
    await _ack(client, ready_admin, incident_id)
    created = await client.post(
        f"{INCIDENTS}/{incident_id}/investigations",
        headers={**ready_admin, "Idempotency-Key": str(uuid.uuid4())},
        json={"max_tool_calls": 3, "deadline_seconds": 60},
    )
    run_id = created.json()["data"]["run_id"]
    resp = await client.get(f"{RUNS}/{run_id}", headers=ready_admin)
    assert resp.status_code == 200
    run = resp.json()["data"]
    assert run["id"] == run_id
    assert run["incident_id"] == incident_id
    assert run["status"] == "queued"
    assert run["langgraph_thread_id"] == run_id
    assert run["budgets"]["max_tool_calls"] == 3
    assert run["model_config_snapshot"]["rag"] == "off"
    assert run["usage"] == {} or run["usage"] is not None


async def test_run_not_found_masked(client: httpx.AsyncClient, ready_admin: dict) -> None:
    resp = await client.get(f"{RUNS}/run_doesnotexist", headers=ready_admin)
    assert resp.status_code == 404


# ---- events（JSON 分页为默认）----


async def test_events_pagination_seq_monotonic(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    """无 Accept 头（默认 JSON）；seq 连续递增；after_seq 增量。"""
    from app.core.enums import RunEventType
    from app.domains.tools.context import RunEventWriter

    asset_id = await _create_asset(client, ready_admin, "run-events-svc")
    incident_id, _ = await _create_incident(client, ready_admin, asset_id)
    await _ack(client, ready_admin, incident_id)
    created = await client.post(
        f"{INCIDENTS}/{incident_id}/investigations",
        headers={**ready_admin, "Idempotency-Key": str(uuid.uuid4())},
        json={},
    )
    run_id = created.json()["data"]["run_id"]

    # 直接写 5 条事件
    writer = await RunEventWriter.create(db_session_factory, run_id)
    for i in range(5):
        await writer.append(RunEventType.ERROR, {"i": i})

    resp = await client.get(f"{RUNS}/{run_id}/events?after_seq=0&limit=3", headers=ready_admin)
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("application/json")
    data = resp.json()["data"]
    assert [e["seq"] for e in data["items"]] == [1, 2, 3]
    assert data["has_more"] is True
    assert data["next_after_seq"] == 3

    page2 = await client.get(
        f"{RUNS}/{run_id}/events?after_seq={data['next_after_seq']}&limit=10",
        headers=ready_admin,
    )
    data2 = page2.json()["data"]
    assert [e["seq"] for e in data2["items"]] == [4, 5]
    assert data2["has_more"] is False


# ---- 取消 ----


async def test_cancel_queued_run(client: httpx.AsyncClient, ready_admin: dict) -> None:
    asset_id = await _create_asset(client, ready_admin, "run-cancel-svc")
    incident_id, _ = await _create_incident(client, ready_admin, asset_id)
    await _ack(client, ready_admin, incident_id)
    created = await client.post(
        f"{INCIDENTS}/{incident_id}/investigations",
        headers={**ready_admin, "Idempotency-Key": str(uuid.uuid4())},
        json={},
    )
    run_id = created.json()["data"]["run_id"]
    resp = await client.post(f"{RUNS}/{run_id}/cancel", headers=ready_admin)
    assert resp.status_code == 200
    run = resp.json()["data"]
    assert run["status"] == "canceled"
    assert run["stop_reason"] == "canceled"
    # 终态后重复取消 → 409
    again = await client.post(f"{RUNS}/{run_id}/cancel", headers=ready_admin)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CONFLICT_STATE"


# ---- 越权与角色 ----


async def test_run_scope_masked_404(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    from tests.conftest import api_login, auth_headers, create_test_user

    asset_id = await _create_asset(client, ready_admin, "run-scope-svc")
    incident_id, _ = await _create_incident(client, ready_admin, asset_id)
    await _ack(client, ready_admin, incident_id)
    created = await client.post(
        f"{INCIDENTS}/{incident_id}/investigations",
        headers={**ready_admin, "Idempotency-Key": str(uuid.uuid4())},
        json={},
    )
    run_id = created.json()["data"]["run_id"]

    await create_test_user(
        db_session_factory,
        username="run_prod_reader",
        password="RunProd123!",
        role_codes=["operator"],
        policies=[{"environment": "production", "permission": "read"}],
    )
    login = await api_login(client, "run_prod_reader", "RunProd123!")
    headers = auth_headers(login.json()["data"]["access_token"])
    resp = await client.get(f"{RUNS}/{run_id}", headers=headers)
    assert resp.status_code == 404  # 越权伪装


async def test_viewer_cannot_create_run(
    client: httpx.AsyncClient, ready_admin: dict, viewer_headers: dict
) -> None:
    asset_id = await _create_asset(client, ready_admin, "run-viewer-svc")
    incident_id, _ = await _create_incident(client, ready_admin, asset_id)
    await _ack(client, ready_admin, incident_id)
    resp = await client.post(
        f"{INCIDENTS}/{incident_id}/investigations",
        headers={**viewer_headers, "Idempotency-Key": str(uuid.uuid4())},
        json={},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


async def test_report_404_before_completion(client: httpx.AsyncClient, ready_admin: dict) -> None:
    asset_id = await _create_asset(client, ready_admin, "run-report-svc")
    incident_id, _ = await _create_incident(client, ready_admin, asset_id)
    await _ack(client, ready_admin, incident_id)
    created = await client.post(
        f"{INCIDENTS}/{incident_id}/investigations",
        headers={**ready_admin, "Idempotency-Key": str(uuid.uuid4())},
        json={},
    )
    run_id = created.json()["data"]["run_id"]
    resp = await client.get(f"{RUNS}/{run_id}/report", headers=ready_admin)
    assert resp.status_code == 404

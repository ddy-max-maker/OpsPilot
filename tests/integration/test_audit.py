"""审计查询 API 集成测试：角色矩阵（Admin/Viewer 可读，Operator 拒绝）、过滤与分页。"""

from __future__ import annotations

import httpx

from tests.conftest import auth_headers, create_test_user

AUDIT = "/api/v1/audit-logs"
ASSETS = "/api/v1/assets"


async def test_audit_requires_reader_role(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    # 触发至少一条审计（创建资产）
    resp = await client.post(
        ASSETS,
        json={"name": "audit-svc", "environment": "sandbox", "asset_type": "service"},
        headers=ready_admin,
    )
    assert resp.status_code == 201

    # Admin 可读
    admin_list = await client.get(AUDIT, headers=ready_admin)
    assert admin_list.status_code == 200
    items = admin_list.json()["data"]["items"]
    assert len(items) >= 1
    create_rows = [i for i in items if i["action"] == "asset.create"]
    assert create_rows
    assert create_rows[0]["resource_type"] == "asset"
    assert create_rows[0]["actor_type"] == "user"
    assert create_rows[0]["request_id"]

    # Operator 拒绝（角色矩阵：admin/viewer 才可查询）
    await create_test_user(
        db_session_factory,
        username="audit_op",
        password="AuditOp123!",
        role_codes=["operator"],
        policies=[{"environment": "sandbox", "permission": "read"}],
    )
    login = await client.post(
        "/api/v1/auth/login", json={"username": "audit_op", "password": "AuditOp123!"}
    )
    operator = auth_headers(login.json()["data"]["access_token"])
    denied = await client.get(AUDIT, headers=operator)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "FORBIDDEN"


async def test_audit_viewer_role_allowed(client: httpx.AsyncClient, viewer_headers: dict) -> None:
    resp = await client.get(AUDIT, headers=viewer_headers)
    assert resp.status_code == 200
    # 无策略 viewer 没做过任何操作，但登录/首登改密会产生审计
    assert resp.json()["data"]["items"] is not None


async def test_audit_filters_and_pagination(client: httpx.AsyncClient, ready_admin: dict) -> None:
    for i in range(3):
        await client.post(
            ASSETS,
            json={"name": f"audit-pg-{i}", "environment": "sandbox", "asset_type": "service"},
            headers=ready_admin,
        )
    # actor_type 过滤
    by_actor = await client.get(AUDIT + "?actor_type=user", headers=ready_admin)
    assert by_actor.status_code == 200
    assert all(i["actor_type"] == "user" for i in by_actor.json()["data"]["items"])
    # action 过滤
    by_action = await client.get(AUDIT + "?action=asset.create", headers=ready_admin)
    actions = {i["action"] for i in by_action.json()["data"]["items"]}
    assert actions == {"asset.create"}
    # 分页游标
    page1 = await client.get(AUDIT + "?limit=2", headers=ready_admin)
    data1 = page1.json()["data"]
    assert len(data1["items"]) == 2
    assert data1["has_more"] is True
    assert data1["next_cursor"]
    page2 = await client.get(AUDIT + f"?limit=2&cursor={data1['next_cursor']}", headers=ready_admin)
    assert page2.status_code == 200
    ids1 = {i["id"] for i in data1["items"]}
    ids2 = {i["id"] for i in page2.json()["data"]["items"]}
    assert ids1.isdisjoint(ids2)  # 游标翻页无重复

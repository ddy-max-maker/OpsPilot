"""资产管理 API 集成测试：创建/查询/修改、RBAC、资产范围、分页、错误规范。"""

from __future__ import annotations

import httpx
from sqlalchemy import select

from app.models import AuditEvent
from tests.conftest import api_login, auth_headers, create_test_user

ASSETS = "/api/v1/assets"


def _asset_payload(name: str, environment: str = "sandbox", **extra: object) -> dict:
    payload: dict[str, object] = {
        "name": name,
        "environment": environment,
        "asset_type": "service",
        "criticality": "medium",
    }
    payload.update(extra)
    return payload


async def _create(
    client: httpx.AsyncClient, headers: dict, name: str, environment: str = "sandbox"
) -> httpx.Response:
    return await client.post(ASSETS, json=_asset_payload(name, environment), headers=headers)


# ---- 创建（Admin）----


async def test_admin_create_asset(client: httpx.AsyncClient, ready_admin: dict) -> None:
    resp = await client.post(
        ASSETS,
        json=_asset_payload(
            "order-api",
            "sandbox",
            endpoints={"health_url": "http://order-api/healthz"},
            owner="team-omega",
            tags=["http", "python"],
        ),
        headers=ready_admin,
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["id"].startswith("as_")
    assert data["name"] == "order-api"
    assert data["environment"] == "sandbox"
    assert data["asset_type"] == "service"
    assert data["status"] == "active"  # 默认值
    assert data["criticality"] == "medium"
    assert data["endpoints"]["health_url"] == "http://order-api/healthz"
    assert data["tags"] == ["http", "python"]
    assert data["created_at"].endswith("Z")


async def test_create_duplicate_name_environment_conflict(
    client: httpx.AsyncClient, ready_admin: dict
) -> None:
    first = await _create(client, ready_admin, "redis-cache", "sandbox")
    assert first.status_code == 201
    dup = await _create(client, ready_admin, "redis-cache", "sandbox")
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "CONFLICT_STATE"
    # 同名不同环境不冲突
    other_env = await _create(client, ready_admin, "redis-cache", "production")
    assert other_env.status_code == 201


async def test_create_validation_errors(client: httpx.AsyncClient, ready_admin: dict) -> None:
    # 非法 environment
    bad_env = await client.post(ASSETS, json=_asset_payload("x1", "prod"), headers=ready_admin)
    assert bad_env.status_code == 400
    assert bad_env.json()["error"]["code"] == "VALIDATION_ERROR"
    # 非法 asset_type
    bad_type = await client.post(
        ASSETS,
        json={"name": "x2", "environment": "sandbox", "asset_type": "vm"},
        headers=ready_admin,
    )
    assert bad_type.status_code == 400
    # 未知字段（extra=forbid）
    extra_field = await client.post(
        ASSETS,
        json={"name": "x3", "environment": "sandbox", "asset_type": "service", "subdomain": "x"},
        headers=ready_admin,
    )
    assert extra_field.status_code == 400


# ---- RBAC ----


async def test_operator_cannot_create_asset(
    client: httpx.AsyncClient, operator_headers: dict
) -> None:
    resp = await client.post(ASSETS, json=_asset_payload("nope"), headers=operator_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


async def test_viewer_cannot_create_asset(client: httpx.AsyncClient, viewer_headers: dict) -> None:
    resp = await client.post(ASSETS, json=_asset_payload("nope"), headers=viewer_headers)
    assert resp.status_code == 403


async def test_operator_cannot_patch_asset(
    client: httpx.AsyncClient, ready_admin: dict, operator_headers: dict
) -> None:
    created = await _create(client, ready_admin, "patch-target")
    asset_id = created.json()["data"]["id"]
    resp = await client.patch(
        ASSETS + f"/{asset_id}", json={"owner": "new-owner"}, headers=operator_headers
    )
    assert resp.status_code == 403  # 角色不足（先于资源解析，不泄漏存在性）


# ---- 列表与范围 ----


async def test_admin_lists_all_environments(client: httpx.AsyncClient, ready_admin: dict) -> None:
    await _create(client, ready_admin, "svc-a", "sandbox")
    await _create(client, ready_admin, "svc-b", "production")
    resp = await client.get(ASSETS, headers=ready_admin)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["items"]) == 2
    assert data["has_more"] is False
    assert data["next_cursor"] is None
    # environment 过滤
    resp_sb = await client.get(ASSETS + "?environment=sandbox", headers=ready_admin)
    names = [i["name"] for i in resp_sb.json()["data"]["items"]]
    assert names == ["svc-b"] or names == ["svc-a"]  # 排序不限定先建先后，仅校验集合
    sandbox_names = {i["name"] for i in resp_sb.json()["data"]["items"]}
    assert sandbox_names == {"svc-a"}


async def test_operator_scope_filters_list(
    client: httpx.AsyncClient, ready_admin: dict, operator_headers: dict
) -> None:
    """跨环境越权：sandbox 操作员只能看到 sandbox 资产；查 production 列表为空而非报错。"""
    await _create(client, ready_admin, "sb-svc", "sandbox")
    await _create(client, ready_admin, "pd-svc", "production")

    visible = await client.get(ASSETS, headers=operator_headers)
    assert visible.status_code == 200
    items = visible.json()["data"]["items"]
    assert [i["name"] for i in items] == ["sb-svc"]  # 只见 sandbox

    prod = await client.get(ASSETS + "?environment=production", headers=operator_headers)
    assert prod.status_code == 200
    assert prod.json()["data"]["items"] == []  # 空列表，不暴露 production 资产


async def test_default_deny_user_sees_nothing(
    client: httpx.AsyncClient, ready_admin: dict, viewer_headers: dict
) -> None:
    """无任何策略的 viewer：默认拒绝，列表为空。"""
    await _create(client, ready_admin, "any-svc", "sandbox")
    resp = await client.get(ASSETS, headers=viewer_headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["items"] == []


async def test_user_and_role_policy_union(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    """user 级 sandbox 策略 ∪ role 级 staging 策略。"""
    await _create(client, ready_admin, "sb-1", "sandbox")
    await _create(client, ready_admin, "st-1", "staging")
    await _create(client, ready_admin, "pd-1", "production")
    # role 级策略：operator 角色 -> staging
    from sqlalchemy import select as sa_select

    from app.models import AssetAccessPolicy, Role

    async with db_session_factory() as session:
        operator_role = (
            await session.execute(sa_select(Role).where(Role.code == "operator"))
        ).scalar_one()
        session.add(
            AssetAccessPolicy(
                id="pol_role_staging",
                user_id=None,
                role_id=operator_role.id,
                asset_id=None,
                environment="staging",
                permission="read",
            )
        )
        await session.commit()
    # user 级策略：sandbox
    await create_test_user(
        db_session_factory,
        username="op_union",
        password="Union123!",
        role_codes=["operator"],
        policies=[{"environment": "sandbox"}],
    )
    login = await api_login(client, "op_union", "Union123!")
    headers = auth_headers(login.json()["data"]["access_token"])
    resp = await client.get(ASSETS, headers=headers)
    names = {i["name"] for i in resp.json()["data"]["items"]}
    assert names == {"sb-1", "st-1"}  # 并集；production 不可见


async def test_single_asset_policy_grant(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    """user 级单资产策略：仅授权特定资产（跨环境单点放行）。"""
    sb = await _create(client, ready_admin, "sb-target", "sandbox")
    await _create(client, ready_admin, "sb-other", "sandbox")
    pd = await _create(client, ready_admin, "pd-target", "production")
    target_id = pd.json()["data"]["id"]
    await create_test_user(
        db_session_factory,
        username="asset_scoped",
        password="Scoped123!",
        role_codes=["viewer"],
        policies=[{"asset_id": target_id}],
    )
    login = await api_login(client, "asset_scoped", "Scoped123!")
    headers = auth_headers(login.json()["data"]["access_token"])
    resp = await client.get(ASSETS, headers=headers)
    items = resp.json()["data"]["items"]
    assert [i["id"] for i in items] == [target_id]
    _ = (sb, sb)  # sb 仅占位断言引用


# ---- cursor 分页 ----


async def test_pagination_cursor_walk(client: httpx.AsyncClient, ready_admin: dict) -> None:
    for i in range(5):
        resp = await _create(client, ready_admin, f"pg-svc-{i}")
        assert resp.status_code == 201

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        url = ASSETS + "?limit=2" + (f"&cursor={cursor}" if cursor else "")
        resp = await client.get(url, headers=ready_admin)
        assert resp.status_code == 200
        data = resp.json()["data"]
        seen.extend(i["id"] for i in data["items"])
        pages += 1
        assert len(data["items"]) <= 2
        if not data["has_more"]:
            assert data["next_cursor"] is None
            break
        assert data["next_cursor"]
        cursor = data["next_cursor"]
    assert pages == 3
    assert len(seen) == 5
    assert len(set(seen)) == 5  # 无重复无遗漏


async def test_pagination_limit_bounds(client: httpx.AsyncClient, ready_admin: dict) -> None:
    over = await client.get(ASSETS + "?limit=201", headers=ready_admin)
    assert over.status_code == 400
    assert over.json()["error"]["code"] == "VALIDATION_ERROR"
    zero = await client.get(ASSETS + "?limit=0", headers=ready_admin)
    assert zero.status_code == 400


async def test_pagination_tampered_cursor(client: httpx.AsyncClient, ready_admin: dict) -> None:
    # 两个资产 + limit=1 才有第一页游标
    await _create(client, ready_admin, "cursor-svc-a")
    await _create(client, ready_admin, "cursor-svc-b")
    resp = await client.get(ASSETS + "?limit=1", headers=ready_admin)
    cursor = resp.json()["data"]["next_cursor"]
    assert cursor is not None
    mid = len(cursor) // 2
    tampered = cursor[:mid] + ("0" if cursor[mid] != "0" else "1") + cursor[mid + 1 :]
    assert tampered != cursor
    bad = await client.get(ASSETS, params={"cursor": tampered}, headers=ready_admin)
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
    garbage = await client.get(ASSETS, params={"cursor": "%%%garbage%%%"}, headers=ready_admin)
    assert garbage.status_code == 400


# ---- 修改（PATCH）----


async def test_patch_asset_updates_fields(client: httpx.AsyncClient, ready_admin: dict) -> None:
    created = await _create(client, ready_admin, "patch-me", "sandbox")
    asset_id = created.json()["data"]["id"]
    resp = await client.patch(
        ASSETS + f"/{asset_id}",
        json={
            "owner": "team-beta",
            "criticality": "high",
            "tags": ["redis"],
            "endpoints": {"health_url": "http://patched/healthz"},
            "status": "retired",
        },
        headers=ready_admin,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["owner"] == "team-beta"
    assert data["criticality"] == "high"
    assert data["tags"] == ["redis"]
    assert data["status"] == "retired"
    assert data["endpoints"]["health_url"] == "http://patched/healthz"
    # 未提供字段不变
    assert data["name"] == "patch-me"
    assert data["environment"] == "sandbox"


async def test_patch_nonexistent_asset_404(client: httpx.AsyncClient, ready_admin: dict) -> None:
    resp = await client.patch(ASSETS + "/as_doesnotexist", json={"owner": "x"}, headers=ready_admin)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


async def test_patch_to_conflicting_name_409(client: httpx.AsyncClient, ready_admin: dict) -> None:
    a = await _create(client, ready_admin, "conflict-a", "sandbox")
    b = await _create(client, ready_admin, "conflict-b", "sandbox")
    b_id = b.json()["data"]["id"]
    resp = await client.patch(ASSETS + f"/{b_id}", json={"name": "conflict-a"}, headers=ready_admin)
    assert resp.status_code == 409
    _ = a


# ---- 审计 ----


async def test_asset_write_audited(
    client: httpx.AsyncClient, ready_admin: dict, db_session_factory
) -> None:
    created = await _create(client, ready_admin, "audited-svc", "sandbox")
    asset_id = created.json()["data"]["id"]
    await client.patch(ASSETS + f"/{asset_id}", json={"owner": "audit-team"}, headers=ready_admin)
    async with db_session_factory() as session:
        actions = (
            (
                await session.execute(
                    select(AuditEvent.action)
                    .where(AuditEvent.resource_id == asset_id)
                    .order_by(AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )
    assert actions == ["asset.create", "asset.update"]

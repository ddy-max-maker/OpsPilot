"""纵向业务链路集成测试（D1-A 验收主链路）：

启动系统(bootstrap) -> 初始化 Admin -> 登录认证 -> 权限验证(403 拦截/放行)
-> 创建资产 -> PostgreSQL 持久化(直查验证) -> 查询资产。
"""

from __future__ import annotations

import httpx
from sqlalchemy import select

from app.models import Asset
from tests.conftest import ADMIN_BOOT_PASSWORD, ADMIN_BOOT_USERNAME, api_login, auth_headers

ASSETS = "/api/v1/assets"


async def test_full_vertical_chain(client: httpx.AsyncClient, db_session_factory) -> None:
    # 0) 就绪检查（数据库连通 = 系统可用）
    ready = await client.get("/api/v1/health/ready")
    assert ready.status_code == 200

    # 1) bootstrap Admin 已由应用 lifespan 创建；登录（首登 must_change_password=True）
    login = await api_login(client, ADMIN_BOOT_USERNAME, ADMIN_BOOT_PASSWORD)
    assert login.status_code == 200
    token = login.json()["data"]["access_token"]
    headers = auth_headers(token)

    # 2) 未改密 -> 业务接口被拦截
    blocked = await client.get(ASSETS, headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"

    # 3) 首登改密 -> 解除拦截（D6：改密响应携带重新签发的新 token；旧 token 失效）
    changed = await client.post(
        "/api/v1/auth/change-password",
        json={"old_password": ADMIN_BOOT_PASSWORD, "new_password": "Vertical123!"},
        headers=headers,
    )
    assert changed.status_code == 200
    headers = auth_headers(changed.json()["data"]["access_token"])

    # 4) 创建资产（Admin 权限放行）
    created = await client.post(
        ASSETS,
        json={
            "name": "vertical-redis",
            "environment": "staging",
            "asset_type": "cache",
            "criticality": "high",
            "owner": "platform-team",
            "endpoints": {"health_url": "http://vertical-redis:6379/ping"},
            "tags": ["redis", "staging"],
        },
        headers=headers,
    )
    assert created.status_code == 201
    asset_id = created.json()["data"]["id"]
    assert asset_id.startswith("as_")

    # 5) PostgreSQL 真实持久化验证（绕过 API 直查数据库）
    async with db_session_factory() as session:
        row = (await session.execute(select(Asset).where(Asset.id == asset_id))).scalar_one()
    assert row.name == "vertical-redis"
    assert row.environment == "staging"
    assert row.asset_type == "cache"
    assert row.criticality == "high"
    assert row.endpoints == {"health_url": "http://vertical-redis:6379/ping"}

    # 6) 查询资产（同一 token；范围 = Admin 全量）
    listed = await client.get(ASSETS + "?environment=staging", headers=headers)
    assert listed.status_code == 200
    items = listed.json()["data"]["items"]
    assert [i["id"] for i in items] == [asset_id]

    # 7) 修改资产 -> 持久化复核
    patched = await client.patch(
        ASSETS + f"/{asset_id}", json={"status": "retired"}, headers=headers
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["status"] == "retired"
    async with db_session_factory() as session:
        row2 = (await session.execute(select(Asset).where(Asset.id == asset_id))).scalar_one()
    assert row2.status == "retired"

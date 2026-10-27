"""认证 API 集成测试：登录成败、首登改密、Token 校验、/auth/me。"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.security import create_access_token
from app.models import AuditEvent
from tests.conftest import (
    ADMIN_BOOT_PASSWORD,
    ADMIN_BOOT_USERNAME,
    api_login,
    auth_headers,
    create_test_user,
)

LOGIN_PATH = "/api/v1/auth/login"


def _envelope(resp: httpx.Response) -> dict:
    body = resp.json()
    assert body["request_id"].startswith("req_")
    return body


# ---- 登录成功 / 失败 ----


async def test_login_success_returns_token(client: httpx.AsyncClient) -> None:
    resp = await api_login(client, ADMIN_BOOT_USERNAME, ADMIN_BOOT_PASSWORD)
    assert resp.status_code == 200
    body = _envelope(resp)
    assert body["error"] is None
    data = body["data"]
    assert data["access_token"]
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == 1800  # 契约：30 分钟
    assert data["user_id"].startswith("usr_")
    assert data["username"] == ADMIN_BOOT_USERNAME
    assert data["must_change_password"] is True  # 引导账号首登强制改密


async def test_login_wrong_password_unauthorized(client: httpx.AsyncClient) -> None:
    resp = await api_login(client, ADMIN_BOOT_USERNAME, "WrongPassword1!")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_login_unknown_user_same_error_as_wrong_password(client: httpx.AsyncClient) -> None:
    """不存在账号与错误密码返回完全一致的错误（不泄漏账号存在性）。"""
    wrong_pw = await api_login(client, ADMIN_BOOT_USERNAME, "WrongPassword1!")
    unknown = await api_login(client, "no_such_user", "Whatever123!")
    assert unknown.status_code == wrong_pw.status_code == 401
    assert unknown.json()["error"] == wrong_pw.json()["error"]


async def test_login_system_account_rejected(client: httpx.AsyncClient) -> None:
    """预置系统账号禁止登录。"""
    resp = await api_login(client, "system_alerts", "AnyPassword123!")
    assert resp.status_code == 401


async def test_login_inactive_user_rejected(client: httpx.AsyncClient, db_session_factory) -> None:
    await create_test_user(
        db_session_factory,
        username="disabled1",
        password="Disabled123!",
        role_codes=["viewer"],
        is_active=False,
    )
    resp = await api_login(client, "disabled1", "Disabled123!")
    assert resp.status_code == 401


async def test_login_validation_error(client: httpx.AsyncClient) -> None:
    resp = await client.post(LOGIN_PATH, json={"username": ""})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_login_success_audited(client: httpx.AsyncClient, db_session_factory) -> None:
    await api_login(client, ADMIN_BOOT_USERNAME, ADMIN_BOOT_PASSWORD)
    async with db_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "auth.login")
            )
        ).scalar_one()
    assert count == 1


# ---- GET /auth/me ----


async def test_me_without_token_unauthenticated(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_me_with_garbage_token_unauthenticated(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me", headers=auth_headers("not-a-jwt"))
    assert resp.status_code == 401


async def test_me_returns_roles_and_scope(client: httpx.AsyncClient) -> None:
    login = await api_login(client, ADMIN_BOOT_USERNAME, ADMIN_BOOT_PASSWORD)
    token = login.json()["data"]["access_token"]
    resp = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["username"] == ADMIN_BOOT_USERNAME
    assert "admin" in data["roles"]
    assert data["must_change_password"] is True
    assert data["asset_scope"]["is_admin"] is True
    assert sorted(data["asset_scope"]["environments"]) == ["production", "sandbox", "staging"]


async def test_expired_token_rejected(client: httpx.AsyncClient) -> None:
    login = await api_login(client, ADMIN_BOOT_USERNAME, ADMIN_BOOT_PASSWORD)
    token = login.json()["data"]["access_token"]  # 有效账号，用于过审依赖
    assert token
    settings = get_settings()
    expired, _ = create_access_token("usr_expired", settings.jwt_secret, -1)
    resp = await client.get("/api/v1/auth/me", headers=auth_headers(expired))
    assert resp.status_code == 401


async def test_token_with_wrong_secret_rejected(client: httpx.AsyncClient) -> None:
    forged, _ = create_access_token("usr_forged", "attacker-secret", 30)
    resp = await client.get("/api/v1/auth/me", headers=auth_headers(forged))
    assert resp.status_code == 401


# ---- 首登强制改密 ----


async def test_must_change_password_blocks_business_api(client: httpx.AsyncClient) -> None:
    """首登未改密：业务接口 403 PASSWORD_CHANGE_REQUIRED；/auth/me 仍可访问。"""
    login = await api_login(client, ADMIN_BOOT_USERNAME, ADMIN_BOOT_PASSWORD)
    token = login.json()["data"]["access_token"]
    resp = await client.get("/api/v1/assets", headers=auth_headers(token))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"
    me = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert me.status_code == 200


async def test_change_password_flow(client: httpx.AsyncClient, db_session_factory) -> None:
    login = await api_login(client, ADMIN_BOOT_USERNAME, ADMIN_BOOT_PASSWORD)
    token = login.json()["data"]["access_token"]
    headers = auth_headers(token)

    # 旧密码错误 -> 400
    wrong = await client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "NotTheOld1!", "new_password": "NewSecure123!"},
        headers=headers,
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"]["code"] == "VALIDATION_ERROR"

    # 新密码过短 -> 400
    short = await client.post(
        "/api/v1/auth/change-password",
        json={"old_password": ADMIN_BOOT_PASSWORD, "new_password": "short"},
        headers=headers,
    )
    assert short.status_code == 400

    # 新旧相同 -> 400
    same = await client.post(
        "/api/v1/auth/change-password",
        json={"old_password": ADMIN_BOOT_PASSWORD, "new_password": ADMIN_BOOT_PASSWORD},
        headers=headers,
    )
    assert same.status_code == 400

    # 改密成功
    ok_resp = await client.post(
        "/api/v1/auth/change-password",
        json={"old_password": ADMIN_BOOT_PASSWORD, "new_password": "NewSecure123!"},
        headers=headers,
    )
    assert ok_resp.status_code == 200

    # 旧密码登录失败，新密码登录成功
    old_login = await api_login(client, ADMIN_BOOT_USERNAME, ADMIN_BOOT_PASSWORD)
    assert old_login.status_code == 401
    new_login = await api_login(client, ADMIN_BOOT_USERNAME, "NewSecure123!")
    assert new_login.status_code == 200

    # 原 token 立即可用（must_change_password 已清除）
    assets = await client.get("/api/v1/assets", headers=headers)
    assert assets.status_code == 200

    # 改密审计
    async with db_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "auth.change_password")
            )
        ).scalar_one()
    assert count == 1


async def test_change_password_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "whatever1!", "new_password": "Whatever2!"},
    )
    assert resp.status_code == 401


# ---- 响应包裹规范 ----


async def test_envelope_shape_on_error(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/no-such-route")
    assert resp.status_code == 404
    body = resp.json()
    assert body["data"] is None
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["request_id"].startswith("req_")


@pytest.mark.parametrize(
    ("path", "method"),
    [("/api/v1/health/live", "GET"), ("/api/v1/health/ready", "GET")],
)
async def test_unknown_route_envelope(client: httpx.AsyncClient, path: str, method: str) -> None:
    """契约端点存在性冒烟：不在此断言健康内容，仅防误删路由。"""
    resp = await client.request(method, "/api/v1/__not_exist__")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"

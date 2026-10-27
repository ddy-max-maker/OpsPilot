"""数据库约束与事务回滚集成测试（直连测试库，不走 API）。

覆盖：唯一约束（assets/users/alerts）、CHECK（策略互斥/自环）、
incidents 局部唯一索引、事务回滚原子性。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import new_id
from app.core.security import hash_password
from app.models import (
    Alert,
    Asset,
    AssetAccessPolicy,
    AssetEdge,
    Incident,
    Role,
    User,
    UserRole,
)

FUTURE = datetime(2262, 1, 1, tzinfo=UTC)


async def _new_asset(session: AsyncSession, name: str, environment: str = "sandbox") -> Asset:
    asset = Asset(
        id=new_id("as_"),
        name=name,
        environment=environment,
        asset_type="service",
        status="active",
        criticality="medium",
        owner="test",
        tags=[],
    )
    session.add(asset)
    await session.flush()
    return asset


async def _new_user(session: AsyncSession, username: str) -> User:
    user = User(
        id=new_id("usr_"),
        username=username,
        email=f"{username}@test.local",
        password_hash=hash_password("AnyPass123!"),
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    await session.flush()
    return user


# ---- 唯一约束 ----


async def test_assets_unique_name_environment(db_session: AsyncSession) -> None:
    await _new_asset(db_session, "dup-asset", "sandbox")
    with pytest.raises(IntegrityError):
        await _new_asset(db_session, "dup-asset", "sandbox")
    await db_session.rollback()
    # 同名不同环境合法
    await _new_asset(db_session, "dup-asset", "staging")
    await db_session.commit()


async def test_users_unique_username(db_session: AsyncSession) -> None:
    await _new_user(db_session, "dup-user")
    with pytest.raises(IntegrityError):
        await _new_user(db_session, "dup-user")
    await db_session.rollback()


async def test_alerts_unique_delivery(db_session: AsyncSession) -> None:
    """(connector_id, external_event_id) 唯一 -> D1-B Webhook 去重的 DB 基础。"""
    asset = await _new_asset(db_session, "alert-host")
    incident = Incident(
        id=new_id("inc_"),
        title="alert dedup test",
        environment="sandbox",
        primary_asset_id=asset.id,
        source="manual",
        severity="high",
        status="open",
        created_by=(await _new_user(db_session, "alert-user")).id,
    )
    db_session.add(incident)
    await db_session.flush()
    for _ in range(2):
        db_session.add(
            Alert(
                id=new_id("alr_"),
                connector_id="webhook_default",
                external_event_id="delivery-001",
                fingerprint="fp-1",
                title="dup alert",
                severity="high",
                payload={"x": 1},
                status="received",
                incident_id=incident.id,
                received_at=datetime.now(UTC),
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_incidents_partial_unique_active_fingerprint(db_session: AsyncSession) -> None:
    """活跃单同 (primary_asset, fingerprint) 唯一；resolved 后可再建。"""
    asset = await _new_asset(db_session, "inc-asset")
    user = await _new_user(db_session, "inc-user")
    # rollback 会过期 ORM 实例，先取纯字符串避免异步懒加载
    asset_id, user_id = asset.id, user.id
    # 先持久化 asset/user：后续 rollback 只回滚 incident，外键仍有效
    await db_session.commit()

    async def _incident(status: str) -> Incident:
        return Incident(
            id=new_id("inc_"),
            title=f"inc {status}",
            environment="sandbox",
            primary_asset_id=asset_id,
            source="manual",
            fingerprint="fp-dup",
            severity="medium",
            status=status,
            created_by=user_id,
        )

    first = await _incident("open")
    db_session.add(first)
    await db_session.flush()
    with pytest.raises(IntegrityError):
        db_session.add(await _incident("investigating"))
        await db_session.flush()
    await db_session.rollback()
    # resolved 同指纹不违反局部唯一索引
    db_session.add(await _incident("open"))
    db_session.add(await _incident("resolved"))
    await db_session.flush()
    db_session.add(await _incident("monitoring"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# ---- CHECK 约束 ----


async def test_policy_subject_exclusive(db_session: AsyncSession) -> None:
    """asset_access_policies：user_id 与 role_id 必须二选一。"""
    user = await _new_user(db_session, "pol-user")
    role = (await db_session.execute(select(Role).where(Role.code == "viewer"))).scalar_one()
    # rollback 会过期 ORM 实例，先取纯字符串避免异步懒加载
    user_id, role_id = user.id, role.id
    # 同时提供 -> 拒绝
    db_session.add(
        AssetAccessPolicy(
            id=new_id("pol_"),
            user_id=user_id,
            role_id=role_id,
            asset_id=None,
            environment="sandbox",
            permission="read",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
    # 都不提供 -> 拒绝
    db_session.add(
        AssetAccessPolicy(
            id=new_id("pol_"), user_id=None, role_id=None, environment=None, permission="read"
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
    # 仅 role -> 合法
    db_session.add(
        AssetAccessPolicy(
            id=new_id("pol_"),
            user_id=None,
            role_id=role_id,
            asset_id=None,
            environment="staging",
            permission="read",
        )
    )
    await db_session.commit()


async def test_asset_edge_no_self_loop(db_session: AsyncSession) -> None:
    asset = await _new_asset(db_session, "self-loop")
    db_session.add(
        AssetEdge(
            id=new_id("edge_"), from_asset_id=asset.id, to_asset_id=asset.id, relation="serves"
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_user_roles_fk_cascade(db_session: AsyncSession) -> None:
    """user_roles 依赖真实外键（users/roles 均存在性校验）。"""
    db_session.add(UserRole(user_id="usr_missing", role_id="rol_admin"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# ---- 事务回滚原子性 ----


async def test_transaction_rollback_keeps_db_clean(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """flush 后主动回滚：数据不留存。"""
    async with db_session_factory() as session:
        await _new_asset(session, "rollback-asset")
        await session.rollback()
        found = (
            await session.execute(select(Asset).where(Asset.name == "rollback-asset"))
        ).scalar_one_or_none()
    assert found is None


async def test_service_conflict_leaves_no_partial_state(
    client, ready_admin: dict, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """重复创建 409 后：资产与审计均无半成品（服务层事务边界原子性）。"""
    from app.models import AuditEvent

    payload = {"name": "atomic-svc", "environment": "sandbox", "asset_type": "service"}
    first = await client.post("/api/v1/assets", json=payload, headers=ready_admin)
    assert first.status_code == 201
    second = await client.post("/api/v1/assets", json=payload, headers=ready_admin)
    assert second.status_code == 409
    async with db_session_factory() as session:
        assets = (
            (await session.execute(select(Asset).where(Asset.name == "atomic-svc"))).scalars().all()
        )
        audit_rows = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.resource_id == first.json()["data"]["id"])
                )
            )
            .scalars()
            .all()
        )
    assert len(assets) == 1  # 仅首次创建成功
    assert [a.action for a in audit_rows] == ["asset.create"]  # 失败尝试未留下审计半成品

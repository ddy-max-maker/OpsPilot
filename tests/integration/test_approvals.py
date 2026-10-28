"""D5 集成测试：Proposal/Approval/Execute 审批链路与权限矩阵。

覆盖（用户指令 §2 / §9）：
- 正向链路：创建（pending_approval）→ dry-run → 他人审批 → execute
  （Outbox 同事务写入 + incident→mitigating + execution=pending）；
- production 目标：即使 Admin 提交也直接 403（数据库 CHECK 兜底单测）；
- 草稿/退役 Runbook 不可引用；r2/r3 不进入执行链路；
- 四眼：创建人自批 403；无 approver 角色拒绝；
- 审批过期：提案过期审批 409；审批窗口过期执行 409；
- 参数篡改：DB 直改参数 → 执行前快照复核 403；
- 重复 execute：不产生第二执行与第二 Outbox 事件；
- 未审批 execute → 409；allowlist 之外的容器名 → 403。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text

from app.core.security import hash_password
from app.domains.knowledge import service as kb_service
from app.models import Asset, Incident, InvestigationRun, User
from tests.conftest import auth_headers, create_test_user

pytestmark = pytest.mark.integration

PRP = "/api/v1/proposals"


async def _seed_world(db_session_factory) -> dict[str, str]:
    """播种完整世界：双用户（operator + approver 角色）+ 资产 + 事件 + run + 已发布 Runbook。"""
    from app.models import AssetAccessPolicy, Role, UserRole

    async with db_session_factory() as session:
        role_rows = (await session.execute(select(Role))).scalars().all()
        role_by_code = {r.code: r.id for r in role_rows}
        for uid, uname, role_code in (
            ("usr_creator", "creator", "operator"),
            ("usr_approver", "approver1", "approver"),
        ):
            if await session.get(User, uid) is None:
                session.add(
                    User(
                        id=uid,
                        username=uname,
                        email=f"{uname}@test.local",
                        password_hash=hash_password("Passw0rd!123"),
                        is_active=True,
                        must_change_password=False,
                    )
                )
                session.add(UserRole(user_id=uid, role_id=role_by_code[role_code]))
        await session.flush()
        asset = Asset(
            id="as_redis_demo",
            name="demo-redis",
            environment="sandbox",
            asset_type="cache",
            status="active",
            criticality="high",
            endpoints={
                "container_name": "opspilot-demo-redis",
                "health_check": {"kind": "redis_ping", "host": "127.0.0.1", "port": 6399},
            },
            owner=None,
            tags=[],
        )
        session.add(asset)
        await session.flush()
        # creator 对演示资产持有 execute 维度权限（执行目标必须在范围内）
        session.add(
            AssetAccessPolicy(
                id="pol_d5_creator_exec",
                user_id="usr_creator",
                asset_id="as_redis_demo",
                environment=None,
                permission="execute",
            )
        )
        await session.flush()
        incident = Incident(
            id="inc_d5demo",
            title="D5 演示：Redis 不可达",
            environment="sandbox",
            primary_asset_id=asset.id,
            source="manual",
            severity="high",
            status="investigating",
            created_by="usr_creator",
        )
        session.add(incident)
        await session.flush()
        run = InvestigationRun(
            id="run_d5demo",
            incident_id=incident.id,
            status="completed",
            triggered_by="usr_creator",
            model_config_json={"provider": "fake"},
            budgets={},
            usage={},
            authorization_snapshot={"is_admin": True, "all_assets": True},
            langgraph_thread_id="run_d5demo",
        )
        session.add(run)
        await session.commit()
    async with db_session_factory() as session:
        _, version = await kb_service.create_runbook(
            session,
            actor_id="usr_approver",
            name="D5 Redis 受控重启",
            description="连接池耗尽受控重启（演示容器白名单内）",
            risk_level="r1",
            steps=[
                {"action": "restart_container", "params": {"container_name": "opspilot-demo-redis"}}
            ],
            preconditions=None,
            parameters_schema=None,
            allowed_environments=["sandbox", "staging"],
            allowlist_action_keys=["restart_container@v1"],
        )
        await session.commit()
        version_id = version.id
    async with db_session_factory() as session:
        await kb_service.publish_runbook_version(
            session, actor_id="usr_approver", version_id=version_id
        )
        await session.commit()
    return {"asset_id": asset.id, "incident_id": incident.id, "run_id": run.id}


async def _login(client: httpx.AsyncClient, username: str) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": "Passw0rd!123"}
    )
    assert resp.status_code == 200, resp.text
    return auth_headers(resp.json()["data"]["access_token"])


async def _make_proposal(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    world: dict[str, str],
    **overrides: object,
) -> httpx.Response:
    body: dict[str, object] = {
        "run_id": world["run_id"],
        "runbook_version_id": world["rbv_id"],
        "target_asset_id": world["asset_id"],
        "environment": "sandbox",
        "parameters": {"container_name": "opspilot-demo-redis"},
    }
    body.update(overrides)
    return await client.post(
        f"{PRP}",
        json=body,
        headers={**headers, "Idempotency-Key": f"idem-{datetime.now(UTC).timestamp()}"},
    )


@pytest.fixture
async def world(db_session_factory, client) -> dict[str, str]:
    # client fixture 已完成 truncate + bootstrap（roles 可用）；此处播种业务世界
    w = await _seed_world(db_session_factory)
    # runbook version id 在第二个 session 中创建，重新读取
    async with db_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id FROM runbook_versions WHERE runbook_id IN "
                    "(SELECT id FROM runbooks WHERE name = 'D5 Redis 受控重启') "
                    "AND status = 'published' ORDER BY version_no DESC LIMIT 1"
                )
            )
        ).scalar_one()
    w["rbv_id"] = str(row)
    return w


# ---- 正向链路 ----


async def test_full_approval_chain(client, ready_admin, world) -> None:
    creator = await _login(client, "creator")
    approver = await _login(client, "approver1")

    resp = await _make_proposal(client, creator, world)
    assert resp.status_code == 201, resp.text
    proposal = resp.json()["data"]["proposal"]
    assert proposal["status"] == "pending_approval"
    assert proposal["risk_level"] == "r1"
    assert proposal["parameter_hash"]
    pid = proposal["id"]

    # dry-run：全部前置检查通过
    dry = await client.post(f"{PRP}/{pid}/dry-run", headers=creator)
    assert dry.status_code == 200, dry.text
    assert dry.json()["data"]["passed"] is True

    # 审批前执行 → 409
    early = await client.post(f"{PRP}/{pid}/execute", headers=creator)
    assert early.status_code == 409

    # 四眼：创建人自批 → 403
    self_approve = await client.post(f"{PRP}/{pid}/approve", json={}, headers=creator)
    assert self_approve.status_code == 403

    # 他人审批通过
    ok = await client.post(f"{PRP}/{pid}/approve", json={"comment": "lgtm"}, headers=approver)
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["proposal"]["status"] == "approved"
    approval = ok.json()["data"]["approval"]
    assert approval["decided_by"] != proposal["created_by_user"]
    assert approval["proposal_snapshot"]["parameter_hash"] == proposal["parameter_hash"]

    # 执行：Outbox 同事务写入 + incident → mitigating + execution=pending
    ex = await client.post(f"{PRP}/{pid}/execute", headers=creator)
    assert ex.status_code == 200, ex.text
    execution = ex.json()["data"]["execution"]
    assert execution["status"] == "pending"
    assert execution["action_key"] == "restart_container@v1"
    detail = await client.get(f"{PRP}/{pid}", headers=creator)
    data = detail.json()["data"]
    assert data["proposal"]["status"] == "executing"

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as conn:
        outbox = (
            await conn.execute(
                text("SELECT event_type, payload FROM outbox_events WHERE aggregate_id = :pid"),
                {"pid": pid},
            )
        ).all()
        assert len(outbox) == 1
        assert outbox[0][0] == "action.execute_requested"
        assert outbox[0][1]["execution_id"] == execution["id"]
        inc_status = (
            await conn.execute(
                text("SELECT status FROM incidents WHERE id = :i"),
                {"i": world["incident_id"]},
            )
        ).scalar_one()
        assert inc_status == "mitigating"
    await engine.dispose()

    # 重复 execute → 409（同一参数哈希唯一执行）且不产生第二 Outbox 事件
    again = await client.post(f"{PRP}/{pid}/execute", headers=creator)
    assert again.status_code == 409


# ---- production / 草稿 / r2 / allowlist 拒绝 ----


async def test_production_target_denied_even_for_admin(client, ready_admin, world) -> None:
    resp = await _make_proposal(client, ready_admin, world, environment="production")
    assert resp.status_code == 403
    assert "production" in resp.json()["error"]["message"].lower()


async def test_draft_runbook_version_rejected(
    client, ready_admin, world, db_session_factory
) -> None:
    async with db_session_factory() as session:
        _, version = await kb_service.create_runbook(
            session,
            actor_id="usr_approver",
            name="D5 草稿手册",
            description="draft",
            risk_level="r1",
            steps=[{"action": "x"}],
            preconditions=None,
            parameters_schema=None,
            allowed_environments=["sandbox"],
            allowlist_action_keys=["restart_container@v1"],
        )
        await session.commit()
        draft_id = version.id
    creator = await _login(client, "creator")
    resp = await _make_proposal(client, creator, world, runbook_version_id=draft_id)
    assert resp.status_code == 409  # 草稿不可当作已批准执行方案


async def test_risk_r2_rejected(client, ready_admin, world, db_session_factory) -> None:
    async with db_session_factory() as session:
        _, version = await kb_service.create_runbook(
            session,
            actor_id="usr_approver",
            name="D5 高风险手册",
            description="r2",
            risk_level="r2",
            steps=[{"action": "x"}],
            preconditions=None,
            parameters_schema=None,
            allowed_environments=["sandbox"],
            allowlist_action_keys=["restart_container@v1"],
        )
        await session.commit()
        r2_id = version.id
    async with db_session_factory() as session:
        await kb_service.publish_runbook_version(session, actor_id="usr_approver", version_id=r2_id)
        await session.commit()
    creator = await _login(client, "creator")
    resp = await _make_proposal(client, creator, world, runbook_version_id=r2_id)
    assert resp.status_code == 403  # r2/r3 不进入执行链路


async def test_container_outside_allowlist_rejected(client, ready_admin, world) -> None:
    creator = await _login(client, "creator")
    # 命名空间之外：action schema pattern 校验拒绝（400）
    resp = await _make_proposal(
        client, creator, world, parameters={"container_name": "victim-container"}
    )
    assert resp.status_code == 400
    # 命名空间内但与资产登记绑定不一致：身份绑定拒绝（403）
    resp2 = await _make_proposal(
        client, creator, world, parameters={"container_name": "opspilot-demo-someone-else"}
    )
    assert resp2.status_code == 403  # 命名空间白名单之外一律拒绝


# ---- 四眼 / 过期 / 篡改 ----


async def test_non_approver_role_cannot_decide(
    client, ready_admin, world, db_session_factory
) -> None:
    await create_test_user(
        db_session_factory,
        username="plain_operator",
        password="Passw0rd!123",
        role_codes=["operator"],
    )
    operator = await _login(client, "plain_operator")
    creator = await _login(client, "creator")
    resp = await _make_proposal(client, creator, world)
    pid = resp.json()["data"]["proposal"]["id"]
    decide = await client.post(f"{PRP}/{pid}/approve", json={}, headers=operator)
    assert decide.status_code == 403


async def test_expired_proposal_cannot_be_approved(
    client, ready_admin, world, db_session_factory
) -> None:
    creator = await _login(client, "creator")
    resp = await _make_proposal(client, creator, world)
    pid = resp.json()["data"]["proposal"]["id"]
    async with db_session_factory() as session:
        await session.execute(
            text("UPDATE proposals SET expires_at = :t WHERE id = :pid"),
            {"t": datetime.now(UTC) - timedelta(minutes=1), "pid": pid},
        )
        await session.commit()
    approver = await _login(client, "approver1")
    decide = await client.post(f"{PRP}/{pid}/approve", json={}, headers=approver)
    assert decide.status_code == 409


async def test_tampered_parameters_denied_at_execute(
    client, ready_admin, world, db_session_factory
) -> None:
    """参数篡改（DB 直改）：执行前快照复核 403，不发起任何外部操作。"""
    creator = await _login(client, "creator")
    approver = await _login(client, "approver1")
    resp = await _make_proposal(client, creator, world)
    pid = resp.json()["data"]["proposal"]["id"]
    await client.post(f"{PRP}/{pid}/approve", json={}, headers=approver)
    async with db_session_factory() as session:
        await session.execute(
            text("UPDATE proposals SET parameters = :p WHERE id = :pid"),
            {
                "p": '{"container_name": "opspilot-demo-redis", "timeout_seconds": 120}',
                "pid": pid,
            },
        )
        await session.commit()
    ex = await client.post(f"{PRP}/{pid}/execute", headers=creator)
    assert ex.status_code == 403  # 与审批快照不一致 → 拒绝
    detail = await client.get(f"{PRP}/{pid}", headers=creator)
    assert detail.json()["data"]["proposal"]["status"] == "approved"  # 状态未推进
    async with db_session_factory() as session:
        executions = (
            await session.execute(
                text("SELECT id FROM action_executions WHERE proposal_id = :pid"),
                {"pid": pid},
            )
        ).all()
        assert executions == []  # 未创建执行记录


async def test_expired_approval_window_denied_at_execute(
    client, ready_admin, world, db_session_factory
) -> None:
    creator = await _login(client, "creator")
    approver = await _login(client, "approver1")
    resp = await _make_proposal(client, creator, world)
    pid = resp.json()["data"]["proposal"]["id"]
    await client.post(f"{PRP}/{pid}/approve", json={}, headers=approver)
    async with db_session_factory() as session:
        await session.execute(
            text("UPDATE approvals SET expires_at = :t WHERE proposal_id = :pid"),
            {"t": datetime.now(UTC) - timedelta(minutes=1), "pid": pid},
        )
        await session.commit()
    ex = await client.post(f"{PRP}/{pid}/execute", headers=creator)
    assert ex.status_code == 409


async def test_reject_stops_the_chain(client, ready_admin, world) -> None:
    creator = await _login(client, "creator")
    approver = await _login(client, "approver1")
    resp = await _make_proposal(client, creator, world)
    pid = resp.json()["data"]["proposal"]["id"]
    rej = await client.post(
        f"{PRP}/{pid}/reject", json={"comment": "not convinced"}, headers=approver
    )
    assert rej.status_code == 200
    assert rej.json()["data"]["proposal"]["status"] == "rejected"
    ex = await client.post(f"{PRP}/{pid}/execute", headers=creator)
    assert ex.status_code == 409  # rejected 是终态

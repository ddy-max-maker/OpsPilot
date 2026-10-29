"""D6 安全与可靠性加固测试（D5 问题修复验证）。

覆盖（用户指令 §一）：
1. 执行请求防篡改：RabbitMQ 消息仅作通知——伪造/篡改 payload（替换
   parameters/action_key/target_asset_id/environment/proposal_id）不得改变
   执行目标；无登记容器身份的资产拒绝执行；外部副作用次数 = 0 或目标精确。
2. demo-only 执行门禁：action_execution_enabled=False（默认）时零外部动作。
3. 验证恢复（场景 E）：succeeded/executed 未验证时重启 → 只读验证收敛，
   副作用动作次数不变；验证记录幂等（多次触发仅一条）。
4. 完整观察窗口：先健康后失败不误报成功；全窗口持续取样不提前退出。
5. Runbook 重名 409；改密后旧 JWT 失效。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from app.action import verification as verification_mod
from app.action.consumer import handle_execute_requested
from app.action.executor import ActionExecutor
from app.core.config import Settings
from app.domains.knowledge import service as kb_service
from app.models import Proposal
from tests.conftest import auth_headers, truncate_all
from tests.integration.test_action_flow import FakeDockerClient, _enqueue_execution, _fast_settings

pytestmark = pytest.mark.integration


def _executor(**docker_kwargs: Any) -> tuple[ActionExecutor, FakeDockerClient]:
    docker = FakeDockerClient(**docker_kwargs)
    return (
        ActionExecutor(
            container_name_prefix="opspilot-demo-", docker=docker, execution_enabled=True
        ),
        docker,
    )


# ---- 1. 执行请求防篡改（消息仅作通知）----


async def test_tampered_payload_parameters_never_change_target(
    client, demo_world, db_session_factory, monkeypatch
) -> None:
    """篡改消息 parameters：执行仍以 DB 参数/资产绑定容器为目标。"""
    bundle = await _enqueue_execution(client, demo_world)
    executor, docker = _executor()
    payload = dict(bundle["payload"])
    payload["parameters"] = {"container_name": "opspilot-demo-evil"}  # 篡改目标容器
    payload["parameter_hash"] = "0" * 64  # 同步篡改哈希也无法通过（快照三方比对）

    async def healthy(check: dict, *, timeout_seconds: float) -> bool:
        return True

    monkeypatch.setattr(verification_mod, "_probe_once", healthy)
    await handle_execute_requested(
        db_session_factory, executor, payload, executor_id="x", settings=_fast_settings()
    )
    # 执行目标 = DB 资产登记容器；篡改值未生效
    assert docker.restart_calls == ["opspilot-demo-redis"]


async def test_tampered_payload_action_key_ignored(
    client, demo_world, db_session_factory, monkeypatch
) -> None:
    """替换消息 action_key：以 DB 解析为准，不执行消息声称的动作。"""
    bundle = await _enqueue_execution(client, demo_world)
    executor, docker = _executor()
    payload = dict(bundle["payload"])
    payload["action_key"] = "rm -rf /@v99"  # 试图替换为未登记动作

    async def healthy(check: dict, *, timeout_seconds: float) -> bool:
        return True

    monkeypatch.setattr(verification_mod, "_probe_once", healthy)
    await handle_execute_requested(
        db_session_factory, executor, payload, executor_id="x", settings=_fast_settings()
    )
    assert docker.restart_calls == ["opspilot-demo-redis"]  # 仍是 DB 权威动作


async def test_forged_proposal_link_dropped(
    client, demo_world, db_session_factory
) -> None:
    """消息声称的 proposal 与执行记录不符（伪造通知）→ 直接丢弃，零副作用。"""
    bundle = await _enqueue_execution(client, demo_world)
    executor, docker = _executor()
    payload = dict(bundle["payload"])
    payload["proposal_id"] = "prp_forged_0000"
    await handle_execute_requested(
        db_session_factory, executor, payload, executor_id="x", settings=_fast_settings()
    )
    assert docker.restart_calls == []  # 零副作用
    async with db_session_factory() as session:
        execution = (
            (await session.execute(text("SELECT status FROM action_executions"))).scalar_one()
        )
        assert execution == "pending"  # 未被消费


async def test_asset_without_registered_container_cannot_execute(
    client, demo_world, db_session_factory, monkeypatch
) -> None:
    """无登记容器身份的资产：仅凭消息/参数容器名不能执行（DB 层拒绝）。"""
    bundle = await _enqueue_execution(client, demo_world)
    async with db_session_factory() as session:
        await session.execute(
            text("UPDATE assets SET endpoints = '{}'::jsonb WHERE name = 'demo-redis'")
        )
        await session.commit()
    executor, docker = _executor()
    await handle_execute_requested(
        db_session_factory, executor, bundle["payload"], executor_id="x",
        settings=_fast_settings(),
    )
    assert docker.restart_calls == []  # 零副作用
    async with db_session_factory() as session:
        status = (

                await session.execute(
                    text(
                        "SELECT status FROM action_executions "
                        "WHERE id = :eid"
                    ),
                    {"eid": bundle["execution_id"]},
                )

        ).scalar_one()
        assert status == "skipped"


async def test_db_parameter_tampering_detected_by_hash_recompute(
    client, demo_world, db_session_factory
) -> None:
    """DB 参数被篡改（绕过 API）：规范化重算哈希不一致 → 拒绝执行。"""
    bundle = await _enqueue_execution(client, demo_world)
    async with db_session_factory() as session:
        await session.execute(
            text(
                "UPDATE proposals SET parameters = parameters || "
                '\'{"timeout_seconds": 1}\'::jsonb WHERE id = :pid'
            ),
            {"pid": bundle["proposal_id"]},
        )
        await session.commit()
    executor, docker = _executor()
    await handle_execute_requested(
        db_session_factory, executor, bundle["payload"], executor_id="x",
        settings=_fast_settings(),
    )
    assert docker.restart_calls == []  # 零副作用


# ---- 2. demo-only 执行门禁 ----


async def test_execution_disabled_by_default_zero_side_effects(
    client, demo_world, db_session_factory
) -> None:
    """action_execution_enabled=False（默认）：执行请求 → pre_check_failed，零副作用。"""
    bundle = await _enqueue_execution(client, demo_world)
    docker = FakeDockerClient()
    executor = ActionExecutor(
        container_name_prefix="opspilot-demo-", docker=docker, execution_enabled=False
    )
    await handle_execute_requested(
        db_session_factory, executor, bundle["payload"], executor_id="x",
        settings=_fast_settings(),
    )
    assert docker.restart_calls == []  # 默认配置零副作用
    async with db_session_factory() as session:
        execution = (
            (
                await session.execute(
                    text("SELECT status, result_detail FROM action_executions WHERE id = :e"),
                    {"e": bundle["execution_id"]},
                )
            )
            .mappings()
            .one()
        )
        assert execution["status"] == "pre_check_failed"
        assert "demo-only" in str(execution["result_detail"])


# ---- 3. 验证恢复（场景 E）+ 验证记录幂等 ----


async def test_crash_between_execution_and_verification_converges(
    client, demo_world, db_session_factory, monkeypatch
) -> None:
    """动作成功落库后、验证前崩溃：重启收敛只读验证，副作用动作次数不变。"""
    from app.action import convergence
    from app.domains.approvals import service as approvals_service

    bundle = await _enqueue_execution(client, demo_world)
    executor, docker = _executor()

    async def healthy(check: dict, *, timeout_seconds: float) -> bool:
        return True

    monkeypatch.setattr(verification_mod, "_probe_once", healthy)
    settings = _fast_settings()
    # 直接通过服务层完成执行（模拟 consumer 阶段 4 完成后、验证前进程崩溃）
    async with db_session_factory() as session:
        await approvals_service.complete_execution(
            session,
            db_session_factory,
            execution_id=bundle["execution_id"],
            succeeded=True,
            result_detail={"container": "opspilot-demo-redis"},
            external_ref="docker:abc@T1",
            executor="x",
        )
        await session.commit()
    assert docker.restart_calls == []  # 本测试不发起动作（动作在 enqueue 后未消费）

    # 重启收敛：只读验证恢复
    async with db_session_factory() as session:
        resumed = await convergence.resume_pending_verifications(
            session, db_session_factory, settings=settings
        )
    assert len(resumed) == 1
    assert "passed" in resumed[0]["action"]
    async with db_session_factory() as session:
        proposal = await session.get(Proposal, bundle["proposal_id"])
        assert proposal.status == "verified"
        count = (
            await session.execute(text("SELECT count(*) FROM verification_results"))
        ).scalar_one()
        assert count == 1  # 验证记录幂等：仅一条
        exec_count = (
            await session.execute(text("SELECT count(*) FROM action_executions"))
        ).scalar_one()
        assert exec_count == 1  # 无重复执行


async def test_verify_execution_idempotent_on_reentry(
    client, demo_world, db_session_factory, monkeypatch
) -> None:
    """验证重入幂等：已结论时 verify_execution 直接返回，不重复写入。"""
    from app.domains.approvals import service as approvals_service

    bundle = await _enqueue_execution(client, demo_world)
    executor, docker = _executor()

    async def healthy(check: dict, *, timeout_seconds: float) -> bool:
        return True

    monkeypatch.setattr(verification_mod, "_probe_once", healthy)
    settings = _fast_settings()
    async with db_session_factory() as session:
        await approvals_service.complete_execution(
            session,
            db_session_factory,
            execution_id=bundle["execution_id"],
            succeeded=True,
            result_detail={},
            executor="x",
        )
        await session.commit()
        proposal = await session.get(Proposal, bundle["proposal_id"])
        assert proposal is not None

    s1 = await verification_mod.verify_execution(
        db_session_factory, execution_id=bundle["execution_id"], proposal=proposal,
        context={"container_name": "opspilot-demo-redis", "requested_at": None},
        executor_id="x", settings=settings,
    )
    s2 = await verification_mod.verify_execution(
        db_session_factory, execution_id=bundle["execution_id"], proposal=proposal,
        context={"container_name": "opspilot-demo-redis", "requested_at": None},
        executor_id="x", settings=settings,
    )
    assert s1 == s2 == "passed"
    async with db_session_factory() as session:
        count = (
            await session.execute(text("SELECT count(*) FROM verification_results"))
        ).scalar_one()
        assert count == 1


# ---- 4. 完整观察窗口 ----


async def test_window_full_sampling_healthy_then_failing(db_engine, clean_db) -> None:
    """先健康后失败：窗口内持续取样捕获退化，不误报 PASSED。"""
    await truncate_all(db_engine)
    check = {"kind": "http_health", "url": "http://probe-target/health"}
    settings = Settings(
        verification_observation_window_seconds=3,
        verification_probe_interval_seconds=1,
        verification_min_passing_probes=1,
        verification_probe_timeout_seconds=1,
    )
    calls = {"n": 0}

    async def degrade_after_two(check: dict, *, timeout_seconds: float) -> bool:
        calls["n"] += 1
        return calls["n"] <= 2  # 前两次健康，随后失败

    orig = verification_mod._probe_once
    verification_mod._probe_once = degrade_after_two  # type: ignore[method-assign]
    try:
        status, probes, window = await verification_mod._run_observation_window(
            check, settings=settings
        )
    finally:
        verification_mod._probe_once = orig  # type: ignore[method-assign]
    assert status == "failed"  # 早期健康不构成误报
    assert calls["n"] >= 3  # 失败被窗口捕获（前两次健康，第 3 次失败）
    assert window["sample_count"] == calls["n"]


async def test_window_samples_entire_duration_when_healthy(db_engine, clean_db) -> None:
    """持续健康：窗口全程取样（不因早期连续通过提前退出）。"""
    await truncate_all(db_engine)
    check = {"kind": "http_health", "url": "http://probe-target/health"}
    settings = Settings(
        verification_observation_window_seconds=2,
        verification_probe_interval_seconds=1,
        verification_min_passing_probes=1,
        verification_probe_timeout_seconds=1,
    )

    async def always_healthy(check: dict, *, timeout_seconds: float) -> bool:
        return True

    orig = verification_mod._probe_once
    verification_mod._probe_once = always_healthy  # type: ignore[method-assign]
    try:
        status, probes, window = await verification_mod._run_observation_window(
            check, settings=settings
        )
    finally:
        verification_mod._probe_once = orig  # type: ignore[method-assign]
    assert status == "passed"
    assert window["sample_count"] >= 2  # 窗口期全程取样


# ---- 5. Runbook 重名 409 ----


async def test_duplicate_runbook_name_conflict(client, ready_admin) -> None:
    body = {
        "name": "D6 重名手册",
        "risk_level": "r1",
        "steps": [{"action": "x"}],
        "allowed_environments": ["sandbox"],
        "allowlist_action_keys": ["restart_container@v1"],
    }
    first = await client.post("/api/v1/runbooks", json=body, headers=ready_admin)
    assert first.status_code == 201, first.text
    second = await client.post("/api/v1/runbooks", json=body, headers=ready_admin)
    assert second.status_code == 409  # 稳定 409（此前 500）
    assert second.json()["error"]["code"] == "CONFLICT_STATE"
    _ = kb_service  # 保持导入语义（服务层修复被上述 API 用例覆盖）


# ---- 6. 改密后旧 JWT 失效 ----


async def test_old_jwt_invalid_after_password_change(client, ready_admin) -> None:
    """改密后：改密前签发的 access token 立即 401（服务端 token_valid_after 校验）；
    改密响应携带重新签发的新 token 可用。"""
    change = await client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "AdminReady123!", "new_password": "AdminD6Pass123!"},
        headers=ready_admin,
    )
    assert change.status_code == 200, change.text
    stale = await client.get("/api/v1/assets?limit=1", headers=ready_admin)
    assert stale.status_code == 401  # 改密前签发的旧 token 已被服务端失效
    # 改密响应中的新 token 可用
    fresh = auth_headers(change.json()["data"]["access_token"])
    ok = await client.get("/api/v1/assets?limit=1", headers=fresh)
    assert ok.status_code == 200

"""D5 集成测试：ops-action 执行流（幂等/崩溃收敛/S9 验证/Outbox）。

覆盖（用户指令 §3–§6 / §9）：
- 执行器：容器身份前检查（白名单/存在/运行中）、成功（StartedAt 刷新）、
  Docker 拒绝（404 → failed）、超时（unknown，禁止盲目重试）；
- 消费者：pending → claimed → succeeded → executed → verified（incident → resolved）；
  重复消息零重复外部动作（幂等账本 + 状态检查双防线）；
  执行前复核拒绝（审批/哈希/有效期不一致 → skipped，不动外部）；
- S9：动作成功但业务健康探测失败 → VERIFY_FAILED + incident 回 investigating，
  绝不 RESOLVED；无健康探测配置 → VERIFY_UNKNOWN；
- 崩溃收敛：claimed 超时 → unknown（默认禁止重试）；
- Outbox：业务事务写入 → dispatcher 投递（幂等：重复 dispatch 零重复投递）。
  RabbitMQ 不可用时相关用例自动跳过（CI service / 本地 compose 提供）。
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.action import verification as verification_mod
from app.action.consumer import handle_execute_requested
from app.action.docker_client import DockerClient
from app.action.executor import ActionExecutor
from app.action.outbox import declare_topology, dispatch_batch
from app.core.config import Settings
from app.models import (
    ActionExecution,
    ActionExecutionStatus,
    ActionIdempotency,
    IdempotencyState,
    Proposal,
)
from tests.conftest import truncate_all
from tests.integration.test_approvals import _login, _make_proposal, _seed_world

pytestmark = pytest.mark.integration


def _fast_settings() -> Settings:
    """验证窗口压缩到秒级；幂等收敛超时立即触发。"""
    s = Settings()
    s.verification_observation_window_seconds = 1
    s.verification_probe_interval_seconds = 1
    s.verification_min_passing_probes = 1
    s.idempotency_claim_timeout_seconds = 0
    return s


class FakeDockerClient(DockerClient):
    """可控 Docker 替身：记录调用、模拟 inspect/restart 行为（含故障注入）。"""

    def __init__(
        self,
        *,
        exists: bool = True,
        running: bool = True,
        refresh_started_at: bool = True,
        restart_error: str | None = None,  # None | "timeout" | "404" | "500"
    ) -> None:
        super().__init__()
        self.exists = exists
        self.running = running
        self.refresh_started_at = refresh_started_at
        self.restart_error = restart_error
        self.restart_calls: list[str] = []
        self.inspect_calls: list[str] = []

    async def inspect_container(self, name: str) -> dict[str, Any] | None:  # noqa: D102
        self.inspect_calls.append(name)
        if not self.exists:
            return None
        # 重启前 StartedAt=T0；重启成功且刷新 → T1（供执行器判定真实生效）
        started = "T1" if (self.restart_calls and self.refresh_started_at) else "T0"
        return {
            "Id": "abc123def4567890",
            "State": {
                "Running": True,
                "Status": "running" if self.running else "exited",
                "StartedAt": started,
            },
        }

    async def restart_container(self, name: str, *, timeout_seconds: int = 30) -> None:  # noqa: D102
        self.restart_calls.append(name)
        if self.restart_error == "timeout":
            raise httpx.TimeoutException("restart request timed out")
        if self.restart_error == "404":
            req = httpx.Request("POST", f"http://docker/containers/{name}/restart")
            raise httpx.HTTPStatusError(
                "no such container", request=req, response=httpx.Response(404, request=req)
            )
        if self.restart_error == "500":
            req = httpx.Request("POST", f"http://docker/containers/{name}/restart")
            raise httpx.HTTPStatusError(
                "internal error", request=req, response=httpx.Response(500, request=req)
            )


def _executor(**docker_kwargs: Any) -> tuple[ActionExecutor, FakeDockerClient]:
    docker = FakeDockerClient(**docker_kwargs)
    return ActionExecutor(container_name_prefix="opspilot-demo-", docker=docker), docker


@pytest.fixture
async def world(db_session_factory, client) -> dict[str, str]:
    """播种世界（client fixture 已 truncate + bootstrap）。"""
    w = await _seed_world(db_session_factory)
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


async def _enqueue_execution(client: httpx.AsyncClient, world: dict[str, str]) -> dict[str, Any]:
    """审批 + 执行入队，返回消费者 payload。"""
    creator = await _login(client, "creator")
    approver = await _login(client, "approver1")
    resp = await _make_proposal(client, creator, world)
    assert resp.status_code == 201, resp.text
    proposal = resp.json()["data"]["proposal"]
    pid = proposal["id"]
    ok = await client.post(f"/api/v1/proposals/{pid}/approve", json={}, headers=approver)
    assert ok.status_code == 200, ok.text
    ex = await client.post(f"/api/v1/proposals/{pid}/execute", headers=creator)
    assert ex.status_code == 200, ex.text
    execution = ex.json()["data"]["execution"]
    return {
        "proposal_id": pid,
        "execution_id": execution["id"],
        "payload": {
            "proposal_id": pid,
            "execution_id": execution["id"],
            "incident_id": world["incident_id"],
            "action_key": "restart_container@v1",
            "idempotency_key": execution["idempotency_key"],
            "runbook_version_id": world["rbv_id"],
            "target_asset_id": world["asset_id"],
            "environment": "sandbox",
            "parameters": {"container_name": "opspilot-demo-redis"},
            "parameter_hash": proposal["parameter_hash"],
            "risk_level": "r1",
            "requested_by": "usr_creator",
            "requested_at": datetime.now(UTC).isoformat(),
        },
    }


# ---- 执行器（真实 allowlist 路径 + Fake Docker）----


async def test_executor_precheck_rejects_outside_namespace(db_engine, clean_db) -> None:
    await truncate_all(db_engine)
    executor, docker = _executor()
    outcome = await executor.execute(
        action_key="restart_container@v1", parameters={"container_name": "victim-1"}
    )
    assert outcome.status.value == "pre_check_failed"
    assert docker.restart_calls == []  # 未发起外部动作


async def test_executor_rejects_unknown_action(db_engine, clean_db) -> None:
    await truncate_all(db_engine)
    executor, docker = _executor()
    outcome = await executor.execute(
        action_key="rm -rf /@v99", parameters={"container_name": "opspilot-demo-redis"}
    )
    assert outcome.status.value == "pre_check_failed"
    assert docker.restart_calls == []


async def test_executor_success_refreshes_started_at(db_engine, clean_db) -> None:
    await truncate_all(db_engine)
    executor, docker = _executor(refresh_started_at=True)
    outcome = await executor.execute(
        action_key="restart_container@v1", parameters={"container_name": "opspilot-demo-redis"}
    )
    assert outcome.status.value == "succeeded"
    assert docker.restart_calls == ["opspilot-demo-redis"]
    assert outcome.external_ref and "T1" in outcome.external_ref


async def test_executor_started_at_unchanged_is_unknown(db_engine, clean_db) -> None:
    """场景 C 变体：restart 应答成功但 StartedAt 未刷新 → 无法确认生效 → unknown。"""
    await truncate_all(db_engine)
    executor, _docker = _executor(refresh_started_at=False)
    outcome = await executor.execute(
        action_key="restart_container@v1", parameters={"container_name": "opspilot-demo-redis"}
    )
    assert outcome.status.value == "unknown"


async def test_executor_docker_404_is_failed(db_engine, clean_db) -> None:
    await truncate_all(db_engine)
    executor, _docker = _executor(restart_error="404")
    outcome = await executor.execute(
        action_key="restart_container@v1", parameters={"container_name": "opspilot-demo-redis"}
    )
    assert outcome.status.value == "failed"  # 明确拒绝：效果确定为未发生


async def test_executor_timeout_is_unknown(db_engine, clean_db) -> None:
    await truncate_all(db_engine)
    executor, _docker = _executor(restart_error="timeout")
    outcome = await executor.execute(
        action_key="restart_container@v1", parameters={"container_name": "opspilot-demo-redis"}
    )
    assert outcome.status.value == "unknown"  # 不可观测：禁止盲目重试


# ---- 消费者全链路 ----


async def test_consumer_full_chain_to_resolved(
    client, world, db_session_factory, monkeypatch
) -> None:
    """S1 主链路：执行成功 + 健康探测全部通过 → VERIFIED + incident RESOLVED。"""
    bundle = await _enqueue_execution(client, world)
    executor, docker = _executor()

    async def healthy_probe(check: dict, *, timeout_seconds: float) -> bool:
        return True

    monkeypatch.setattr(verification_mod, "_probe_once", healthy_probe)
    await handle_execute_requested(
        db_session_factory,
        executor,
        bundle["payload"],
        executor_id="ops-action-test",
        settings=_fast_settings(),
    )
    assert docker.restart_calls == ["opspilot-demo-redis"]  # 恰好一次外部动作
    async with db_session_factory() as session:
        execution = await session.get(ActionExecution, bundle["execution_id"])
        assert execution.status == "succeeded"
        proposal = await session.get(Proposal, bundle["proposal_id"])
        assert proposal.status == "verified"
        inc = await session.execute(
            text("SELECT status FROM incidents WHERE id = :i"),
            {"i": world["incident_id"]},
        )
        assert inc.scalar_one() == "resolved"
        ver_rows = (
            (
                await session.execute(
                    text("SELECT status, observation_window FROM verification_results")
                )
            )
            .mappings()
            .all()
        )
        assert ver_rows and ver_rows[0]["status"] == "passed"
        assert ver_rows[0]["observation_window"]["sample_count"] >= 1
        ledger = (await session.execute(text("SELECT state FROM action_idempotency"))).scalar_one()
        assert ledger == "completed"


async def test_duplicate_message_never_repeats_side_effect(
    client, world, db_session_factory, monkeypatch
) -> None:
    """重复消息/重投：幂等账本 + 状态检查双防线，外部动作恰好一次。"""
    bundle = await _enqueue_execution(client, world)
    executor, docker = _executor()

    async def healthy_probe(check: dict, *, timeout_seconds: float) -> bool:
        return True

    monkeypatch.setattr(verification_mod, "_probe_once", healthy_probe)
    settings = _fast_settings()
    for _ in range(3):  # 原始投递 + 两次重复投递
        await handle_execute_requested(
            db_session_factory, executor, bundle["payload"], executor_id="x", settings=settings
        )
    assert docker.restart_calls == ["opspilot-demo-redis"]  # 零重复副作用


async def test_s9_action_succeeds_but_service_still_unhealthy(
    client, world, db_session_factory, monkeypatch
) -> None:
    """S9 红线：容器重启成功但业务健康探测失败 → VERIFY_FAILED，incident 回 investigating。"""
    bundle = await _enqueue_execution(client, world)
    executor, docker = _executor()  # 动作"成功"

    async def failing_probe(check: dict, *, timeout_seconds: float) -> bool:
        return False

    monkeypatch.setattr(verification_mod, "_probe_once", failing_probe)
    await handle_execute_requested(
        db_session_factory,
        executor,
        bundle["payload"],
        executor_id="x",
        settings=_fast_settings(),
    )
    assert docker.restart_calls == ["opspilot-demo-redis"]
    async with db_session_factory() as session:
        proposal = await session.get(Proposal, bundle["proposal_id"])
        assert proposal.status == "verify_failed"  # 绝不 verified
        inc = await session.execute(
            text("SELECT status FROM incidents WHERE id = :i"), {"i": world["incident_id"]}
        )
        assert inc.scalar_one() == "investigating"  # 转回调查
        ver = (await session.execute(text("SELECT status FROM verification_results"))).scalar_one()
        assert ver == "failed"


async def test_no_health_probe_configured_yields_unknown(
    client, world, db_session_factory, monkeypatch
) -> None:
    """目标资产未配置健康探测 → VERIFY_UNKNOWN（不宣称成功）。"""
    async with db_session_factory() as session:
        await session.execute(
            text("UPDATE assets SET endpoints = :e WHERE id = 'as_redis_demo'"),
            {"e": '{"container_name": "opspilot-demo-redis"}'},  # 无 health_check
        )
        await session.commit()
    bundle = await _enqueue_execution(client, world)
    executor, docker = _executor()

    async def never_probe(check: dict, *, timeout_seconds: float) -> bool:  # pragma: no cover
        raise AssertionError("probe must not run without configuration")

    monkeypatch.setattr(verification_mod, "_probe_once", never_probe)
    await handle_execute_requested(
        db_session_factory,
        executor,
        bundle["payload"],
        executor_id="x",
        settings=_fast_settings(),
    )
    async with db_session_factory() as session:
        proposal = await session.get(Proposal, bundle["proposal_id"])
        assert proposal.status == "verify_unknown"


async def test_pre_execute_recheck_rejects_tampered_state(
    client, world, db_session_factory
) -> None:
    """执行前复核：提案状态被 DB 篡改（非 executing）→ skipped，外部动作不发起。"""
    bundle = await _enqueue_execution(client, world)
    async with db_session_factory() as session:
        await session.execute(
            text("UPDATE proposals SET status = 'approved' WHERE id = :pid"),
            {"pid": bundle["proposal_id"]},
        )
        await session.commit()
    executor, docker = _executor()
    await handle_execute_requested(
        db_session_factory,
        executor,
        bundle["payload"],
        executor_id="x",
        settings=_fast_settings(),
    )
    assert docker.restart_calls == []  # 外部动作未发起
    async with db_session_factory() as session:
        execution = await session.get(ActionExecution, bundle["execution_id"])
        assert execution.status == "skipped"
        assert "recheck" in str(execution.result_detail)


# ---- 崩溃收敛 ----


async def test_crash_convergence_marks_unknown(client, world, db_session_factory) -> None:
    """场景 B/C：claimed 超时且无可靠外部查询 → unknown，禁止盲目重试，转人工。"""
    from app.action import convergence

    bundle = await _enqueue_execution(client, world)
    async with db_session_factory() as session:
        execution = await session.get(ActionExecution, bundle["execution_id"])
        execution.status = ActionExecutionStatus.RUNNING
        execution.started_at = datetime.now(UTC) - timedelta(seconds=999)
        session.add(
            ActionIdempotency(
                idempotency_key=execution.idempotency_key,
                action_execution_id=execution.id,
                state=IdempotencyState.CLAIMED,
                claimed_at=datetime.now(UTC) - timedelta(seconds=999),
            )
        )
        await session.commit()

    async with db_session_factory() as session:
        results = await convergence.converge_stale_claims(
            session, db_session_factory, settings=_fast_settings()
        )
    assert len(results) == 1
    assert results[0]["action"] == "marked unknown"
    async with db_session_factory() as session:
        execution = await session.get(ActionExecution, bundle["execution_id"])
        assert execution.status == "unknown"
        ledger = (await session.execute(text("SELECT state FROM action_idempotency"))).scalar_one()
        assert ledger == "unknown"
        inc = await session.execute(
            text("SELECT status FROM incidents WHERE id = :i"), {"i": world["incident_id"]}
        )
        assert inc.scalar_one() == "needs_human"  # 转人工


# ---- Outbox dispatcher（真实 RabbitMQ；不可用时跳过）----


def _rabbitmq_available() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 5672), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _rabbitmq_available(), reason="RabbitMQ not reachable on 127.0.0.1:5672")
async def test_outbox_dispatch_idempotent(client, world, db_session_factory) -> None:
    """Outbox 投递：pending → dispatched；重复 dispatch 零重复投递。

    使用测试专用队列绑定同一 exchange（topic 复制）——不受本地演示环境中
    活跃 ops-action 消费者影响；生产队列的消费行为由 consumer 测试覆盖。
    """
    import aio_pika

    await _enqueue_execution(client, world)
    settings = Settings(rabbitmq_url="amqp://opspilot:opspilot@127.0.0.1:5672/%2F")
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    try:
        channel = await connection.channel()
        exchange, _queue = await declare_topology(channel)
        # 测试专用队列（无消费者；消息留存供断言）
        probe_queue = await channel.declare_queue("test.outbox.probe", durable=False)
        await probe_queue.bind(exchange, routing_key="action.execute_requested")
        await probe_queue.purge()

        dispatched = await dispatch_batch(db_session_factory, channel, settings)
        assert dispatched == 1
        async with db_session_factory() as session:
            statuses = (
                (await session.execute(text("SELECT status FROM outbox_events"))).scalars().all()
            )
            assert statuses == ["dispatched"]
        msg = await probe_queue.get(fail=False, timeout=2)
        assert msg is not None
        payload = json.loads(msg.body)
        assert payload["execution_id"]
        await msg.ack()

        # 第二次 dispatch：无 pending 事件 → 0 条（不重复投递）
        assert await dispatch_batch(db_session_factory, channel, settings) == 0
        assert await probe_queue.get(fail=False, timeout=0.5) is None
    finally:
        await connection.close()

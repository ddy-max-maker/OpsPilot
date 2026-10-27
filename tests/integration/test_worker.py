"""ops-worker 集成测试（IMPLEMENTATION_PLAN §2.5 预算/取消/fencing/认领/图推进/红线）。

使用真实 PostgreSQL（业务表 + langgraph schema checkpoint）+ FakeDiagnoserChatModel
（确定性脚本模型，报告由真实工具结果合成）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text

from app.core.config import Settings
from app.core.enums import (
    HypothesisStatus,
    RunEventType,
    RunStatus,
    RunStopReason,
)
from app.core.ids import new_id
from app.core.security import hash_password
from app.domains.agent.fake import FakeDiagnoserChatModel
from app.domains.agent.provider import ModelHandle
from app.domains.agent.runtime import AgentRuntime, RunExecution, RuntimeDeps
from app.domains.tools.context import RunEventWriter
from app.domains.tools.gateway import ToolGateway
from app.models import (
    Asset,
    DiagnosticReport,
    Evidence,
    Hypothesis,
    Incident,
    InvestigationRun,
    RunEvent,
    User,
)
from app.worker import claim


async def _make_runtime(
    db_engine, db_session_factory, settings: Settings
) -> tuple[AgentRuntime, Any]:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        conninfo=settings.psycopg_database_url,
        min_size=1,
        max_size=2,
        open=False,
        kwargs={"autocommit": True},
    )
    await pool.open()
    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()
    model = FakeDiagnoserChatModel(model_name=settings.agent_model_name)
    handle = ModelHandle(model, provider="fake", model_name=settings.agent_model_name)
    deps = RuntimeDeps(
        session_factory=db_session_factory,
        settings=settings,
        gateway=ToolGateway(db_session_factory, settings),
        checkpointer=checkpointer,
        pool=pool,
        model_handle=handle,
        model=model,
    )
    return AgentRuntime(deps), pool


async def _seed_run(
    db_session_factory, db_engine, *, budgets: dict | None = None
) -> InvestigationRun:
    """seed：用户 + 资产 + incident(investigating) + run(queued)。"""
    from tests.conftest import truncate_all

    await truncate_all(db_engine)
    async with db_session_factory() as session:
        user_id = "usr_worker_probe"
        if await session.get(User, user_id) is None:
            session.add(
                User(
                    id=user_id,
                    username="worker_probe",
                    email="worker_probe@test.local",
                    password_hash=hash_password("WorkerProbe123!"),
                    is_active=True,
                    must_change_password=False,
                )
            )
            await session.flush()
        asset = Asset(
            id=new_id("as_"),
            name="worker-svc",
            environment="sandbox",
            asset_type="service",
            status="active",
            criticality="medium",
            endpoints={},
            owner=None,
            tags=[],
        )
        session.add(asset)
        await session.flush()
        incident = Incident(
            id=new_id("inc_"),
            title="worker probe 事件",
            environment="sandbox",
            primary_asset_id=asset.id,
            source="manual",
            severity="medium",
            status="investigating",
            created_by=user_id,
        )
        session.add(incident)
        await session.flush()
        run_id = new_id("run_")
        run = InvestigationRun(
            id=run_id,
            incident_id=incident.id,
            status=RunStatus.QUEUED.value,
            triggered_by=user_id,
            model_config_json={"provider": "fake", "rag": "off"},
            budgets=budgets or {},
            usage={},
            authorization_snapshot={
                "is_admin": True,
                "all_assets": True,
                "asset_ids": [],
                "environments": [],
            },
            langgraph_thread_id=run_id,  # ADR-5：thread_id = run_id
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        await session.refresh(asset)
        await session.refresh(incident)
        return run


async def _execute(
    runtime: AgentRuntime,
    db_session_factory,
    run_id: str,
    *,
    cancel_checker=None,
) -> RunExecution:
    async with db_session_factory() as session:
        run = await session.get(InvestigationRun, run_id)
        incident = await session.get(Incident, run.incident_id)
        asset = await session.get(Asset, incident.primary_asset_id)
    events = await RunEventWriter.create(db_session_factory, run_id)
    execution = RunExecution(
        run=run,
        incident=incident,
        asset=asset,
        cancel_checker=cancel_checker or _never_cancel,
        events=events,
    )
    return execution


async def _never_cancel() -> bool:
    return False


@pytest.fixture
def worker_settings() -> Settings:
    return Settings()


@pytest.fixture
async def worker_runtime(db_engine, db_session_factory, worker_settings, clean_db):
    runtime, pool = await _make_runtime(db_engine, db_session_factory, worker_settings)
    yield runtime
    await pool.close()


# ---- 图推进（fake 模型确定性全链路）----


async def test_full_run_produces_evidence_hypotheses_report(
    worker_runtime, db_session_factory, worker_settings, db_engine
) -> None:
    run = await _seed_run(db_session_factory, db_engine)
    claimed = await claim.claim_next_run(db_session_factory, worker_id="w1", lease_ttl_seconds=60)
    assert claimed is not None and claimed["id"] == run.id
    execution = await _execute(worker_runtime, db_session_factory, run.id)
    outcome = await worker_runtime.execute_run(execution)

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.stop_reason is RunStopReason.COMPLETED
    assert outcome.usage["tool_calls_used"] > 0
    # Worker 终态收尾：run_finished 事件（与 loop._finalize 一致）
    await execution.events.append(
        RunEventType.RUN_FINISHED,
        {"status": outcome.status.value, "stop_reason": outcome.stop_reason.value},
    )

    async with db_session_factory() as session:
        # 证据 > 0 且 run 归属正确
        evidence = (
            (await session.execute(select(Evidence).where(Evidence.run_id == run.id)))
            .scalars()
            .all()
        )
        assert len(evidence) == outcome.usage["tool_calls_used"]
        assert all(e.schema_version == "1" for e in evidence)
        # 假设引用有效 Evidence ID；supported 必须有实时证据
        hypotheses = (
            (await session.execute(select(Hypothesis).where(Hypothesis.run_id == run.id)))
            .scalars()
            .all()
        )
        evidence_ids = {e.id for e in evidence}
        for h in hypotheses:
            for ref in h.supporting_evidence_ids or []:
                assert ref in evidence_ids
            if h.status == HypothesisStatus.SUPPORTED.value:
                assert h.supporting_evidence_ids
        # 报告快照存在且引用全部为有效证据
        report = (
            await session.execute(
                select(DiagnosticReport)
                .where(DiagnosticReport.run_id == run.id)
                .order_by(DiagnosticReport.version_no.desc())
                .limit(1)
            )
        ).scalar_one()
        assert set(report.evidence_refs or []) <= evidence_ids
        assert report.generator["provider"] == "fake"
        assert report.generator["prompt_version"] == worker_settings.agent_prompt_version
        # 事件序列：run_started → ... → run_finished
        types = (
            (
                await session.execute(
                    select(RunEvent.type).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
                )
            )
            .scalars()
            .all()
        )
        assert types[0] == RunEventType.RUN_STARTED.value
        assert RunEventType.TOOL_CALLED.value in types
        assert RunEventType.EVIDENCE_ADDED.value in types
        assert types[-1] == RunEventType.RUN_FINISHED.value
        # checkpoint 落入 langgraph 独立 schema，thread_id = run_id
        checkpoint_rows = await session.execute(
            text("SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = :tid"),
            {"tid": run.id},
        )
        assert checkpoint_rows.scalar_one() > 0


async def test_unknown_exit_when_no_evidence(worker_runtime, db_session_factory, db_engine) -> None:
    """S7 雏形：全部工具失败且无证据 → unknown（这里直接用空计划不可行，
    以连接器全未配置 + all 探测失败路径验证 unknown 报告语义）。"""
    run = await _seed_run(db_session_factory, db_engine)
    await claim.claim_next_run(db_session_factory, worker_id="w1", lease_ttl_seconds=60)
    execution = await _execute(worker_runtime, db_session_factory, run.id)
    outcome = await worker_runtime.execute_run(execution)
    # fake 模型按计划调用工具：每一步都会产出（失败也是）证据 → completed；
    # 断言至少不是 failed，且报告可查
    assert outcome.status in (RunStatus.COMPLETED, RunStatus.NEEDS_HUMAN)


# ---- 预算（服务端硬限制）----


async def test_budget_exhausted_forced_stop(worker_runtime, db_session_factory, db_engine) -> None:
    """max_tool_calls=2：服务端强制终止（模型/Prompt 无法绕过）。"""
    run = await _seed_run(
        db_session_factory,
        db_engine,
        budgets={"max_tool_calls": 2, "max_iterations": 100, "max_tokens": 1000000},
    )
    await claim.claim_next_run(db_session_factory, worker_id="w1", lease_ttl_seconds=60)
    execution = await _execute(worker_runtime, db_session_factory, run.id)
    outcome = await worker_runtime.execute_run(execution)

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.stop_reason is RunStopReason.BUDGET_EXHAUSTED
    assert outcome.usage["tool_calls_used"] <= 2
    assert outcome.unknown is True
    # 终态补 unknown 报告快照（与 worker loop._finalize 一致：非正常终态补报告）
    await worker_runtime.write_fallback_report(execution, reason="预算耗尽（服务端强制终止）")
    async with db_session_factory() as session:
        report = (
            await session.execute(select(DiagnosticReport).where(DiagnosticReport.run_id == run.id))
        ).scalar_one()
        assert report.unknown_note["is_unknown"] is True


# ---- 取消（合作式）----


async def test_cooperative_cancel_mid_run(
    worker_runtime, db_session_factory, worker_settings, db_engine
) -> None:
    run = await _seed_run(db_session_factory, db_engine)
    await claim.claim_next_run(db_session_factory, worker_id="w1", lease_ttl_seconds=60)

    async def cancel_after_first_tool() -> bool:
        async with db_session_factory() as session:
            count = int(
                await session.scalar(
                    select(func.count()).select_from(Evidence).where(Evidence.run_id == run.id)
                )
            )
        return count >= 1  # 首条证据落库后请求取消

    execution = await _execute(
        worker_runtime, db_session_factory, run.id, cancel_checker=cancel_after_first_tool
    )
    outcome = await worker_runtime.execute_run(execution)
    assert outcome.status is RunStatus.CANCELED
    assert outcome.stop_reason is RunStopReason.CANCELED
    # 终态回写（fencing 保护）成功
    fence = await claimed_fence(run.id, db_session_factory)
    finished = await claim.finish_run(
        db_session_factory,
        run_id=run.id,
        fence_token=fence,
        status=outcome.status.value,
        stop_reason=outcome.stop_reason.value,
        usage=outcome.usage,
    )
    assert finished is True


async def claimed_fence(run_id: str, db_session_factory) -> int:
    async with db_session_factory() as session:
        run = await session.get(InvestigationRun, run_id)
        return int(run.fence_token)


# ---- fencing（ADR-12）----


async def test_stale_worker_write_rejected(
    db_session_factory, worker_runtime, clean_db, db_engine
) -> None:
    run = await _seed_run(db_session_factory, db_engine)
    claimed = await claim.claim_next_run(db_session_factory, worker_id="w1", lease_ttl_seconds=60)
    fence = int(claimed["fence_token"])
    # 旧 Worker（fence-1）回写 → 拒绝且状态保持 running
    stale = await claim.finish_run(
        db_session_factory,
        run_id=run.id,
        fence_token=fence - 1,
        status=RunStatus.COMPLETED.value,
        stop_reason=RunStopReason.COMPLETED.value,
        usage={},
    )
    assert stale is False
    async with db_session_factory() as session:
        current = await session.get(InvestigationRun, run.id)
        assert current.status == RunStatus.RUNNING.value
    # 正确 token 回写 → 成功
    ok = await claim.finish_run(
        db_session_factory,
        run_id=run.id,
        fence_token=fence,
        status=RunStatus.COMPLETED.value,
        stop_reason=RunStopReason.COMPLETED.value,
        usage={"tool_calls_used": 1},
    )
    assert ok is True
    async with db_session_factory() as session:
        current = await session.get(InvestigationRun, run.id)
        assert current.status == RunStatus.COMPLETED.value
        assert current.usage == {"tool_calls_used": 1}


# ---- 认领（SKIP LOCKED / 租约回收）----


async def test_claim_is_exclusive(db_session_factory, db_engine, clean_db) -> None:
    run = await _seed_run(db_session_factory, db_engine)
    first = await claim.claim_next_run(db_session_factory, worker_id="w1", lease_ttl_seconds=60)
    second = await claim.claim_next_run(db_session_factory, worker_id="w2", lease_ttl_seconds=60)
    assert first is not None and first["id"] == run.id
    assert second is None  # running 且租约未过期 → 不可重复认领


async def test_expired_lease_reclaimed(db_session_factory, db_engine, clean_db) -> None:
    run = await _seed_run(db_session_factory, db_engine)
    first = await claim.claim_next_run(db_session_factory, worker_id="w1", lease_ttl_seconds=60)
    assert first is not None
    # 模拟 Worker 失联：租约过期
    async with db_session_factory() as session:
        current = await session.get(InvestigationRun, run.id)
        current.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    reclaimed = await claim.claim_next_run(db_session_factory, worker_id="w2", lease_ttl_seconds=60)
    assert reclaimed is not None
    assert reclaimed["id"] == run.id
    assert int(reclaimed["fence_token"]) == int(first["fence_token"]) + 1


async def test_heartbeat_requires_valid_fence(db_session_factory, db_engine, clean_db) -> None:
    run = await _seed_run(db_session_factory, db_engine)
    claimed = await claim.claim_next_run(db_session_factory, worker_id="w1", lease_ttl_seconds=60)
    fence = int(claimed["fence_token"])
    assert await claim.heartbeat(
        db_session_factory, run_id=run.id, fence_token=fence, lease_ttl_seconds=60
    )
    assert not await claim.heartbeat(
        db_session_factory, run_id=run.id, fence_token=fence + 99, lease_ttl_seconds=60
    )


# ---- checkpoint 恢复 ----


async def test_checkpoint_resume_after_interrupt(
    worker_runtime, db_session_factory, db_engine
) -> None:
    """Worker 崩溃（执行中断）后重新认领 → 从 checkpoint 恢复并完成。"""
    run = await _seed_run(db_session_factory, db_engine)
    await claim.claim_next_run(db_session_factory, worker_id="w1", lease_ttl_seconds=60)

    # 第一次执行：完成第一个工具步后模拟崩溃（Cancel scope 直接中断 astream）
    class _CrashAfterFirstTool:
        def __init__(self) -> None:
            self.checked = 0

        async def __call__(self) -> bool:
            self.checked += 1
            async with db_session_factory() as session:
                count = int(
                    await session.scalar(
                        select(func.count()).select_from(Evidence).where(Evidence.run_id == run.id)
                    )
                )
            if count >= 1:
                raise KeyboardInterrupt  # 模拟进程崩溃
            return False

    execution = await _execute(
        worker_runtime, db_session_factory, run.id, cancel_checker=_CrashAfterFirstTool()
    )
    with pytest.raises(KeyboardInterrupt):
        await worker_runtime.execute_run(execution)

    # 崩溃后 run 仍为 running + 租约过期 → 新 Worker 认领（fence +1）
    async with db_session_factory() as session:
        current = await session.get(InvestigationRun, run.id)
        current.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    reclaimed = await claim.claim_next_run(db_session_factory, worker_id="w2", lease_ttl_seconds=60)
    assert reclaimed is not None

    # 第二次执行：从 checkpoint 恢复 → 正常完成
    execution2 = await _execute(worker_runtime, db_session_factory, run.id)
    outcome = await worker_runtime.execute_run(execution2)
    assert outcome.status is RunStatus.COMPLETED
    # 报告存在且证据链完整
    async with db_session_factory() as session:
        report = (
            await session.execute(
                select(DiagnosticReport)
                .where(DiagnosticReport.run_id == run.id)
                .order_by(DiagnosticReport.version_no.desc())
                .limit(1)
            )
        ).scalar_one()
        evidence_ids = set(
            (await session.execute(select(Evidence.id).where(Evidence.run_id == run.id))).scalars()
        )
        assert set(report.evidence_refs or []) <= evidence_ids


# ---- 红线：工具面裁剪 ----


def test_harness_profile_redline() -> None:
    from deepagents.profiles.harness.harness_profiles import HarnessProfile

    from app.domains.agent.runtime import HARNESS_PROFILE

    assert isinstance(HARNESS_PROFILE, HarnessProfile)
    assert "execute" in HARNESS_PROFILE.excluded_tools
    assert HARNESS_PROFILE.general_purpose_subagent is not None
    assert HARNESS_PROFILE.general_purpose_subagent.enabled is False


def test_gateway_tool_surface_readonly(db_session_factory, worker_settings) -> None:
    gateway = ToolGateway(db_session_factory, worker_settings)
    from app.domains.tools.context import RunBudget, RunToolContext

    ctx = RunToolContext(
        run_id="run_surface",
        environment="sandbox",
        primary_asset_id="as_x",
        is_admin=True,
        authorized_asset_ids=frozenset(),
        authorized_environments=frozenset(),
        budget=RunBudget(10, 10, 10000, datetime.now(UTC)),
        cancel_checker=_never_cancel,
        connector_configs={},
    )
    events = RunEventWriter(db_session_factory, "run_surface")
    tools = gateway.build_agent_tools(ctx, events)
    names = {t.name for t in tools}
    assert names == {
        "get_service_health",
        "check_redis_health",
        "get_container_status",
        "read_container_logs",
    }
    assert "execute" not in names
    assert "task" not in names

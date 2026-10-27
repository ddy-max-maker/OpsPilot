"""D2.1 双 Worker 接管与 fencing 集成测试（任务三）。

证明目标（不只是 finish_run 的 WHERE 条件）：
- 旧 Worker 失去租约后，RunEventWriter / Gateway 证据落库 / 报告写入
  / finish_run / heartbeat 全部失效——不产生任何有效业务写入；
- 新 Worker 安全接管：seq 续接唯一、正常完成全链路；
- 报告版本递增不覆盖（接管重跑场景）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text

from app.core.config import Settings
from app.core.enums import RunEventType, RunStatus
from app.domains.tools.context import (
    LeaseFence,
    LeaseLostError,
    RunBudget,
    RunEventWriter,
    RunToolContext,
)
from app.domains.tools.gateway import ToolGateway
from app.domains.tools.manifests import ALL_CAPABILITY_KEYS
from app.models import Asset, DiagnosticReport, Evidence, Hypothesis, RunEvent
from app.worker import claim
from tests.integration.test_worker import _execute, _make_runtime, _never_cancel, _seed_run


@pytest.fixture
def worker_settings() -> Settings:
    return Settings()


@pytest.fixture
async def worker_runtime(db_engine, db_session_factory, worker_settings, clean_db):
    """独立 AgentRuntime（真实 PG checkpoint 池 + Fake 模型）。"""
    runtime, pool = await _make_runtime(db_engine, db_session_factory, worker_settings)
    yield runtime
    await pool.close()


async def _force_lease_expiry(db_session_factory, run_id: str) -> None:
    """模拟旧 Worker 停滞：将租约到期时间拨回过去（新 Worker 可立即接管）。"""
    async with db_session_factory() as session:
        await session.execute(
            text("UPDATE investigation_runs SET claim_expires_at = :past WHERE id = :run_id"),
            {"past": datetime.now(UTC) - timedelta(seconds=1), "run_id": run_id},
        )
        await session.commit()


def _fence(db_session_factory, run_id: str, token: int, worker_id: str) -> LeaseFence:
    return LeaseFence(
        session_factory=db_session_factory,
        run_id=run_id,
        fence_token=token,
        worker_id=worker_id,
    )


async def _counts(db_session_factory, run_id: str) -> tuple[int, int, int]:
    async with db_session_factory() as session:
        events = int(
            await session.scalar(
                select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run_id)
            )
        )
        evidence = int(
            await session.scalar(
                select(func.count()).select_from(Evidence).where(Evidence.run_id == run_id)
            )
        )
        reports = int(
            await session.scalar(
                select(func.count())
                .select_from(DiagnosticReport)
                .where(DiagnosticReport.run_id == run_id)
            )
        )
    return events, evidence, reports


def _tool_ctx(db_session_factory, run_id: str, fence: LeaseFence | None) -> RunToolContext:
    return RunToolContext(
        run_id=run_id,
        environment="sandbox",
        primary_asset_id="",
        is_admin=True,
        authorized_asset_ids=frozenset(),
        authorized_environments=frozenset({"sandbox"}),
        budget=RunBudget(
            max_tool_calls=10,
            max_iterations=100,
            max_tokens=100000,
            deadline_at=datetime.now(UTC) + timedelta(seconds=300),
        ),
        cancel_checker=_never_cancel,
        connector_configs={},
        all_assets=True,
        lease_fence=fence,
    )


@pytest.fixture
async def seeded_assets_gw(db_engine, db_session_factory, clean_db):
    """Gateway 用例的沙箱 service 资产（幂等插入）。

    注意：必须用短生命周期 session 并在返回前关闭——若经由长生命周期
    db_session 返回，读取事务将保持 idle in transaction 并持有 assets 的
    ACCESS SHARE 锁，导致后续 _seed_run 的 TRUNCATE（ACCESS EXCLUSIVE）永久阻塞。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from tests.conftest import truncate_all

    await truncate_all(db_engine)
    async with db_session_factory() as session:
        stmt = (
            pg_insert(Asset)
            .values(
                id="as_gw_svc",
                name="gw-svc",
                environment="sandbox",
                asset_type="service",
                status="active",
                criticality="medium",
                endpoints={},
                owner=None,
                tags=[],
            )
            .on_conflict_do_nothing(index_elements=["name", "environment"])
        )
        await session.execute(stmt)
        await session.commit()
        asset = (
            await session.execute(
                select(Asset).where(Asset.name == "gw-svc", Asset.environment == "sandbox")
            )
        ).scalar_one()
        asset_id = asset.id
    return {"svc_id": asset_id}


async def test_takeover_blocks_stale_worker_business_writes(
    worker_runtime, db_session_factory, worker_settings, db_engine, seeded_assets_gw
) -> None:
    """旧 Worker（w1）租约被 w2 接管后：事件/证据/报告/终态回写全部被拒。"""
    run = await _seed_run(db_session_factory, db_engine)
    w1 = await claim.claim_next_run(db_session_factory, worker_id="w1", lease_ttl_seconds=60)
    assert w1 is not None and w1["fence_token"] == 1

    fence_w1 = _fence(db_session_factory, run.id, 1, "w1")
    events_w1 = await RunEventWriter.create(db_session_factory, run.id, lease_fence=fence_w1)

    # 接管前 w1 是合法所有者：事件写入成功
    seq0 = await events_w1.append(RunEventType.RUN_STARTED, {"by": "w1"})
    assert seq0 == 1

    # ---- 故障注入：旧 Worker 停滞，租约过期，w2 接管（fence_token=2）----
    await _force_lease_expiry(db_session_factory, run.id)
    w2 = await claim.claim_next_run(db_session_factory, worker_id="w2", lease_ttl_seconds=60)
    assert w2 is not None and w2["id"] == run.id and w2["fence_token"] == 2

    # ---- 旧 Worker 复活后的所有业务写入必须被拒绝 ----
    # 1) run_events
    with pytest.raises(LeaseLostError):
        await events_w1.append(RunEventType.TOOL_CALLED, {"capability": "late"})
    # 2) heartbeat / finish_run（fencing WHERE）
    assert not await claim.heartbeat(
        db_session_factory, run_id=run.id, fence_token=1, lease_ttl_seconds=60
    )
    assert not await claim.finish_run(
        db_session_factory,
        run_id=run.id,
        fence_token=1,
        status=RunStatus.COMPLETED.value,
        stop_reason="completed",
        usage={},
    )
    # 3) Gateway 证据链路（TOOL_CALLED 事件先于证据落库被拒）
    gateway = ToolGateway(db_session_factory, worker_settings)
    ctx_w1 = _tool_ctx(db_session_factory, run.id, fence_w1)
    spec = ALL_CAPABILITY_KEYS["get_service_health"]
    with pytest.raises(LeaseLostError):
        await gateway.execute_capability(
            ctx_w1, events_w1, spec.key, spec, {"asset_id": seeded_assets_gw["svc_id"]}
        )
    # 4) 报告写入
    stale_execution = await _execute(worker_runtime, db_session_factory, run.id)
    stale_execution.lease_fence = fence_w1
    with pytest.raises(LeaseLostError):
        await worker_runtime.write_fallback_report(stale_execution, reason="旧 Worker 迟到写入")

    # 旧 Worker 未产生任何有效业务写入（仅接管前那条合法事件）
    events_n, evidence_n, reports_n = await _counts(db_session_factory, run.id)
    assert (events_n, evidence_n, reports_n) == (1, 0, 0)

    # ---- 新 Worker 安全接管：seq 续接、全链路完成 ----
    fence_w2 = _fence(db_session_factory, run.id, 2, "w2")
    events_w2 = await RunEventWriter.create(db_session_factory, run.id, lease_fence=fence_w2)
    execution = await _execute(worker_runtime, db_session_factory, run.id)
    execution.events = events_w2
    execution.lease_fence = fence_w2
    outcome = await worker_runtime.execute_run(execution)
    assert outcome.status in (RunStatus.COMPLETED, RunStatus.NEEDS_HUMAN)

    finished = await claim.finish_run(
        db_session_factory,
        run_id=run.id,
        fence_token=2,
        status=outcome.status.value,
        stop_reason=outcome.stop_reason.value,
        usage=outcome.usage,
    )
    assert finished is True
    # 终态后合法所有者仍可补写 RUN_FINISHED（fence 未变）
    last_seq = await events_w2.append(RunEventType.RUN_FINISHED, {"by": "w2"})
    # seq 在接管后单调且唯一（create 从 MAX(seq) 续接 + (run_id, seq) 唯一约束）
    async with db_session_factory() as session:
        seq_list = list(
            (
                await session.execute(
                    select(RunEvent.seq).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
                )
            ).scalars()
        )
    assert last_seq == seq_list[-1]
    assert len(seq_list) == len(set(seq_list))
    assert seq_list[0] == 1
    _, _, reports_after = await _counts(db_session_factory, run.id)
    assert reports_after == 1


async def test_report_version_increments_never_overwrites(
    worker_runtime, db_session_factory, db_engine
) -> None:
    """同一 run 的报告重复写入（接管重跑场景）→ 版本递增，不覆盖。"""
    run = await _seed_run(db_session_factory, db_engine)
    execution = await _execute(worker_runtime, db_session_factory, run.id)
    await worker_runtime.write_fallback_report(execution, reason="第一次终止")
    await worker_runtime.write_fallback_report(execution, reason="第二次终止")
    async with db_session_factory() as session:
        versions = list(
            (
                await session.execute(
                    select(DiagnosticReport.version_no)
                    .where(DiagnosticReport.run_id == run.id)
                    .order_by(DiagnosticReport.version_no)
                )
            ).scalars()
        )
    assert versions == [1, 2]


async def test_supported_hypothesis_requires_ok_evidence(
    worker_runtime, db_session_factory, db_engine
) -> None:
    """任务四：仅由采集失败证据（error/unavailable）支撑的假设降级为 insufficient_data。"""
    from app.core.ids import new_id
    from app.models import Incident

    run = await _seed_run(db_session_factory, db_engine)
    async with db_session_factory() as session:
        incident = await session.get(Incident, run.incident_id)
        asset = await session.get(Asset, incident.primary_asset_id)
        ev_error = Evidence(
            id=new_id("ev_"),
            run_id=run.id,
            asset_id=asset.id,
            environment="sandbox",
            source_type="health",
            collector="http.health_check",
            observed_at=datetime.now(UTC),
            status="error",
            data_ref={"redacted": True, "content": {}},
            summary_redacted="http.health_check error (HTTP_504)",
            correlation={"service": asset.name},
            error_code="HTTP_504",
            truncated=False,
            schema_version="1",
        )
        ev_ok = Evidence(
            id=new_id("ev_"),
            run_id=run.id,
            asset_id=asset.id,
            environment="sandbox",
            source_type="infra",
            collector="docker.inspect",
            observed_at=datetime.now(UTC),
            status="ok",
            data_ref={"redacted": True, "content": {"state": "running"}},
            summary_redacted="docker.inspect OK: state=running",
            correlation={"service": asset.name},
            error_code=None,
            truncated=False,
            schema_version="1",
        )
        session.add_all([ev_error, ev_ok])
        await session.commit()
        err_id, ok_id = ev_error.id, ev_ok.id

    execution = await _execute(worker_runtime, db_session_factory, run.id)
    report_data: dict[str, Any] = {
        "summary": "验证假设判定",
        "key_findings": [
            {"finding": "失败证据不构成根因", "evidence_ids": [err_id, "ev_forged"]},
            {"finding": "正常取证支持结论", "evidence_ids": [ok_id]},
        ],
        "hypotheses": [
            {
                "rank": 1,
                "statement": "仅有连接器失败证据的假设",
                "status": "supported",
                "supporting_evidence_ids": [err_id],
                "contradicting_evidence_ids": [],
                "missing_information": [],
                "confidence": "high",
            },
            {
                "rank": 2,
                "statement": "有正常取证的假设",
                "status": "supported",
                "supporting_evidence_ids": [ok_id],
                "contradicting_evidence_ids": [],
                "missing_information": [],
                "confidence": "medium",
            },
        ],
        "unknown": {"is_unknown": False},
        "next_manual_steps": [],
    }
    budget = RunBudget(
        max_tool_calls=10,
        max_iterations=100,
        max_tokens=100000,
        deadline_at=datetime.now(UTC) + timedelta(seconds=300),
    )
    outcome = await worker_runtime._finalize_report(execution, report_data, budget)
    assert outcome.status is RunStatus.COMPLETED

    async with db_session_factory() as session:
        hyps = (
            (await session.execute(select(Hypothesis).where(Hypothesis.run_id == run.id)))
            .scalars()
            .all()
        )
        by_rank = {h.rank: h for h in hyps}
        assert by_rank[1].status == "insufficient_data"
        assert any("采集失败" in m for m in by_rank[1].missing_information)
        assert by_rank[2].status == "supported"
        report = (
            await session.execute(select(DiagnosticReport).where(DiagnosticReport.run_id == run.id))
        ).scalar_one()
        # 伪造引用被过滤：不存在/越权的 evidence id 不出现在报告与结论引用中
        assert set(report.evidence_refs or []) <= {err_id, ok_id}
        for finding in report.conclusion["key_findings"]:
            assert set(finding["evidence_ids"]) <= {err_id, ok_id}


async def test_worker_settings_shape(worker_settings: Settings) -> None:
    """心跳/租约配置存在于 Settings（后台心跳续租的依据）。"""
    assert worker_settings.worker_lease_ttl_seconds > 0
    assert (
        0
        < worker_settings.worker_heartbeat_interval_seconds
        < worker_settings.worker_lease_ttl_seconds
    )

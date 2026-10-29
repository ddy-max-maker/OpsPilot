"""诊断运行服务：创建（含 incident 状态转换）、取消、查询（CONTRACTS §2.4/§5）。

范围语义与事件领域一致：查看 = read 范围；发起/取消 = investigate 范围；
越权一律 404 伪装。Run 创建把 incident 推入 INVESTIGATING
（状态机：ACKNOWLEDGED/NEEDS_HUMAN → INVESTIGATING，INVESTIGATING 保持）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    RUN_TERMINAL_STATUSES,
    EvidenceSourceType,
    EvidenceStatus,
    IncidentStatus,
    RunStatus,
)
from app.core.errors import conflict_state, not_found, validation_error
from app.core.ids import new_id
from app.domains.access.scope import AssetScope
from app.domains.audit import service as audit_service
from app.domains.incidents.state_machine import require_transition
from app.domains.runs import repository
from app.domains.runs.schemas import (
    EvidenceListData,
    EvidenceResponse,
    HypothesisResponse,
    InvestigationCreateRequest,
    ReportResponse,
    RunCreatedData,
    RunEventData,
    RunEventListData,
    RunResponse,
)
from app.models import Asset, Incident, InvestigationRun, RunEvent

if TYPE_CHECKING:
    from app.core.config import Settings

# 发起 run 允许的事件状态（状态机：→ INVESTIGATING）
_RUNNABLE_INCIDENT_STATUSES = frozenset(
    {
        IncidentStatus.ACKNOWLEDGED,
        IncidentStatus.INVESTIGATING,
        IncidentStatus.NEEDS_HUMAN,
    }
)


def _to_run_response(run: InvestigationRun) -> RunResponse:
    return RunResponse(
        id=run.id,
        incident_id=run.incident_id,
        status=str(run.status),
        stop_reason=run.stop_reason,
        triggered_by=run.triggered_by,
        model_config_snapshot=dict(run.model_config_json or {}),
        budgets=dict(run.budgets or {}),
        usage=dict(run.usage or {}),
        langgraph_thread_id=run.langgraph_thread_id,
        cancel_requested=bool(run.cancel_requested),
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _snapshot_from_scope(scope: AssetScope) -> dict[str, Any]:
    """授权快照：创建时刻的 investigate 范围（CONTRACTS §2.4）。"""
    return {
        "is_admin": scope.is_admin,
        "all_assets": scope.all_assets,
        "asset_ids": sorted(scope.asset_ids),
        "environments": sorted(scope.environments),
    }


async def _load_scoped_incident(
    session: AsyncSession, incident_id: str, scope: AssetScope
) -> Incident:
    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise not_found("incident not found")
    if not scope.allows(incident.primary_asset_id, str(incident.environment)):
        raise not_found("incident not found")
    return incident


async def create_run(
    session: AsyncSession,
    settings: Settings,
    incident_id: str,
    scope: AssetScope,
    actor_id: str,
    req: InvestigationCreateRequest,
    *,
    commit: bool = True,
) -> RunCreatedData:
    """创建诊断 run：校验 → 状态转换 → 预算/快照落库 → 审计。

    commit=False 供幂等包装在同事务内存储 202 响应。
    """
    incident = await _load_scoped_incident(session, incident_id, scope)
    current_status = IncidentStatus(str(incident.status))
    if current_status not in _RUNNABLE_INCIDENT_STATUSES:
        raise conflict_state(
            "incident must be acknowledged (or investigating/needs_human) before starting a run",
            details={"from": current_status.value},
        )
    asset = await session.get(Asset, incident.primary_asset_id)
    if asset is None:  # 理论不可达（FK 保证）
        raise not_found("asset not found")

    # 事件状态机推进：ACKNOWLEDGED/NEEDS_HUMAN → INVESTIGATING
    if current_status is not IncidentStatus.INVESTIGATING:
        require_transition(current_status, IncidentStatus.INVESTIGATING)
        incident.status = IncidentStatus.INVESTIGATING.value
        session.add(incident)

    deadline = datetime.now(UTC) + timedelta(
        seconds=req.deadline_seconds or settings.budget_deadline_seconds
    )
    budgets: dict[str, Any] = {
        "max_tool_calls": req.max_tool_calls or settings.budget_max_tool_calls,
        "max_iterations": req.max_iterations or settings.budget_max_iterations,
        "max_tokens": req.max_tokens or settings.budget_max_tokens,
        "deadline_at": deadline.isoformat(),
    }
    model_config_snapshot: dict[str, Any] = {
        "provider": settings.agent_provider,
        "model_name": settings.agent_model_name,
        "prompt_version": settings.agent_prompt_version,
        # D7 修复：此前硬编码 "off"，导致 RAG 开启时 run 快照仍记录 off，
        # 违反 CONTRACTS（RAG 开关必须进 run model_config），且让 RAG on/off
        # 对照实验无法按快照分组。此处取部署配置的真实值。
        "rag": settings.rag_mode,
    }
    run_id = new_id("run_")
    session.add(
        InvestigationRun(
            id=run_id,
            incident_id=incident.id,
            status=RunStatus.QUEUED.value,
            triggered_by=actor_id,
            model_config_json=model_config_snapshot,
            budgets=budgets,
            usage={},
            authorization_snapshot=_snapshot_from_scope(scope),
            langgraph_thread_id=run_id,
            cancel_requested=False,
        )
    )
    await session.flush()
    await audit_service.record(
        session,
        actor_type="user",
        actor_id=actor_id,
        action="run.create",
        resource_type="run",
        resource_id=run_id,
        environment=str(incident.environment),
        detail={
            "incident_id": incident.id,
            "budgets": budgets,
            "from_incident_status": current_status.value,
        },
    )
    if commit:
        await session.commit()
    return RunCreatedData(run_id=run_id, status=RunStatus.QUEUED.value, incident_id=incident.id)


async def get_run_detail(session: AsyncSession, run_id: str, scope: AssetScope) -> RunResponse:
    run = await repository.get_run(session, run_id)
    if run is None:
        raise not_found("run not found")
    incident = await session.get(Incident, run.incident_id)
    if incident is None or not scope.allows(incident.primary_asset_id, str(incident.environment)):
        raise not_found("run not found")
    return _to_run_response(run)


async def cancel_run(
    session: AsyncSession, run_id: str, scope: AssetScope, actor_id: str
) -> RunResponse:
    """合作式取消：queued 直接终态；running 置取消标志由 Worker 收敛。"""
    run = await repository.get_run(session, run_id)
    if run is None:
        raise not_found("run not found")
    incident = await session.get(Incident, run.incident_id)
    if incident is None or not scope.allows(incident.primary_asset_id, str(incident.environment)):
        raise not_found("run not found")
    status = RunStatus(str(run.status))
    if status in RUN_TERMINAL_STATUSES:
        raise conflict_state("run already finished", details={"status": status.value})
    now = datetime.now(UTC)
    if status in (RunStatus.CREATED, RunStatus.QUEUED):
        run.status = RunStatus.CANCELED.value
        run.stop_reason = "canceled"
        run.finished_at = now
    else:
        run.cancel_requested = True
    session.add(run)
    await audit_service.record(
        session,
        actor_type="user",
        actor_id=actor_id,
        action="run.cancel",
        resource_type="run",
        resource_id=run.id,
        environment=str(incident.environment),
        detail={"from_status": status.value},
    )
    await session.commit()
    await session.refresh(run)
    return _to_run_response(run)


def _ensure_run_scoped(incident: Incident | None, scope: AssetScope) -> None:
    if incident is None or not scope.allows(incident.primary_asset_id, str(incident.environment)):
        raise not_found("run not found")


async def list_run_events(
    session: AsyncSession,
    settings: Settings,
    run_id: str,
    scope: AssetScope,
    *,
    after_seq: int,
    limit: int,
) -> RunEventListData:
    """事件流（JSON 分页为默认响应；D2 无 SSE，CONTRACTS §1.3）。"""
    if limit < 1 or limit > settings.max_page_size:
        raise validation_error(f"limit must be in [1, {settings.max_page_size}]")
    if after_seq < 0:
        raise validation_error("after_seq must be >= 0")
    run = await repository.get_run(session, run_id)
    if run is None:
        raise not_found("run not found")
    from app.models import Incident  # noqa: PLC0415

    incident = await session.get(Incident, run.incident_id)
    _ensure_run_scoped(incident, scope)
    rows = await repository.list_events(session, run_id, after_seq=after_seq, limit=limit)
    has_more = len(rows) > limit
    items = rows[:limit]
    next_after_seq = items[-1].seq if items else after_seq
    return RunEventListData(
        items=[_event_to_response(e) for e in items],
        next_after_seq=int(next_after_seq),
        has_more=has_more,
    )


def _event_to_response(event: RunEvent) -> RunEventData:
    return RunEventData(
        id=int(event.id),
        run_id=event.run_id,
        seq=int(event.seq),
        type=str(event.type),
        payload=dict(event.payload or {}),
        created_at=event.created_at,
    )


async def list_run_evidence(
    session: AsyncSession,
    settings: Settings,
    run_id: str,
    scope: AssetScope,
    *,
    source_type: EvidenceSourceType | None,
    status: EvidenceStatus | None,
    limit: int,
    cursor: str | None,
) -> EvidenceListData:
    if limit < 1 or limit > settings.max_page_size:
        raise validation_error(f"limit must be in [1, {settings.max_page_size}]")
    run = await repository.get_run(session, run_id)
    if run is None:
        raise not_found("run not found")
    from app.models import Incident  # noqa: PLC0415

    incident = await session.get(Incident, run.incident_id)
    _ensure_run_scoped(incident, scope)
    rows = await repository.list_evidence(
        session,
        run_id,
        source_type=source_type,
        status=status,
        limit=limit,
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    return EvidenceListData(
        items=[_evidence_to_response(e) for e in items],
        next_cursor=None,
        has_more=has_more,
    )


def _evidence_to_response(evidence: Any) -> EvidenceResponse:
    return EvidenceResponse(
        id=evidence.id,
        run_id=evidence.run_id,
        asset_id=evidence.asset_id,
        environment=str(evidence.environment),
        source_type=str(evidence.source_type),
        collector=str(evidence.collector),
        observed_at=evidence.observed_at,
        time_range=evidence.time_range,
        status=str(evidence.status),
        data_ref=evidence.data_ref,
        summary_redacted=evidence.summary_redacted,
        correlation=evidence.correlation,
        error_code=evidence.error_code,
        truncated=bool(evidence.truncated),
        schema_version=str(evidence.schema_version),
        created_at=evidence.created_at,
    )


async def get_run_report(session: AsyncSession, run_id: str, scope: AssetScope) -> ReportResponse:
    """最新版本报告快照（ADR-13）；无报告 → 404。"""
    run = await repository.get_run(session, run_id)
    if run is None:
        raise not_found("run not found")
    from app.models import Incident  # noqa: PLC0415

    incident = await session.get(Incident, run.incident_id)
    _ensure_run_scoped(incident, scope)
    report = await repository.latest_report(session, run_id)
    if report is None:
        raise not_found("report not available yet")
    hypotheses = await repository.list_hypotheses(session, run_id)
    return ReportResponse(
        id=report.id,
        run_id=report.run_id,
        version_no=int(report.version_no),
        status=str(report.status),
        conclusion=dict(report.conclusion or {}),
        hypotheses=[
            HypothesisResponse(
                id=h.id,
                rank=int(h.rank),
                statement=h.statement,
                status=str(h.status),
                supporting_evidence_ids=[str(r) for r in (h.supporting_evidence_ids or [])],
                contradicting_evidence_ids=[str(r) for r in (h.contradicting_evidence_ids or [])],
                missing_information=[str(r) for r in (h.missing_information or [])],
                confidence=str(h.confidence),
                created_at=h.created_at,
            )
            for h in hypotheses
        ],
        evidence_refs=[str(r) for r in (report.evidence_refs or [])],
        unknown_note=report.unknown_note,
        generator=report.generator,
        generated_at=report.generated_at,
        created_at=report.created_at,
    )

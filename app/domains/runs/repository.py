"""诊断运行仓储（查询封装，不含事务提交）。"""

from __future__ import annotations

from sqlalchemy import ColumnExpressionArgument, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import EvidenceSourceType, EvidenceStatus
from app.models import (
    DiagnosticReport,
    Evidence,
    Hypothesis,
    InvestigationRun,
    RunEvent,
)


async def get_run(session: AsyncSession, run_id: str) -> InvestigationRun | None:
    return await session.get(InvestigationRun, run_id)


async def max_event_seq(session: AsyncSession, run_id: str) -> int:
    return int(
        await session.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)) or 0
    )


async def list_events(
    session: AsyncSession,
    run_id: str,
    *,
    after_seq: int,
    limit: int,
) -> list[RunEvent]:
    """seq 升序增量拉取（CONTRACTS §2.5：after_seq 增量；JSON 分页为默认）。"""
    stmt = (
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
        .order_by(RunEvent.seq)
        .limit(limit + 1)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_evidence(
    session: AsyncSession,
    run_id: str,
    *,
    source_type: EvidenceSourceType | None = None,
    status: EvidenceStatus | None = None,
    limit: int,
) -> list[Evidence]:
    conditions: list[ColumnExpressionArgument[bool]] = [Evidence.run_id == run_id]
    if source_type is not None:
        conditions.append(Evidence.source_type == source_type.value)
    if status is not None:
        conditions.append(Evidence.status == status.value)
    stmt = (
        select(Evidence)
        .where(*conditions)
        .order_by(Evidence.created_at, Evidence.id)
        .limit(limit + 1)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_hypotheses(session: AsyncSession, run_id: str) -> list[Hypothesis]:
    stmt = select(Hypothesis).where(Hypothesis.run_id == run_id).order_by(Hypothesis.rank)
    return list((await session.execute(stmt)).scalars().all())


async def latest_report(session: AsyncSession, run_id: str) -> DiagnosticReport | None:
    stmt = (
        select(DiagnosticReport)
        .where(DiagnosticReport.run_id == run_id)
        .order_by(DiagnosticReport.version_no.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()

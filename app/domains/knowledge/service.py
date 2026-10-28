"""知识生命周期服务（D4）：状态机/四眼复核/审计/发布索引/报告转草稿。

红线：
- 发布必须经人工复核（reviewed_by ≠ created_by，服务端强制）；
- 发布/退役/版本变更写审计（PRD §6）；
- 发布时构建分块与向量索引（版本快照落库；不同 embedding 版本不混用）；
- 历史报告 → 知识草稿仅由人工触发，绝不自动复核/发布（不自我强化）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ActorType
from app.core.errors import conflict_state, not_found
from app.core.ids import new_id
from app.domains.audit import service as audit_service
from app.domains.knowledge.schemas import AppliesTo
from app.domains.rag.embeddings import EmbeddingProvider
from app.domains.rag.retrieval import build_version_embeddings
from app.models import (
    DiagnosticReport,
    InvestigationRun,
    KnowledgeDocument,
    KnowledgeStatus,
    KnowledgeVersion,
    Runbook,
    RunbookStatus,
    RunbookVersion,
)

VALID_DOC_TYPES = ("runbook", "postmortem", "guide")


def require_kb_transition(current: str, target: str) -> None:
    """知识版本状态机：非法转换 409（CONTRACTS §2.9）。"""
    if target not in KnowledgeStatus.TRANSITIONS.get(current, frozenset()):
        raise conflict_state(
            "knowledge version status transition is not allowed",
            details={"from": current, "to": target},
        )


def _applies_to_dict(applies: AppliesTo | None) -> dict[str, Any]:
    data = applies.model_dump() if applies else AppliesTo().model_dump()
    return data


async def get_document_or_404(session: AsyncSession, document_id: str) -> KnowledgeDocument:
    doc = await session.get(KnowledgeDocument, document_id)
    if doc is None:
        raise not_found("knowledge document not found")
    return doc


async def get_version_or_404(session: AsyncSession, version_id: str) -> KnowledgeVersion:
    version = await session.get(KnowledgeVersion, version_id)
    if version is None:
        raise not_found("knowledge version not found")
    return version


# ---- 创建 ----


async def create_document(
    session: AsyncSession,
    *,
    actor_id: str,
    doc_type: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
    source_incident_id: str | None = None,
    sensitivity: str = "internal",
    applies_to: AppliesTo | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    change_note: str | None = None,
) -> tuple[KnowledgeDocument, KnowledgeVersion]:
    """创建文档 + v1 草稿（创建者 = owner；草稿不可检索）。"""
    if source_incident_id is not None:
        from app.models import Incident  # noqa: PLC0415

        if await session.get(Incident, source_incident_id) is None:
            raise not_found("source incident not found")
    doc = KnowledgeDocument(
        id=new_id("kb_"),
        doc_type=doc_type,
        title=title,
        source_incident_id=source_incident_id,
        owner=actor_id,
        tags=list(tags or []),
    )
    version = KnowledgeVersion(
        id=new_id("kbv_"),
        document_id=doc.id,
        version_no=1,
        status=KnowledgeStatus.DRAFT,
        content=content,
        change_note=change_note,
        created_by=actor_id,
        sensitivity=sensitivity,
        applies_to=_applies_to_dict(applies_to),
        valid_from=valid_from,
        valid_until=valid_until,
    )
    session.add_all([doc, version])
    await audit_service.record(
        session,
        actor_type=ActorType.USER.value,
        actor_id=actor_id,
        action="knowledge.created",
        resource_type="knowledge_document",
        resource_id=doc.id,
        environment=None,
        detail={"doc_type": doc_type, "version_id": version.id},
    )
    return doc, version


async def create_version(
    session: AsyncSession,
    *,
    actor_id: str,
    document_id: str,
    content: str,
    change_note: str | None = None,
    applies_to: AppliesTo | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> KnowledgeVersion:
    """追加新草稿版本（发布新版本时旧 published 版本自动 superseded）。"""
    doc = await get_document_or_404(session, document_id)
    next_no = int(
        await session.scalar(
            select(func.max(KnowledgeVersion.version_no)).where(
                KnowledgeVersion.document_id == document_id
            )
        )
        or 0
    )
    version = KnowledgeVersion(
        id=new_id("kbv_"),
        document_id=doc.id,
        version_no=next_no + 1,
        status=KnowledgeStatus.DRAFT,
        content=content,
        change_note=change_note,
        created_by=actor_id,
        applies_to=_applies_to_dict(applies_to)
        if applies_to is not None
        else None,  # None = 沿用文档上一版本的适用范围
        valid_from=valid_from,
        valid_until=valid_until,
    )
    if applies_to is None:
        last = (
            await session.execute(
                select(KnowledgeVersion)
                .where(KnowledgeVersion.document_id == document_id)
                .order_by(KnowledgeVersion.version_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if last is not None and last.applies_to is not None:
            version.applies_to = dict(last.applies_to)
    session.add(version)
    await audit_service.record(
        session,
        actor_type=ActorType.USER.value,
        actor_id=actor_id,
        action="knowledge.version_created",
        resource_type="knowledge_document",
        resource_id=document_id,
        environment=None,
        detail={"version_id": version.id, "version_no": version.version_no},
    )
    return version


# ---- 人工复核（四眼）与发布 ----


async def review_version(
    session: AsyncSession, *, actor_id: str, version_id: str
) -> KnowledgeVersion:
    """人工复核：draft → reviewed；复核人不得等于创建人（四眼原则）。"""
    version = await get_version_or_404(session, version_id)
    require_kb_transition(version.status, KnowledgeStatus.REVIEWED)
    if version.created_by == actor_id:
        raise conflict_state(
            "four-eyes violation: reviewer must differ from the author",
            details={"version_id": version_id},
        )
    version.status = KnowledgeStatus.REVIEWED
    version.reviewed_by = actor_id
    await audit_service.record(
        session,
        actor_type=ActorType.USER.value,
        actor_id=actor_id,
        action="knowledge.reviewed",
        resource_type="knowledge_version",
        resource_id=version_id,
        environment=None,
        detail={"document_id": version.document_id, "version_no": version.version_no},
    )
    return version


async def publish_version(
    session: AsyncSession,
    session_factory: Any,
    *,
    actor_id: str,
    version_id: str,
    provider: EmbeddingProvider,
) -> KnowledgeVersion:
    """发布：reviewed → published；构建向量索引；旧 published 版本自动 superseded。"""
    version = await get_version_or_404(session, version_id)
    require_kb_transition(version.status, KnowledgeStatus.PUBLISHED)
    if version.reviewed_by is None:
        raise conflict_state("version must be reviewed before publish")
    if version.reviewed_by == version.created_by:
        raise conflict_state("four-eyes violation recorded on this version")
    now = datetime.now(UTC)
    if version.valid_from is None:
        version.valid_from = now
    version.status = KnowledgeStatus.PUBLISHED
    version.published_at = now
    # 同文档其他 published 版本 → superseded（状态机合法转换）
    others = (
        (
            await session.execute(
                select(KnowledgeVersion).where(
                    KnowledgeVersion.document_id == version.document_id,
                    KnowledgeVersion.status == KnowledgeStatus.PUBLISHED,
                    KnowledgeVersion.id != version.id,
                )
            )
        )
        .scalars()
        .all()
    )
    for other in others:
        require_kb_transition(other.status, KnowledgeStatus.SUPERSEDED)
        other.status = KnowledgeStatus.SUPERSEDED
    await session.commit()
    # 向量索引（独立事务：发布成功后构建；幂等）
    await build_version_embeddings(
        session_factory,
        version_id=version.id,
        provider=provider,
    )
    async with session_factory() as session2:
        await audit_service.record(
            session2,
            actor_type=ActorType.USER.value,
            actor_id=actor_id,
            action="knowledge.published",
            resource_type="knowledge_version",
            resource_id=version.id,
            environment=None,
            detail={
                "document_id": version.document_id,
                "version_no": version.version_no,
                "embedding_model_version": provider.model_version,
            },
        )
        await session2.commit()
    return version


async def retire_version(
    session: AsyncSession, *, actor_id: str, version_id: str
) -> KnowledgeVersion:
    """退役：draft/reviewed/published → retired（立即退出检索范围）。"""
    version = await get_version_or_404(session, version_id)
    require_kb_transition(version.status, KnowledgeStatus.RETIRED)
    version.status = KnowledgeStatus.RETIRED
    await audit_service.record(
        session,
        actor_type=ActorType.USER.value,
        actor_id=actor_id,
        action="knowledge.retired",
        resource_type="knowledge_version",
        resource_id=version_id,
        environment=None,
        detail={"document_id": version.document_id, "version_no": version.version_no},
    )
    return version


# ---- 报告 → 知识草稿（人工触发；不自动复核/发布）----


def _compose_postmortem(report: DiagnosticReport, run: InvestigationRun) -> str:
    """从诊断报告快照生成复盘草稿正文（结构化引用，可追溯）。"""
    conclusion = report.conclusion or {}
    findings = cast("list[dict[str, Any]]", conclusion.get("key_findings") or [])
    steps = cast("list[str]", conclusion.get("next_manual_steps") or [])
    lines = [
        f"# 复盘草稿（来源 run {run.id}）",
        "",
        str(conclusion.get("summary") or ""),
        "",
        "## 关键发现",
    ]
    for finding in findings:
        refs = ", ".join(str(r) for r in finding.get("evidence_ids") or [])
        lines.append(f"- {finding.get('finding')}" + (f"（证据：{refs}）" if refs else ""))
    lines.append("")
    lines.append("## 候选根因")
    unknown = report.unknown_note or {}
    if unknown.get("is_unknown"):
        lines.append(f"- 结论为待确认：{unknown.get('reason') or ''}")
    else:
        lines.append("- 见关联诊断报告（hypotheses 快照）")
    lines.append("")
    lines.append("## 后续建议")
    for step in steps:
        lines.append(f"- {step}")
    return "\n".join(lines)


async def create_draft_from_report(
    session: AsyncSession,
    *,
    actor_id: str,
    run_id: str,
    title: str | None = None,
) -> tuple[KnowledgeDocument, KnowledgeVersion]:
    """把诊断报告转为知识草稿（doc_type=postmortem；状态恒为 draft）。"""
    report = (
        await session.execute(
            select(DiagnosticReport)
            .where(DiagnosticReport.run_id == run_id)
            .order_by(DiagnosticReport.version_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    run = await session.get(InvestigationRun, run_id)
    if report is None or run is None:
        raise not_found("diagnostic report for run not found")
    doc, version = await create_document(
        session,
        actor_id=actor_id,
        doc_type="postmortem",
        title=title or f"复盘：{run_id}",
        content=_compose_postmortem(report, run),
        source_incident_id=run.incident_id,
        change_note="由诊断报告生成（人工触发，未复核）",
    )
    return doc, version


# ---- Runbook（CONTRACTS §2.10；草稿不可执行）----


def _validate_runbook_environments(envs: list[str]) -> None:
    """allowed_environments 硬排除 production（DATA_MODEL §3.6 迁移断言）。"""
    if "production" in envs:
        raise conflict_state("runbook allowed_environments must not contain production")


async def create_runbook(
    session: AsyncSession,
    *,
    actor_id: str,
    name: str,
    description: str | None,
    risk_level: str,
    steps: list[dict[str, Any]],
    preconditions: dict[str, Any] | None,
    parameters_schema: dict[str, Any] | None,
    allowed_environments: list[str],
    allowlist_action_keys: list[str],
) -> tuple[Runbook, RunbookVersion]:
    """创建 Runbook + v1 草稿版本（无执行链路；D5 才消费已发布版本）。"""
    _validate_runbook_environments(allowed_environments)
    runbook = Runbook(
        id=new_id("rb_"),
        name=name,
        description=description,
        risk_level=risk_level,
        status=RunbookStatus.DRAFT,
        created_by=actor_id,
    )
    version = RunbookVersion(
        id=new_id("rbv_"),
        runbook_id=runbook.id,
        version_no=1,
        status=RunbookStatus.DRAFT,
        steps=steps,
        preconditions=preconditions,
        parameters_schema=parameters_schema,
        allowed_environments=allowed_environments,
        allowlist_action_keys=allowlist_action_keys,
        risk_level=risk_level,
        created_by=actor_id,
    )
    session.add_all([runbook, version])
    await audit_service.record(
        session,
        actor_type=ActorType.USER.value,
        actor_id=actor_id,
        action="runbook.created",
        resource_type="runbook",
        resource_id=runbook.id,
        environment=None,
        detail={"version_id": version.id},
    )
    return runbook, version


async def create_runbook_version(
    session: AsyncSession,
    *,
    actor_id: str,
    runbook_id: str,
    steps: list[dict[str, Any]],
    preconditions: dict[str, Any] | None,
    parameters_schema: dict[str, Any] | None,
    allowed_environments: list[str],
    allowlist_action_keys: list[str],
    risk_level: str | None,
) -> RunbookVersion:
    runbook = await session.get(Runbook, runbook_id)
    if runbook is None:
        raise not_found("runbook not found")
    _validate_runbook_environments(allowed_environments)
    next_no = int(
        await session.scalar(
            select(func.max(RunbookVersion.version_no)).where(
                RunbookVersion.runbook_id == runbook_id
            )
        )
        or 0
    )
    version = RunbookVersion(
        id=new_id("rbv_"),
        runbook_id=runbook_id,
        version_no=next_no + 1,
        status=RunbookStatus.DRAFT,
        steps=steps,
        preconditions=preconditions,
        parameters_schema=parameters_schema,
        allowed_environments=allowed_environments,
        allowlist_action_keys=allowlist_action_keys,
        risk_level=risk_level or runbook.risk_level,
        created_by=actor_id,
    )
    session.add(version)
    await audit_service.record(
        session,
        actor_type=ActorType.USER.value,
        actor_id=actor_id,
        action="runbook.version_created",
        resource_type="runbook",
        resource_id=runbook_id,
        environment=None,
        detail={"version_id": version.id, "version_no": version.version_no},
    )
    return version


async def publish_runbook_version(
    session: AsyncSession, *, actor_id: str, version_id: str
) -> RunbookVersion:
    """发布 Runbook 版本：draft → published；旧 published 版本退役。"""
    version = await session.get(RunbookVersion, version_id)
    if version is None:
        raise not_found("runbook version not found")
    if version.status != RunbookStatus.DRAFT:
        raise conflict_state(
            "runbook version status transition is not allowed",
            details={"from": version.status, "to": RunbookStatus.PUBLISHED},
        )
    version.status = RunbookStatus.PUBLISHED
    version.published_at = datetime.now(UTC)
    others = (
        (
            await session.execute(
                select(RunbookVersion).where(
                    RunbookVersion.runbook_id == version.runbook_id,
                    RunbookVersion.status == RunbookStatus.PUBLISHED,
                    RunbookVersion.id != version.id,
                )
            )
        )
        .scalars()
        .all()
    )
    for other in others:
        other.status = RunbookStatus.RETIRED
    runbook = await session.get(Runbook, version.runbook_id)
    if runbook is not None:
        runbook.status = RunbookStatus.PUBLISHED
    await audit_service.record(
        session,
        actor_type=ActorType.USER.value,
        actor_id=actor_id,
        action="runbook.published",
        resource_type="runbook_version",
        resource_id=version_id,
        environment=None,
        detail={"runbook_id": version.runbook_id, "version_no": version.version_no},
    )
    return version


async def retire_runbook_version(
    session: AsyncSession, *, actor_id: str, version_id: str
) -> RunbookVersion:
    version = await session.get(RunbookVersion, version_id)
    if version is None:
        raise not_found("runbook version not found")
    if version.status == RunbookStatus.RETIRED:
        raise conflict_state("runbook version already retired")
    version.status = RunbookStatus.RETIRED
    await audit_service.record(
        session,
        actor_type=ActorType.USER.value,
        actor_id=actor_id,
        action="runbook.retired",
        resource_type="runbook_version",
        resource_id=version_id,
        environment=None,
        detail={"runbook_id": version.runbook_id, "version_no": version.version_no},
    )
    return version

"""Proposals API（CONTRACTS §5 / §2.11–§2.14，D5 交付）：

- POST /proposals                     创建处置提案（策略校验 → pending_approval；幂等端点）
- GET  /proposals/{id}                详情聚合（提案 + 审批 + 执行 + 验证）
- GET  /proposals                     列表（按 run/incident/状态过滤）
- POST /proposals/{id}/dry-run        只读前置检查（不发起任何外部动作）
- POST /proposals/{id}/approve        审批通过（四眼：approver/admin 且 ≠ 发起人）
- POST /proposals/{id}/reject         审批拒绝
- POST /proposals/{id}/execute        执行（审批复核 → 幂等 → Outbox 同事务入队）

安全约定：production 目标一律 403（Admin 也不行）；r2/r3 不进执行链路；
审批/执行均在服务层二次校验角色与快照（不依赖 API 层单一拦截）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.deps import (
    CurrentUser,
    ReadyUserDep,
    SessionDep,
    SettingsDep,
    get_asset_scope_for,
    require_roles,
)
from app.core.db import get_session_factory
from app.core.errors import idempotency_key_required
from app.core.responses import Envelope, ok
from app.domains.access.scope import AssetScope
from app.domains.approvals import service as proposals_service
from app.domains.approvals.schemas import (
    ApprovalView,
    DryRunResponse,
    ExecutionView,
    ProposalCreateRequest,
    ProposalDecisionRequest,
    ProposalDetailResponse,
    ProposalView,
    VerificationView,
)
from app.domains.idempotency import service as idempotency_service
from app.models import Proposal

router = APIRouter(prefix="/proposals", tags=["proposals"])

ApproverDep = Annotated[CurrentUser, Depends(require_roles("approver", "admin"))]
ProposalActorDep = Annotated[CurrentUser, Depends(require_roles("operator", "approver", "admin"))]
# 执行目标必须处于当前用户 execute 权限维度范围（Admin 天然全量）
ExecuteScopeDep = Annotated[AssetScope, Depends(get_asset_scope_for("execute"))]


def _proposal_view(p: Proposal) -> dict[str, Any]:
    return ProposalView.model_validate(p).model_dump(mode="json")


def idempotency_key_header(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    """POST /proposals 复用 Idempotency-Key 约定（Header 形态，与 incidents 一致）。"""
    return idempotency_key


IdempotencyKeyDep = Annotated[str | None, Depends(idempotency_key_header)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_proposal(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    current: ProposalActorDep,
    body: ProposalCreateRequest,
    idempotency_key: IdempotencyKeyDep,
) -> JSONResponse:
    """创建处置提案（幂等端点；只能引用已发布 Runbook 版本；production 直接拒绝）。"""
    if idempotency_key is None:
        raise idempotency_key_required()
    raw_body = await request.body()
    endpoint = idempotency_service.endpoint_tag("POST", request.url.path)

    async def execute() -> tuple[int, dict[str, Any]]:
        proposal = await proposals_service.create_proposal(
            session,
            get_session_factory(),
            actor_id=current.user.id,
            run_id=body.run_id,
            runbook_version_id=body.runbook_version_id,
            target_asset_id=body.target_asset_id,
            environment=body.environment.value,
            parameters=body.parameters,
            comment=body.comment,
        )
        envelope: Envelope[Any] = ok({"proposal": _proposal_view(proposal)})
        await session.commit()
        await session.refresh(proposal)
        return status.HTTP_201_CREATED, envelope.model_dump(mode="json")

    status_code, resp_body, replayed = await idempotency_service.run_idempotent(
        session,
        settings,
        user_id=current.user.id,
        endpoint=endpoint,
        key=idempotency_key,
        request_hash=idempotency_service.hash_request_body(raw_body),
        execute=execute,
    )
    headers = {"Idempotency-Replayed": "true"} if replayed else None
    return JSONResponse(status_code=status_code, content=resp_body, headers=headers)


@router.get("")
async def list_proposals(
    session: SessionDep,
    current: ReadyUserDep,
    run_id: str | None = None,
    incident_id: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Any:
    """提案列表（认证用户；按 run/incident/状态过滤）。"""
    stmt = select(Proposal).order_by(Proposal.created_at.desc()).limit(limit)
    if run_id:
        stmt = stmt.where(Proposal.run_id == run_id)
    if incident_id:
        stmt = stmt.where(Proposal.incident_id == incident_id)
    if status_filter:
        stmt = stmt.where(Proposal.status == status_filter)
    rows = (await session.execute(stmt)).scalars().all()
    return ok({"proposals": [_proposal_view(p) for p in rows]})


@router.get("/{proposal_id}")
async def get_proposal(session: SessionDep, current: ReadyUserDep, proposal_id: str) -> Any:
    """提案聚合详情：提案 + 审批记录 + 执行 + 验证。"""
    proposal, approvals, executions, verifications = await proposals_service.load_proposal_detail(
        session, proposal_id
    )
    return ok(
        ProposalDetailResponse(
            proposal=ProposalView.model_validate(proposal),
            approvals=[ApprovalView.model_validate(a) for a in approvals],
            executions=[ExecutionView.model_validate(e) for e in executions],
            verifications=[VerificationView.model_validate(v) for v in verifications],
        ).model_dump(mode="json")
    )


@router.post("/{proposal_id}/dry-run")
async def dry_run_proposal(session: SessionDep, current: ProposalActorDep, proposal_id: str) -> Any:
    """只读前置检查（写检查快照；不发起任何外部动作）。"""
    proposal, checks = await proposals_service.dry_run_proposal(
        session, proposal_id=proposal_id, actor_id=current.user.id
    )
    await session.commit()
    return ok(
        DryRunResponse(
            proposal_id=proposal.id,
            passed=all(c.passed for c in checks),
            checks=checks,
            preconditions_result=proposal.preconditions_result or {},
        ).model_dump(mode="json")
    )


@router.post("/{proposal_id}/approve")
async def approve_proposal(
    session: SessionDep, approver: ApproverDep, proposal_id: str, body: ProposalDecisionRequest
) -> Any:
    """审批通过（四眼 + 审批快照；记录只追加）。"""
    proposal, approval = await proposals_service.decide_proposal(
        session,
        get_session_factory(),
        proposal_id=proposal_id,
        approver_id=approver.user.id,
        approver_role_codes=approver.role_codes,
        decision="approved",
        comment=body.comment,
    )
    await session.commit()
    await session.refresh(proposal)
    return ok(
        {
            "proposal": _proposal_view(proposal),
            "approval": ApprovalView.model_validate(approval).model_dump(mode="json"),
        }
    )


@router.post("/{proposal_id}/reject")
async def reject_proposal(
    session: SessionDep, approver: ApproverDep, proposal_id: str, body: ProposalDecisionRequest
) -> Any:
    """审批拒绝（记录只追加）。"""
    proposal, approval = await proposals_service.decide_proposal(
        session,
        get_session_factory(),
        proposal_id=proposal_id,
        approver_id=approver.user.id,
        approver_role_codes=approver.role_codes,
        decision="rejected",
        comment=body.comment,
    )
    await session.commit()
    await session.refresh(proposal)
    return ok(
        {
            "proposal": _proposal_view(proposal),
            "approval": ApprovalView.model_validate(approval).model_dump(mode="json"),
        }
    )


@router.post("/{proposal_id}/execute")
async def execute_proposal(
    session: SessionDep, current: ProposalActorDep, scope: ExecuteScopeDep, proposal_id: str
) -> Any:
    """执行提案：审批/快照/有效期复核后经 Outbox 入队（不直接调用基础设施）。"""
    proposal, execution = await proposals_service.execute_proposal(
        session,
        get_session_factory(),
        proposal_id=proposal_id,
        actor_id=current.user.id,
        scope=scope,
    )
    await session.commit()
    await session.refresh(proposal)
    return ok(
        {
            "proposal": _proposal_view(proposal),
            "execution": ExecutionView.model_validate(execution).model_dump(mode="json"),
        }
    )

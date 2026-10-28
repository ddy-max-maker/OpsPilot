"""审批与受控执行服务（D5，CONTRACTS §2.11–§2.14 / PRD §8）。

红线（服务端强制，不信任任何调用方）：
- production 直接拒绝写操作（应用层 403 + 数据库 CHECK 兜底）；
- 只能引用已发布、未退役的 Runbook 版本；r2/r3 不进入执行链路；
- 四眼原则：decided_by ≠ created_by_user 且审批人必须具备 approver/admin 角色；
- 执行前复核：审批有效、参数哈希一致、目标资产/环境/Runbook 版本与审批快照一致；
- 执行入队 = Outbox 事务写（业务变更与事件同一 PostgreSQL 事务）；
- 动作必须命中代码级 Allowlist（模型字符串不能直接作为执行命令）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.enums import ActorType, Environment, IncidentStatus, OutboxStatus, PolicyPermission
from app.core.errors import conflict_state, forbidden, not_found, validation_error
from app.core.ids import new_id
from app.domains.access.scope import AssetScope
from app.domains.approvals.allowlist import (
    ACTION_ALLOWLIST,
    get_action,
    is_registered_container_name,
    resolve_container_name,
    validate_parameters,
)
from app.domains.approvals.schemas import CheckView
from app.domains.approvals.state import require_proposal_transition
from app.domains.audit import service as audit_service
from app.domains.incidents.state_machine import (
    require_transition as require_incident_transition,
)
from app.models import (
    ActionExecution,
    ActionExecutionStatus,
    ActionIdempotency,
    Approval,
    Asset,
    IdempotencyState,
    Incident,
    InvestigationRun,
    OutboxEvent,
    Proposal,
    ProposalStatus,
    Role,
    RunbookStatus,
    RunbookVersion,
    User,
    UserRole,
    VerificationResult,
    VerificationStatus,
)

# ---- 辅助 ----


def canonical_json(data: object) -> str:
    """规范化 JSON（参数哈希与快照比对的基础；跨进程稳定）。"""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_parameter_hash(runbook_version_id: str, parameters: dict[str, Any]) -> str:
    """parameter_hash = sha256(runbook_version_id + canonical(parameters))（PRD §8）。"""
    return hashlib.sha256(f"{runbook_version_id}|{canonical_json(parameters)}".encode()).hexdigest()


async def get_proposal_or_404(session: AsyncSession, proposal_id: str) -> Proposal:
    proposal = await session.get(Proposal, proposal_id)
    if proposal is None:
        raise not_found("proposal not found")
    return proposal


def _resolve_action_key(runbook_version: RunbookVersion, parameters: dict[str, Any]) -> str:
    """从 Runbook allowlist 解析唯一动作键（多键时必须由参数显式指定）。"""
    keys = [str(k) for k in runbook_version.allowlist_action_keys or []]
    if not keys:
        raise validation_error("runbook version has no allowlist action keys; it is not executable")
    requested = parameters.get("action_key")
    if len(keys) == 1 and requested is None:
        return keys[0]
    if not isinstance(requested, str) or requested not in keys:
        raise validation_error(
            "parameters.action_key must be one of the runbook allowlist keys",
            details={"allowlist": keys},
        )
    return requested


def _check(name: str, passed: bool, detail: str | None = None) -> CheckView:
    return CheckView(name=name, passed=passed, detail=detail)


async def _run_prechecks(
    session: AsyncSession,
    *,
    proposal: Proposal,
    settings: Settings,
) -> list[CheckView]:
    """前置检查（只读）：状态/注册表/绑定/参数/时效。"""
    checks: list[CheckView] = []
    rbv = await session.get(RunbookVersion, proposal.runbook_version_id)
    checks.append(
        _check(
            "runbook_version_published",
            rbv is not None and rbv.status == RunbookStatus.PUBLISHED,
            None if rbv is None else f"status={rbv.status}",
        )
    )
    # action_key 从 Runbook allowlist 现场解析（提案不冗余存储动作键）
    action_key: str | None = None
    if rbv is not None and rbv.status == RunbookStatus.PUBLISHED:
        try:
            action_key = _resolve_action_key(rbv, dict(proposal.parameters))
        except Exception:  # noqa: BLE001 — 解析失败作为未通过检查项呈现
            action_key = None
    asset = await session.get(Asset, proposal.target_asset_id)
    checks.append(
        _check(
            "asset_active",
            asset is not None and asset.status == "active",
            None if asset is None else f"status={asset.status}",
        )
    )
    checks.append(
        _check(
            "environment_match",
            asset is not None and asset.environment == proposal.environment,
            None if asset is None else f"asset_env={asset.environment}",
        )
    )
    spec = get_action(action_key or "")
    checks.append(
        _check(
            "action_registered",
            spec is not None and spec.risk_level == proposal.risk_level,
            None if spec is None else f"risk={spec.risk_level}",
        )
    )
    if spec is not None:
        errors = validate_parameters(spec, dict(proposal.parameters))
        checks.append(
            _check(
                "parameters_valid",
                not errors,
                "; ".join(errors) if errors else None,
            )
        )
        container = resolve_container_name(
            dict(proposal.parameters), dict(asset.endpoints) if asset and asset.endpoints else None
        )
        container_ok = (
            container is not None
            and is_registered_container_name(container)
            and container.startswith(settings.action_container_name_prefix)
        )
        checks.append(
            _check(
                "container_binding",
                container_ok,
                container if container_ok else "target container not registered/bound",
            )
        )
    checks.append(
        _check(
            "not_expired",
            proposal.expires_at > datetime.now(UTC),
            f"expires_at={proposal.expires_at.isoformat()}",
        )
    )
    return checks


# ---- 创建（策略校验 → pending_approval / 拒绝）----


async def create_proposal(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    actor_id: str,
    run_id: str,
    runbook_version_id: str,
    target_asset_id: str,
    environment: str,
    parameters: dict[str, Any],
    comment: str | None = None,
    settings: Settings | None = None,
) -> Proposal:
    """创建处置提案：策略校验全部通过 → pending_approval（否则显式拒绝，不入库）。"""
    settings = settings or get_settings()

    # production：直接拒绝写操作（即使 Admin 提交）
    if environment == Environment.PRODUCTION.value:
        raise forbidden(
            "production write operations are denied by policy",
            details={"environment": environment},
        )
    if environment not in (Environment.SANDBOX.value, Environment.STAGING.value):
        raise validation_error(
            "environment must be sandbox or staging", details={"environment": environment}
        )

    run = await session.get(InvestigationRun, run_id)
    if run is None:
        raise not_found("investigation run not found")
    incident = await session.get(Incident, run.incident_id)
    if incident is None:
        raise not_found("incident not found")

    rbv = await session.get(RunbookVersion, runbook_version_id)
    if rbv is None:
        raise not_found("runbook version not found")
    # 只能引用已发布、未退役的 Runbook 版本（草稿不可当作已批准执行方案）
    if rbv.status != RunbookStatus.PUBLISHED:
        raise conflict_state(
            "runbook version must be published (drafts are not executable guidance)",
            details={"status": rbv.status},
        )
    # r2/r3 不进入执行链路（PRD §8）
    if rbv.risk_level not in ("r0", "r1"):
        raise forbidden(
            "risk level r2/r3 is outside the executable scope of this release",
            details={"risk_level": rbv.risk_level},
        )

    asset = await session.get(Asset, target_asset_id)
    if asset is None:
        raise not_found("target asset not found")
    if asset.environment != environment:
        raise conflict_state(
            "target asset does not belong to the proposal environment",
            details={"asset_env": asset.environment, "proposal_env": environment},
        )

    action_key = _resolve_action_key(rbv, parameters)
    spec = ACTION_ALLOWLIST.get(action_key)
    if spec is None:
        raise forbidden(
            "action is not registered in the code-level allowlist",
            details={"action_key": action_key},
        )
    if spec.risk_level != rbv.risk_level:
        raise conflict_state(
            "runbook risk level does not match the registered action spec",
            details={"runbook_risk": rbv.risk_level, "action_risk": spec.risk_level},
        )
    errors = validate_parameters(spec, parameters)
    if errors:
        raise validation_error(
            "parameters rejected by the action schema", details={"errors": errors}
        )
    # 基础设施兜底之外的应用层约束：容器名必须在演示命名空间且与资产登记一致
    asset_endpoints = dict(asset.endpoints) if asset.endpoints else None
    container = resolve_container_name(parameters, asset_endpoints)
    if (
        container is None
        or not is_registered_container_name(container)
        or not container.startswith(settings.action_container_name_prefix)
    ):
        raise forbidden(
            "target container is not within the registered demo namespace",
            details={"container": container},
        )

    now = datetime.now(UTC)
    proposal = Proposal(
        id=new_id("prp_"),
        run_id=run.id,
        incident_id=incident.id,
        runbook_version_id=rbv.id,
        target_asset_id=asset.id,
        environment=environment,
        parameters=parameters,
        parameter_hash=compute_parameter_hash(rbv.id, parameters),
        risk_level=rbv.risk_level,
        status=ProposalStatus.PENDING_APPROVAL,
        created_by=f"agent:{settings.agent_provider}:{settings.agent_model_name}",
        created_by_user=actor_id,
        expires_at=now + timedelta(seconds=settings.proposal_ttl_seconds),
    )
    session.add(proposal)
    await audit_service.record(
        session,
        actor_type=ActorType.USER.value,
        actor_id=actor_id,
        action="proposal.created",
        resource_type="proposal",
        resource_id=proposal.id,
        environment=environment,
        detail={
            "run_id": run.id,
            "incident_id": incident.id,
            "runbook_version_id": rbv.id,
            "action_key": action_key,
            "risk_level": rbv.risk_level,
            "parameter_hash": proposal.parameter_hash,
            "comment": comment,
        },
    )
    await session.flush()
    return proposal


# ---- dry-run（只读前置检查）----


async def dry_run_proposal(
    session: AsyncSession,
    *,
    proposal_id: str,
    actor_id: str,
    settings: Settings | None = None,
) -> tuple[Proposal, list[CheckView]]:
    """只读前置检查：不发起任何外部动作；快照写入 preconditions_result。"""
    settings = settings or get_settings()
    proposal = await get_proposal_or_404(session, proposal_id)
    checks = await _run_prechecks(session, proposal=proposal, settings=settings)
    proposal.preconditions_result = {
        "checked_at": datetime.now(UTC).isoformat(),
        "passed": all(c.passed for c in checks),
        "checks": [c.model_dump() for c in checks],
    }
    await audit_service.record(
        session,
        actor_type=ActorType.USER.value,
        actor_id=actor_id,
        action="proposal.dry_run",
        resource_type="proposal",
        resource_id=proposal.id,
        environment=proposal.environment,
        detail={"passed": all(c.passed for c in checks)},
    )
    return proposal, checks


# ---- 审批（四眼；只追加）----


async def _require_approver_role(
    session: AsyncSession, *, approver_id: str, role_codes: list[str]
) -> None:
    """审批角色校验（服务层纵深防御：不依赖 API 层单一拦截）。"""
    if "approver" in role_codes or "admin" in role_codes:
        return
    raise forbidden("approver role required for this decision")


async def decide_proposal(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    proposal_id: str,
    approver_id: str,
    approver_role_codes: list[str],
    decision: str,
    comment: str | None = None,
    settings: Settings | None = None,
) -> tuple[Proposal, Approval]:
    """审批决定：approve（四眼 + 快照）或 reject；审批记录只追加。"""
    settings = settings or get_settings()
    await _require_approver_role(session, approver_id=approver_id, role_codes=approver_role_codes)
    if decision not in ("approved", "rejected"):
        raise validation_error("decision must be approved or rejected")
    proposal = await get_proposal_or_404(session, proposal_id)
    require_proposal_transition(proposal.status, ProposalStatus.APPROVED)
    approver = await session.get(User, approver_id)
    if approver is None or not approver.is_active:
        raise not_found("approver not found")
    # 四眼原则：审批人不得等于发起人
    if proposal.created_by_user == approver_id:
        raise forbidden("four-eyes violation: the approver must differ from the proposal creator")

    if proposal.expires_at <= datetime.now(UTC):
        require_proposal_transition(proposal.status, ProposalStatus.EXPIRED)
        proposal.status = ProposalStatus.EXPIRED
        raise conflict_state("proposal has expired", details={"proposal_id": proposal.id})

    now = datetime.now(UTC)
    snapshot = {
        "proposal_id": proposal.id,
        "runbook_version_id": proposal.runbook_version_id,
        "target_asset_id": proposal.target_asset_id,
        "environment": proposal.environment,
        "parameters": proposal.parameters,
        "parameter_hash": proposal.parameter_hash,
        "risk_level": proposal.risk_level,
    }
    approval = Approval(
        id=new_id("apv_"),
        proposal_id=proposal.id,
        decision=decision,
        decided_by=approver_id,
        proposal_snapshot=snapshot,
        expires_at=now + timedelta(seconds=settings.approval_execution_window_seconds),
        comment=comment,
        decided_at=now,
    )
    session.add(approval)
    if decision == "approved":
        proposal.status = ProposalStatus.APPROVED
    else:
        proposal.status = ProposalStatus.REJECTED
    await audit_service.record(
        session,
        actor_type=ActorType.USER.value,
        actor_id=approver_id,
        action=f"proposal.{decision}",
        resource_type="proposal",
        resource_id=proposal.id,
        environment=proposal.environment,
        detail={
            "approval_id": approval.id,
            "parameter_hash": proposal.parameter_hash,
            "comment": comment,
        },
    )
    await session.flush()
    return proposal, approval


# ---- 执行入队（审批复核 → 幂等账本 → Outbox 同事务）----


async def execute_proposal(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    proposal_id: str,
    actor_id: str,
    scope: AssetScope,
    settings: Settings | None = None,
) -> tuple[Proposal, ActionExecution]:
    """执行提案：审批复核全部通过后写入 Outbox（与业务变更同事务）。

    复核项（CONTRACTS §2.13 / 用户指令 §2）：
    - proposal 状态 = approved；
    - 审批存在且 approved、未过期；
    - 四眼再复核（decided_by ≠ created_by_user，防数据库手改）；
    - 参数哈希、目标资产、环境、Runbook 版本与审批快照一致（防篡改）；
    - Runbook 版本仍 published（未退役）；
    - 目标资产在当前用户执行权限范围内（execute 维度）。
    """
    settings = settings or get_settings()
    proposal = await get_proposal_or_404(session, proposal_id)
    require_proposal_transition(proposal.status, ProposalStatus.EXECUTING)

    # 执行权限：目标资产必须处于当前用户的 execute 范围（Admin 天然全量）
    if not (scope.is_admin or scope.all_assets) and (
        proposal.target_asset_id not in scope.asset_ids
        and proposal.environment not in scope.environments
    ):
        raise forbidden(
            "target asset is outside your execute scope",
            details={"asset_id": proposal.target_asset_id},
        )

    approval = (
        (
            await session.execute(
                select(Approval)
                .where(
                    Approval.proposal_id == proposal.id,
                    Approval.decision == "approved",
                )
                .order_by(Approval.decided_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if approval is None:
        raise conflict_state("no approved approval record for this proposal")
    if approval.expires_at <= datetime.now(UTC):
        require_proposal_transition(proposal.status, ProposalStatus.EXPIRED)
        proposal.status = ProposalStatus.EXPIRED
        raise conflict_state("approval has expired; execute is denied")
    if approval.decided_by == proposal.created_by_user:
        raise forbidden("four-eyes violation detected at execute time; execute is denied")
    snap = approval.proposal_snapshot
    if (
        snap.get("parameter_hash") != proposal.parameter_hash
        or snap.get("runbook_version_id") != proposal.runbook_version_id
        or snap.get("target_asset_id") != proposal.target_asset_id
        or snap.get("environment") != proposal.environment
        or canonical_json(snap.get("parameters")) != canonical_json(proposal.parameters)
    ):
        raise forbidden("proposal state diverged from the approval snapshot; execute is denied")

    rbv = await session.get(RunbookVersion, proposal.runbook_version_id)
    if rbv is None or rbv.status != RunbookStatus.PUBLISHED:
        raise conflict_state("runbook version is no longer published; execute is denied")

    action_key = _resolve_action_key(rbv, dict(proposal.parameters))
    spec = ACTION_ALLOWLIST.get(action_key)
    if spec is None:
        raise forbidden("action is no longer registered in the allowlist")

    idempotency_key = hashlib.sha256(
        f"{proposal.id}|{proposal.parameter_hash}".encode()
    ).hexdigest()

    # 幂等抢占：同 proposal+hash 只允许一条执行链（重复请求不再发起外部操作）
    existing_execution = (
        await session.execute(
            select(ActionExecution).where(
                ActionExecution.proposal_id == proposal.id,
                ActionExecution.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing_execution is not None:
        raise conflict_state(
            "proposal already has an execution for the same parameter hash",
            details={"execution_id": existing_execution.id},
        )

    now = datetime.now(UTC)
    execution = ActionExecution(
        id=new_id("act_"),
        proposal_id=proposal.id,
        action_key=action_key,
        idempotency_key=idempotency_key,
        status=ActionExecutionStatus.PENDING,
        created_at=now,
    )
    session.add(execution)

    # 幂等账本由 ops-action 消费侧以 INSERT…ON CONFLICT 原子抢占；
    # 此处已有同 proposal+hash 唯一执行约束（上方重复执行检查）兜底。

    proposal.status = ProposalStatus.EXECUTING
    # incident 进入缓解中（失败时由调用方决定回退；非 investigating 状态不阻塞）
    incident = await session.get(Incident, proposal.incident_id)
    incident_transition_note: str | None = None
    if incident is not None and incident.status == IncidentStatus.INVESTIGATING.value:
        require_incident_transition(IncidentStatus.INVESTIGATING, IncidentStatus.MITIGATING)
        incident.status = IncidentStatus.MITIGATING.value
    elif incident is not None:
        incident_transition_note = f"incident status={incident.status}; transition skipped"

    # Outbox 事件与业务变更同一事务（绝不 commit 后直接 publish）
    outbox = OutboxEvent(
        aggregate_type="proposal",
        aggregate_id=proposal.id,
        event_type="action.execute_requested",
        payload={
            "proposal_id": proposal.id,
            "execution_id": execution.id,
            "incident_id": proposal.incident_id,
            "action_key": action_key,
            "idempotency_key": idempotency_key,
            "runbook_version_id": proposal.runbook_version_id,
            "target_asset_id": proposal.target_asset_id,
            "environment": proposal.environment,
            "parameters": proposal.parameters,
            "parameter_hash": proposal.parameter_hash,
            "risk_level": proposal.risk_level,
            "requested_by": actor_id,
            "requested_at": now.isoformat(),
        },
        status=OutboxStatus.PENDING.value,
    )
    session.add(outbox)
    await audit_service.record(
        session,
        actor_type=ActorType.USER.value,
        actor_id=actor_id,
        action="proposal.execute_requested",
        resource_type="proposal",
        resource_id=proposal.id,
        environment=proposal.environment,
        detail={
            "execution_id": execution.id,
            "action_key": action_key,
            "idempotency_key": idempotency_key,
            "incident_transition": incident_transition_note,
        },
    )
    await session.flush()
    return proposal, execution


async def claim_idempotency_in_transaction(
    session: AsyncSession, *, execution: ActionExecution, executor: str
) -> bool:
    """ops-action 消费侧：同事务原子抢占幂等账本 + execution → running。

    INSERT … ON CONFLICT DO NOTHING 原子语义：返回 False 表示账本已被
    抢占（重复消息/并发实例）——消费方必须 ack 并跳过外部动作。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: PLC0415

    result = await session.execute(
        pg_insert(ActionIdempotency)
        .values(
            idempotency_key=execution.idempotency_key,
            action_execution_id=execution.id,
            state=IdempotencyState.CLAIMED,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    if getattr(result, "rowcount", 0) == 0:
        return False
    execution.status = ActionExecutionStatus.RUNNING
    execution.executor = executor
    execution.started_at = datetime.now(UTC)
    return True


# ---- 执行结果回写（ops-action 专用；统一状态机推进 + incident 链路）----


async def _load_execution(
    session: AsyncSession, execution_id: str
) -> tuple[ActionExecution, Proposal]:
    execution = await session.get(ActionExecution, execution_id)
    if execution is None:
        raise not_found("action execution not found")
    proposal = await session.get(Proposal, execution.proposal_id)
    if proposal is None:
        raise not_found("proposal not found")
    return execution, proposal


async def _settle_ledger(
    session: AsyncSession, execution: ActionExecution, state: str, evidence: dict[str, Any] | None
) -> None:
    """幂等账本回写（claimed → completed/unknown；调用方事务内执行）。"""
    await session.execute(
        update(ActionIdempotency)
        .where(ActionIdempotency.idempotency_key == execution.idempotency_key)
        .values(
            state=state,
            external_evidence=evidence,
            completed_at=datetime.now(UTC),
        )
    )


async def complete_execution(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    execution_id: str,
    succeeded: bool,
    result_detail: dict[str, Any] | None = None,
    external_ref: str | None = None,
    executor: str | None = None,
) -> tuple[ActionExecution, Proposal]:
    """外部动作结果回写：succeeded → executed；failed → execution_failed。"""
    execution, proposal = await _load_execution(session, execution_id)
    now = datetime.now(UTC)
    execution.finished_at = now
    execution.result_detail = result_detail
    if external_ref:
        execution.external_ref = external_ref
    if executor:
        execution.executor = executor
    if succeeded:
        execution.status = ActionExecutionStatus.SUCCEEDED
        await _settle_ledger(
            session, execution, IdempotencyState.COMPLETED, {"external_ref": external_ref}
        )
        require_proposal_transition(proposal.status, ProposalStatus.EXECUTED)
        proposal.status = ProposalStatus.EXECUTED
    else:
        execution.status = ActionExecutionStatus.FAILED
        await _settle_ledger(session, execution, IdempotencyState.COMPLETED, {"failed": True})
        require_proposal_transition(proposal.status, ProposalStatus.EXECUTION_FAILED)
        proposal.status = ProposalStatus.EXECUTION_FAILED
        # 恢复失败：incident 回调查路径（mitigating → monitoring → investigating）
        await _return_incident_to_investigating(session, proposal)
    await audit_service.record(
        session,
        actor_type=ActorType.ACTION_SERVICE.value,
        actor_id=executor or "ops-action",
        action=f"execution.{execution.status}",
        resource_type="action_execution",
        resource_id=execution.id,
        environment=proposal.environment,
        detail={"proposal_id": proposal.id, "external_ref": external_ref},
    )
    await session.flush()
    return execution, proposal


async def mark_execution_unknown(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    execution_id: str,
    result_detail: dict[str, Any] | None = None,
    external_ref: str | None = None,
    executor: str | None = None,
) -> tuple[ActionExecution, Proposal]:
    """外部效果未知：UNKNOWN + 账本 unknown；禁止盲目重试，转人工。"""
    execution, proposal = await _load_execution(session, execution_id)
    now = datetime.now(UTC)
    execution.finished_at = now
    execution.result_detail = result_detail
    if external_ref:
        execution.external_ref = external_ref
    if executor:
        execution.executor = executor
    execution.status = ActionExecutionStatus.UNKNOWN
    await _settle_ledger(
        session, execution, IdempotencyState.UNKNOWN, {"external_ref": external_ref}
    )
    require_proposal_transition(proposal.status, ProposalStatus.EXECUTION_UNKNOWN)
    proposal.status = ProposalStatus.EXECUTION_UNKNOWN
    incident = await session.get(Incident, proposal.incident_id)
    if incident is not None and incident.status == IncidentStatus.MITIGATING.value:
        require_incident_transition(IncidentStatus.MITIGATING, IncidentStatus.NEEDS_HUMAN)
        incident.status = IncidentStatus.NEEDS_HUMAN.value
    await audit_service.record(
        session,
        actor_type=ActorType.ACTION_SERVICE.value,
        actor_id=executor or "ops-action",
        action="execution.unknown",
        resource_type="action_execution",
        resource_id=execution.id,
        environment=proposal.environment,
        detail={"proposal_id": proposal.id, "reason": "external outcome unverifiable"},
    )
    await session.flush()
    return execution, proposal


async def _return_incident_to_investigating(session: AsyncSession, proposal: Proposal) -> None:
    incident = await session.get(Incident, proposal.incident_id)
    if incident is None:
        return
    if incident.status == IncidentStatus.MITIGATING.value:
        require_incident_transition(IncidentStatus.MITIGATING, IncidentStatus.MONITORING)
        incident.status = IncidentStatus.MONITORING.value
        require_incident_transition(IncidentStatus.MONITORING, IncidentStatus.INVESTIGATING)
        incident.status = IncidentStatus.INVESTIGATING.value


# ---- 恢复验证落库（verification service 调用）----


async def record_verification(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    execution_id: str,
    status: str,
    checks: list[dict[str, Any]],
    observation_window: dict[str, Any],
    executor: str | None = None,
) -> tuple[VerificationResult, Proposal]:
    """验证结果落库并推进状态机：passed 才允许 VERIFIED（incident → RESOLVED 路径）。"""
    execution, proposal = await _load_execution(session, execution_id)
    if execution.status != ActionExecutionStatus.SUCCEEDED:
        raise conflict_state(
            "verification requires a succeeded execution",
            details={"execution_status": execution.status},
        )
    require_proposal_transition(proposal.status, ProposalStatus.VERIFIED)
    if status == VerificationStatus.FAILED:
        require_proposal_transition(proposal.status, ProposalStatus.VERIFY_FAILED)
    elif status == VerificationStatus.UNKNOWN:
        require_proposal_transition(proposal.status, ProposalStatus.VERIFY_UNKNOWN)

    result = VerificationResult(
        id=new_id("vf_"),
        execution_id=execution.id,
        status=status,
        checks=checks,
        observation_window=observation_window,
        concluded_at=datetime.now(UTC) if status != VerificationStatus.IN_PROGRESS else None,
    )
    session.add(result)
    if status == VerificationStatus.PASSED:
        proposal.status = ProposalStatus.VERIFIED
        await _resolve_incident(session, proposal)
    elif status == VerificationStatus.FAILED:
        proposal.status = ProposalStatus.VERIFY_FAILED
        # S9 红线：动作成功但服务不健康 → 绝不 RESOLVED，incident 转回调查
        await _return_incident_to_investigating(session, proposal)
    else:
        proposal.status = ProposalStatus.VERIFY_UNKNOWN
        incident = await session.get(Incident, proposal.incident_id)
        if incident is not None and incident.status == IncidentStatus.MITIGATING.value:
            require_incident_transition(IncidentStatus.MITIGATING, IncidentStatus.NEEDS_HUMAN)
            incident.status = IncidentStatus.NEEDS_HUMAN.value
    await audit_service.record(
        session,
        actor_type=ActorType.ACTION_SERVICE.value,
        actor_id=executor or "ops-action",
        action=f"verification.{status}",
        resource_type="verification_result",
        resource_id=result.id,
        environment=proposal.environment,
        detail={"execution_id": execution.id, "proposal_id": proposal.id},
    )
    await session.flush()
    return result, proposal


async def _resolve_incident(session: AsyncSession, proposal: Proposal) -> None:
    """验证 PASSED：mitigating → monitoring → resolved（仅此路径可达 RESOLVED）。"""
    incident = await session.get(Incident, proposal.incident_id)
    if incident is None:
        return
    if incident.status == IncidentStatus.MITIGATING.value:
        require_incident_transition(IncidentStatus.MITIGATING, IncidentStatus.MONITORING)
        incident.status = IncidentStatus.MONITORING.value
        require_incident_transition(IncidentStatus.MONITORING, IncidentStatus.RESOLVED)
        incident.status = IncidentStatus.RESOLVED.value


# ---- 过期收敛 ----


async def expire_stale_proposals(session: AsyncSession) -> int:
    """PENDING_APPROVAL 超期 → expired；APPROVED 但审批窗口过期 → expired。"""
    now = datetime.now(UTC)
    stale_pending = (
        (
            await session.execute(
                select(Proposal).where(
                    Proposal.status == ProposalStatus.PENDING_APPROVAL,
                    Proposal.expires_at <= now,
                )
            )
        )
        .scalars()
        .all()
    )
    stale_approved = (
        (await session.execute(select(Proposal).where(Proposal.status == ProposalStatus.APPROVED)))
        .scalars()
        .all()
    )
    expired = 0
    for proposal in stale_pending:
        require_proposal_transition(proposal.status, ProposalStatus.EXPIRED)
        proposal.status = ProposalStatus.EXPIRED
        expired += 1
    for proposal in stale_approved:
        approval = (
            (
                await session.execute(
                    select(Approval)
                    .where(
                        Approval.proposal_id == proposal.id,
                        Approval.decision == "approved",
                    )
                    .order_by(Approval.decided_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if approval is not None and approval.expires_at <= now:
            require_proposal_transition(proposal.status, ProposalStatus.EXPIRED)
            proposal.status = ProposalStatus.EXPIRED
            expired += 1
    return expired


# ---- 视图聚合 ----


async def load_proposal_detail(
    session: AsyncSession, proposal_id: str
) -> tuple[Proposal, list[Approval], list[ActionExecution], list[VerificationResult]]:
    proposal = await get_proposal_or_404(session, proposal_id)
    approvals = (
        (
            await session.execute(
                select(Approval)
                .where(Approval.proposal_id == proposal_id)
                .order_by(Approval.decided_at)
            )
        )
        .scalars()
        .all()
    )
    executions = (
        (
            await session.execute(
                select(ActionExecution)
                .where(ActionExecution.proposal_id == proposal_id)
                .order_by(ActionExecution.created_at)
            )
        )
        .scalars()
        .all()
    )
    verifications: list[VerificationResult] = []
    for execution in executions:
        rows = (
            (
                await session.execute(
                    select(VerificationResult)
                    .where(VerificationResult.execution_id == execution.id)
                    .order_by(VerificationResult.created_at)
                )
            )
            .scalars()
            .all()
        )
        verifications.extend(rows)
    return proposal, list(approvals), list(executions), verifications


async def user_role_codes(session: AsyncSession, user_id: str) -> list[str]:
    """用户角色 code 列表（审批服务层校验用；与 auth service 同语义）。"""
    rows = (
        (
            await session.execute(
                select(Role.code)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def policy_permission_hint() -> str:
    """执行链路使用的策略权限维度（execute）。"""
    return PolicyPermission.EXECUTE.value

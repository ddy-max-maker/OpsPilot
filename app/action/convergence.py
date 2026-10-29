"""崩溃收敛（D5 → D6 加固）：幂等账本 claimed 超时 → 状态收敛 + 验证恢复。

崩溃场景对账（A–D）：
- A. 数据库记录前崩溃：execute 事务未提交 → 无 execution/无消息 → 无外部效果；
- B. 已认领但外部动作尚未发出时崩溃：账本 claimed、execution=running，
  external_ref 为空 → 收敛为 unknown（无法证明动作未发出——保守处理）；
- C. 外部动作成功但数据库尚未记录成功时崩溃：有 external_ref 且动作声明
  支持状态查询（idempotent=true）→ 查询收敛；否则 → unknown；
- D. 外部动作失败或响应丢失：执行器已归类 unknown → 同 C 收敛路径。

D6 新增（E 场景）：外部动作成功落库（execution=succeeded、proposal=executed）
但验证缺失/进行中时崩溃 → 重启后重新执行**只读验证**（不重发副作用动作），
验证记录幂等（同一 execution 至多一条）。

默认策略：外部效果未知时标记 UNKNOWN，禁止盲目重试（转人工）。
数据库唯一键 ≠ 外部操作 exactly-once——账本只承诺零重复提交。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.action import verification as verification_mod
from app.core.config import Settings, get_settings
from app.core.enums import ActorType
from app.domains.approvals import service as approvals_service
from app.domains.approvals.allowlist import get_action
from app.domains.audit import service as audit_service
from app.models import (
    ActionExecution,
    ActionExecutionStatus,
    ActionIdempotency,
    Asset,
    IdempotencyState,
    Proposal,
    ProposalStatus,
)

logger = logging.getLogger("ops-action.convergence")


async def _converge_one(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ledger: ActionIdempotency,
    execution: ActionExecution,
    executor_id: str,
) -> str:
    """单条超时账本收敛（返回收敛动作说明）。"""
    spec = get_action(execution.action_key)
    external_evidence = ledger.external_evidence or {}
    external_ref = execution.external_ref or (
        str(external_evidence.get("external_ref"))
        if external_evidence.get("external_ref")
        else None
    )
    # 场景 C（收敛查询）：仅当动作声明外部接口支持幂等执行与状态查询时，
    # 按协议查询并收敛；首版 allowlist 无此类动作（docker restart 无查询接口）。
    if spec is not None and spec.idempotent and external_ref:
        return (
            "deferred: action declares idempotent external interface; "
            "status query convergence is protocol-specific (not implemented for this action)"
        )
    async with session_factory() as session2:
        await approvals_service.mark_execution_unknown(
            session2,
            session_factory,
            execution_id=execution.id,
            result_detail={
                "reason": "crash convergence: claimed lease expired without a settled outcome",
                "external_ref": external_ref,
                "claimed_at": ledger.claimed_at.isoformat() if ledger.claimed_at else None,
            },
            external_ref=external_ref,
            executor=executor_id,
        )
        await audit_service.record(
            session2,
            actor_type=ActorType.ACTION_SERVICE.value,
            actor_id=executor_id,
            action="execution.converged_unknown",
            resource_type="action_execution",
            resource_id=execution.id,
            detail={
                "reason": "claimed lease expired; outcome unknown; manual retry forbidden",
            },
        )
        await session2.commit()
    return "marked unknown"


async def converge_stale_claims(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings | None = None,
    executor_id: str = "ops-action",
) -> list[dict[str, str]]:
    """扫描 claimed 超时的幂等账本并收敛（启动 + 周期调用）。"""
    settings = settings or get_settings()
    deadline = datetime.now(UTC) - timedelta(seconds=settings.idempotency_claim_timeout_seconds)
    stale = (
        await session.execute(
            select(ActionIdempotency, ActionExecution)
            .join(
                ActionExecution,
                ActionExecution.id == ActionIdempotency.action_execution_id,
            )
            .where(
                ActionIdempotency.state == IdempotencyState.CLAIMED,
                ActionIdempotency.claimed_at <= deadline,
            )
        )
    ).all()
    results: list[dict[str, str]] = []
    for ledger, execution in stale:
        logger.warning(
            "converging stale idempotency claim for execution %s (claimed_at=%s)",
            execution.id,
            ledger.claimed_at,
        )
        action = await _converge_one(
            session,
            session_factory,
            ledger=ledger,
            execution=execution,
            executor_id=executor_id,
        )
        results.append({"execution_id": execution.id, "action": action})
    return results


async def resume_pending_verifications(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings | None = None,
    executor_id: str = "ops-action",
) -> list[dict[str, str]]:
    """场景 E 收敛（D6）：动作成功落库但验证缺失/进行中 → 重新执行只读验证。

    - 仅针对 execution=succeeded 且 proposal=executed 的记录；
    - 绝不重发有副作用动作（验证探测全部只读）；
    - 验证记录幂等由 verify_execution 的结论检查 + UNIQUE 约束兜底。
    """
    settings = settings or get_settings()
    rows = (
        (
            await session.execute(
                select(ActionExecution, Proposal)
                .join(Proposal, Proposal.id == ActionExecution.proposal_id)
                .where(
                    ActionExecution.status == ActionExecutionStatus.SUCCEEDED,
                    Proposal.status == ProposalStatus.EXECUTED,
                )
            )
        )
        .all()
    )
    results: list[dict[str, str]] = []
    for execution, proposal in rows:
        # 只读验证重入（幂等：已有结论时 verify_execution 内部跳过）
        context = await _build_verification_context(session, execution, proposal)
        status = await verification_mod.verify_execution(
            session_factory,
            execution_id=execution.id,
            proposal=proposal,
            context=context,
            executor_id=executor_id,
            settings=settings,
        )
        results.append({"execution_id": execution.id, "action": f"verification resumed: {status}"})
        logger.warning(
            "verification resumed for execution %s -> %s (read-only; no side effects)",
            execution.id,
            status,
        )
    return results


async def _build_verification_context(
    session: AsyncSession, execution: ActionExecution, proposal: Proposal
) -> dict[str, object]:
    """从 DB 导出验证上下文（只读；与消费侧导出同源）。"""
    asset = await session.get(Asset, proposal.target_asset_id)
    endpoints = dict(asset.endpoints) if asset is not None and asset.endpoints else {}
    return {
        "proposal_id": proposal.id,
        "environment": proposal.environment,
        "target_asset_id": proposal.target_asset_id,
        "container_name": endpoints.get("container_name"),
        "requested_at": execution.finished_at.isoformat() if execution.finished_at else None,
    }

"""RabbitMQ 消费者（D5）：执行请求处理（手动 ack + 幂等 + 执行前复核）。

单条消息处理流程（CONTRACTS §2.13 / 用户指令 §2、§4、§7）：
1. 解析 payload；非本进程职责的消息直接 ack（拓扑兼容）；
2. 重复消息防线：execution 状态不是 pending → ack 跳过（不重复执行外部动作）；
3. 原子抢占幂等账本（INSERT … ON CONFLICT）→ 失败即重复消息 → ack 跳过；
4. 执行前复核（HITL 衔接，不从 checkpoint 绕过审批）：
   proposal 状态、审批快照、参数哈希、有效期、Runbook 版本；
   任何一项不一致 → execution=skipped（不改 proposal 状态）→ ack；
5. 前置检查（容器身份/状态）→ 执行 allowlist 动作；
6. 结果归类落库：succeeded → executed；failed → execution_failed；
   不可观测 → execution_unknown（禁止盲目重试）；
7. succeeded → 立即进入恢复验证（真实健康检查 + 观察窗口）。

异常路径：处理异常 → nack(requeue=False) → DLQ；claimed 账本由收敛扫描兜底。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import aio_pika
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.action import outbox as outbox_mod
from app.action.executor import ActionExecutor, OutcomeStatus
from app.action.verification import verify_execution
from app.core.config import Settings, get_settings
from app.core.enums import ActorType
from app.domains.approvals import service as approvals_service
from app.domains.audit import service as audit_service
from app.models import (
    ActionExecution,
    ActionExecutionStatus,
    Approval,
    Proposal,
    ProposalStatus,
    RunbookStatus,
    RunbookVersion,
)

logger = logging.getLogger("ops-action.consumer")


def _parse_payload(body: bytes) -> dict[str, Any]:
    import json

    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON object")
    return data


async def _recheck_authorization(
    session: AsyncSession, *, payload: dict[str, Any], settings: Settings
) -> str | None:
    """执行前复核（返回 None = 通过；否则返回拒绝原因）。

    复核项：proposal 状态、审批快照、参数哈希、有效期、Runbook 版本仍发布。
    该复核独立于 HTTP 请求与 checkpoint——直接以数据库权威状态为准。
    """
    proposal = await session.get(Proposal, str(payload.get("proposal_id")))
    if proposal is None:
        return "proposal not found"
    if proposal.status != ProposalStatus.EXECUTING:
        return f"proposal status is {proposal.status}, not executing"
    if proposal.expires_at <= datetime.now(UTC):
        return "proposal expired"
    if str(payload.get("parameter_hash")) != proposal.parameter_hash:
        return "parameter hash mismatch"
    if str(payload.get("runbook_version_id")) != proposal.runbook_version_id:
        return "runbook version mismatch"
    if str(payload.get("target_asset_id")) != proposal.target_asset_id:
        return "target asset mismatch"
    if str(payload.get("environment")) != proposal.environment:
        return "environment mismatch"
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
        return "no approved approval record"
    if approval.decided_by == proposal.created_by_user:
        return "four-eyes violation detected at consume time"
    snap = approval.proposal_snapshot
    if str(snap.get("parameter_hash")) != proposal.parameter_hash:
        return "approval snapshot parameter hash mismatch"
    if approval.expires_at <= datetime.now(UTC):
        return "approval window expired"
    rbv = await session.get(RunbookVersion, proposal.runbook_version_id)
    if rbv is None or rbv.status != RunbookStatus.PUBLISHED:
        return "runbook version no longer published"
    return None


async def handle_execute_requested(
    session_factory: async_sessionmaker[AsyncSession],
    executor: ActionExecutor,
    payload: dict[str, Any],
    *,
    executor_id: str,
    settings: Settings | None = None,
) -> None:
    """处理一条执行请求（幂等；外部动作至多发起一次）。"""
    settings = settings or get_settings()
    execution_id = str(payload.get("execution_id"))
    proposal_id = str(payload.get("proposal_id"))

    # ---- 阶段 1：抢占（独立事务）----
    async with session_factory() as session:
        execution = await session.get(ActionExecution, execution_id)
        if execution is None:
            logger.warning("execution %s not found; dropping message", execution_id)
            return
        if execution.status != ActionExecutionStatus.PENDING:
            # 重复消息/重投：已进入后续状态 → 不再发起外部动作
            logger.info(
                "execution %s already in status=%s; skip (duplicate message)",
                execution_id,
                execution.status,
            )
            return
        claimed = await approvals_service.claim_idempotency_in_transaction(
            session, execution=execution, executor=executor_id
        )
        if not claimed:
            logger.info("idempotency ledger already claimed for %s; skip (duplicate)", execution_id)
            return
        await session.commit()

    try:
        # ---- 阶段 2：执行前复核（独立只读事务）----
        async with session_factory() as session:
            rejection = await _recheck_authorization(session, payload=payload, settings=settings)
        if rejection is not None:
            async with session_factory() as session:
                execution = await session.get(ActionExecution, execution_id)
                assert execution is not None
                execution.status = ActionExecutionStatus.SKIPPED
                execution.finished_at = datetime.now(UTC)
                execution.result_detail = {"reason": f"pre-execute recheck rejected: {rejection}"}
                await audit_service.record(
                    session,
                    actor_type=ActorType.ACTION_SERVICE.value,
                    actor_id=executor_id,
                    action="execution.skipped",
                    resource_type="action_execution",
                    resource_id=execution_id,
                    detail={"proposal_id": proposal_id, "reason": rejection},
                )
                await session.commit()
            logger.warning("execution %s skipped: %s", execution_id, rejection)
            return

        # ---- 阶段 3：执行外部动作（容器身份/状态前检查在执行器内）----
        parameters = dict(payload.get("parameters") or {})
        outcome = await executor.execute(
            action_key=str(payload.get("action_key")), parameters=parameters
        )

        # ---- 阶段 4：结果落库 ----
        async with session_factory() as session:
            if outcome.status == OutcomeStatus.SUCCEEDED:
                _, proposal = await approvals_service.complete_execution(
                    session,
                    session_factory,
                    execution_id=execution_id,
                    succeeded=True,
                    result_detail=outcome.result_detail,
                    external_ref=outcome.external_ref,
                    executor=executor_id,
                )
                await session.commit()
                # ---- 阶段 5：恢复验证（真实健康检查 + 观察窗口）----
                await verify_execution(
                    session_factory,
                    execution_id=execution_id,
                    proposal=proposal,
                    payload=payload,
                    executor_id=executor_id,
                    settings=settings,
                )
            elif outcome.status == OutcomeStatus.FAILED:
                async with session_factory() as session:
                    await approvals_service.complete_execution(
                        session,
                        session_factory,
                        execution_id=execution_id,
                        succeeded=False,
                        result_detail=outcome.result_detail,
                        external_ref=outcome.external_ref,
                        executor=executor_id,
                    )
                    await session.commit()
            elif outcome.status == OutcomeStatus.PRE_CHECK_FAILED:
                async with session_factory() as session:
                    await approvals_service.fail_execution_precheck(
                        session,
                        session_factory,
                        execution_id=execution_id,
                        reason=str(outcome.result_detail.get("reason", "pre-check failed")),
                        executor=executor_id,
                    )
                    await session.commit()
            else:  # UNKNOWN：外部效果不可观测 → 禁止盲目重试
                async with session_factory() as session:
                    await approvals_service.mark_execution_unknown(
                        session,
                        session_factory,
                        execution_id=execution_id,
                        result_detail=outcome.result_detail,
                        external_ref=outcome.external_ref,
                        executor=executor_id,
                    )
                    await session.commit()
    except Exception:
        # 处理异常：账本 claimed 遗留 → 收敛扫描兜底标记 unknown；
        # 消息进 DLQ 供人工排查（不 requeue 避免死循环）。
        logger.exception(
            "failed to process execution %s; message will be dead-lettered", execution_id
        )
        raise


async def on_message(
    message: aio_pika.abc.AbstractIncomingMessage,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    executor: ActionExecutor,
    executor_id: str,
    settings: Settings,
) -> None:
    """aio-pika 回调包装：手动 ack。"""
    async with message.process(
        reject_on_redelivered=False,
        requeue=False,
        ignore_processed=True,
    ):
        payload = _parse_payload(message.body)
        event_type = (message.headers or {}).get("event_type", "")
        if event_type and event_type != "action.execute_requested":
            logger.info("ignoring unrelated event type %s", event_type)
            return
        await handle_execute_requested(
            session_factory,
            executor,
            payload,
            executor_id=executor_id,
            settings=settings,
        )


async def run_consumer(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    rabbitmq_url: str,
    settings: Settings,
    executor: ActionExecutor | None = None,
    executor_id: str | None = None,
) -> None:
    """阻塞式消费主循环（robust 连接：断线自动重连，持久队列可恢复）。"""
    import asyncio

    executor_id = executor_id or settings.action_executor_id or "ops-action"
    executor = executor or ActionExecutor(
        container_name_prefix=settings.action_container_name_prefix
    )
    connection = await aio_pika.connect_robust(rabbitmq_url)
    try:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)  # 逐条处理，简化幂等/收敛语义
        _exchange, queue = await outbox_mod.declare_topology(channel)
        logger.info("ops-action consumer started (queue=%s)", queue.name)
        await queue.consume(
            lambda msg: on_message(
                msg,
                session_factory=session_factory,
                executor=executor,
                executor_id=executor_id,
                settings=settings,
            )
        )
        await asyncio.Event().wait()  # 消费直到进程退出
    finally:
        await connection.close()

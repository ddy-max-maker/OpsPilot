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
from app.action.docker_client import DockerClient
from app.action.executor import ActionExecutor, OutcomeStatus
from app.action.verification import verify_execution
from app.core.config import Settings, get_settings
from app.core.enums import ActorType
from app.domains.approvals import service as approvals_service
from app.domains.approvals.allowlist import (
    ACTION_ALLOWLIST,
    is_registered_container_name,
    resolve_container_name,
    validate_parameters,
)
from app.domains.approvals.service import _resolve_action_key
from app.domains.audit import service as audit_service
from app.models import (
    ActionExecution,
    ActionExecutionStatus,
    Approval,
    Asset,
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


async def _load_authorized_context(
    session: AsyncSession,
    *,
    execution_id: str,
    settings: Settings,
) -> tuple[str | None, dict[str, Any] | None]:
    """执行前复核（D6 加固）：以数据库为唯一权威来源导出执行上下文。

    RabbitMQ 消息仅作为"执行通知"，任何消息字段都不作为参数/目标/动作来源。
    返回 (拒绝原因, 执行上下文)；通过时上下文包含从 DB 导出的
    action_key / parameters / container_name / environment / asset_id。

    复核链（全链 DB 读取 + 规范化重算）：
    1. ActionExecution → Proposal（状态/有效期/环境/资产）；
    2. Approval（approved/未过期/四眼/快照哈希一致）；
    3. 规范化重算 parameter_hash（runbook_version_id + canonical(parameters)）
       与 proposal 及审批快照三方比对——任何篡改都会导致哈希不一致；
    4. RunbookVersion（published、r0/r1）→ allowlist 解析唯一 action_key，
       且必须与 execution.action_key 一致（防消息替换动作语义）；
    5. Asset（active、环境匹配）→ 容器身份必须以资产 endpoints 登记
       为权威（无登记容器身份的资产不能执行；参数容器名必须与登记一致）；
    6. 代码级 Allowlist 参数 schema 校验。
    """
    execution = await session.get(ActionExecution, execution_id)
    if execution is None:
        return "execution not found", None
    proposal = await session.get(Proposal, execution.proposal_id)
    if proposal is None:
        return "proposal not found", None
    if proposal.status != ProposalStatus.EXECUTING:
        return f"proposal status is {proposal.status}, not executing", None
    if proposal.expires_at <= datetime.now(UTC):
        return "proposal expired", None
    if proposal.environment not in ("sandbox", "staging"):
        return f"environment '{proposal.environment}' is not executable", None

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
        return "no approved approval record", None
    if approval.decided_by == proposal.created_by_user:
        return "four-eyes violation detected at consume time", None
    if approval.expires_at <= datetime.now(UTC):
        return "approval window expired", None

    rbv = await session.get(RunbookVersion, proposal.runbook_version_id)
    if rbv is None or rbv.status != RunbookStatus.PUBLISHED:
        return "runbook version no longer published", None
    if rbv.risk_level not in ("r0", "r1"):
        return f"risk level {rbv.risk_level} is outside the executable scope", None

    # 规范化重算哈希：数据库参数是唯一权威（防消息/DB 篡改路径）
    db_parameters = dict(proposal.parameters or {})
    recomputed = approvals_service.compute_parameter_hash(
        proposal.runbook_version_id, db_parameters
    )
    if recomputed != proposal.parameter_hash:
        return "parameter hash mismatch (proposal)", None
    snap = approval.proposal_snapshot
    if (
        str(snap.get("parameter_hash")) != proposal.parameter_hash
        or approvals_service.canonical_json(snap.get("parameters"))
        != approvals_service.canonical_json(db_parameters)
        or str(snap.get("runbook_version_id")) != proposal.runbook_version_id
        or str(snap.get("target_asset_id")) != proposal.target_asset_id
        or str(snap.get("environment")) != proposal.environment
    ):
        return "approval snapshot mismatch", None

    # action_key：从 Runbook allowlist + DB 参数解析（与 execution.action_key 比对）
    try:
        action_key = _resolve_action_key(rbv, db_parameters)
    except Exception:  # noqa: BLE001 — 解析失败即拒绝
        return "action key resolution failed", None
    if action_key != execution.action_key:
        return "action key mismatch with execution record", None
    spec = ACTION_ALLOWLIST.get(action_key)
    if spec is None:
        return "action is not registered in the code-level allowlist", None
    if spec.risk_level != rbv.risk_level:
        return "runbook risk level does not match the action spec", None
    param_errors = validate_parameters(spec, db_parameters)
    if param_errors:
        return f"parameters rejected: {'; '.join(param_errors)}", None

    asset = await session.get(Asset, proposal.target_asset_id)
    if asset is None or asset.status != "active":
        return "target asset not found or inactive", None
    if asset.environment != proposal.environment:
        return "target asset does not belong to the proposal environment", None

    # 容器身份：资产 endpoints 登记为权威（无登记 = 拒绝；参数必须与登记一致）
    asset_endpoints = dict(asset.endpoints) if asset.endpoints else {}
    container = resolve_container_name(db_parameters, asset_endpoints)
    if (
        container is None
        or not is_registered_container_name(container)
        or not container.startswith(settings.action_container_name_prefix)
    ):
        return "target container is not registered/bound on the asset", None

    return None, {
        "proposal_id": proposal.id,
        "incident_id": proposal.incident_id,
        "environment": proposal.environment,
        "target_asset_id": proposal.target_asset_id,
        "action_key": action_key,
        "parameters": db_parameters,
        "container_name": container,
        "risk_level": rbv.risk_level,
        "runbook_version_id": proposal.runbook_version_id,
        "requested_at": proposal.updated_at.isoformat() if proposal.updated_at else None,
    }


async def handle_execute_requested(
    session_factory: async_sessionmaker[AsyncSession],
    executor: ActionExecutor,
    payload: dict[str, Any],
    *,
    executor_id: str,
    settings: Settings | None = None,
) -> None:
    """处理一条执行请求（幂等；外部动作至多发起一次）。

    payload 仅用于定位 execution_id（执行通知）；全部执行输入从数据库导出。
    """
    settings = settings or get_settings()
    execution_id = str(payload.get("execution_id"))
    proposal_id = str(payload.get("proposal_id"))

    # ---- 阶段 1：抢占（独立事务）----
    async with session_factory() as session:
        execution = await session.get(ActionExecution, execution_id)
        if execution is None:
            logger.warning("execution %s not found; dropping message", execution_id)
            return
        if execution.proposal_id != proposal_id:
            # 伪造通知：消息声称的 proposal 与执行记录不符 → 直接丢弃
            logger.warning("message proposal_id mismatch for execution %s; dropping", execution_id)
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
        # ---- 阶段 2：执行前复核（独立只读事务；DB 为唯一权威）----
        async with session_factory() as session:
            rejection, context = await _load_authorized_context(
                session, execution_id=execution_id, settings=settings
            )
        if rejection is not None or context is None:
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

        # ---- 阶段 3：执行外部动作（参数/目标全部来自 DB 导出上下文）----
        outcome = await executor.execute(
            action_key=str(context["action_key"]),
            parameters=dict(context["parameters"]),
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
                    context=context,
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
    # D6 安全默认值：真实执行要求「显式开启」+「隔离环境确认」双条件，
    # 任一缺失即降级为拒绝执行（不是静默放行）。
    execution_enabled = bool(settings.action_execution_enabled) and bool(
        settings.action_isolated_daemon_ack
    )
    if settings.action_execution_enabled and not settings.action_isolated_daemon_ack:
        logger.error(
            "ACTION_EXECUTION_ENABLED=true but ACTION_ISOLATED_DAEMON_ACK is not set: "
            "real write actions stay DISABLED (host docker.sock is a high-privilege credential; "
            "acknowledge the isolated demo daemon explicitly)"
        )
    executor = executor or ActionExecutor(
        container_name_prefix=settings.action_container_name_prefix,
        execution_enabled=execution_enabled,
        docker=(
            DockerClient(socket_path=settings.action_docker_socket)
            if settings.action_docker_socket
            else None
        ),
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

"""ops-worker 主循环：认领 → 执行 → fencing 回写 → incident 终态裁决。

红线（测试固化）：本进程不注册任何写工具；审计写操作尝试 = 0。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.enums import (
    RUN_TERMINAL_STATUSES,
    ActorType,
    AgentProvider,
    IncidentStatus,
    RunEventType,
    RunStatus,
    RunStopReason,
)
from app.domains.agent.fake import FakeDiagnoserChatModel
from app.domains.agent.provider import ModelHandle, build_chat_model
from app.domains.agent.runtime import AgentRuntime, RunExecution, RunOutcome
from app.domains.audit import service as audit_service
from app.domains.incidents.state_machine import require_transition
from app.domains.tools.context import LeaseFence, LeaseLostError, RunEventWriter
from app.domains.tools.gateway import ToolGateway
from app.models import Asset, Incident, InvestigationRun
from app.worker import claim

logger = logging.getLogger("opspilot.worker")


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class AgentWorker:
    """单进程串行执行（D2：一次一个 run；D5 起可横向扩容多 Worker）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._worker_id = settings.worker_id or default_worker_id()
        self._engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._gateway = ToolGateway(self._session_factory, settings)
        self._runtime: AgentRuntime | None = None
        self._model_handle: Any | None = None
        self._stop = asyncio.Event()

    # ---- 生命周期 ----

    async def startup(self) -> None:
        """初始化 checkpoint（langgraph schema 自建表）与模型运行时。"""
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: PLC0415
        from psycopg_pool import AsyncConnectionPool  # noqa: PLC0415

        pool = AsyncConnectionPool(
            conninfo=self._settings.psycopg_database_url,
            max_size=4,
            open=False,
            # setup() 内含 CREATE INDEX CONCURRENTLY，必须运行在 autocommit 连接上
            kwargs={"autocommit": True},
        )
        await pool.open()
        checkpointer = AsyncPostgresSaver(cast("Any", pool))
        await checkpointer.setup()  # 首次建 checkpoint 表（ADR-5）
        self._pool = pool
        self._checkpointer = checkpointer

        if self._settings.agent_provider == AgentProvider.FAKE.value:
            model: Any = FakeDiagnoserChatModel(
                model_name=self._settings.agent_model_name,
                include_knowledge_steps=self._settings.rag_mode != "off",
            )
            handle = ModelHandle(model, provider="fake", model_name=self._settings.agent_model_name)
            logger.warning(
                "AGENT_PROVIDER=fake：使用确定性脚本模型（报告由真实工具结果合成）；"
                "真实模型验收需配置 AGENT_PROVIDER=deepseek|anthropic|google 与 API Key"
            )
        else:
            handle = build_chat_model(self._settings)
        self._model_handle = handle
        # D5 修复①：RAG 开启时后台预热 Embedding 模型（不阻塞 startup/心跳/取消）
        self._embedding_warmup_task: asyncio.Task | None = None
        if self._settings.rag_mode != "off":
            provider = self._gateway._get_embedding_provider()
            if not provider.ready:

                async def _warm() -> None:
                    try:
                        await asyncio.to_thread(provider.warm_up)
                        logger.info("embedding model warmed up (%s)", provider.model_version)
                    except Exception:  # noqa: BLE001 — 预热失败记录 load_error，检索侧显式降级
                        logger.exception(
                            "embedding warm-up failed; retrieval will degrade (KNOWLEDGE_WARMING)"
                        )

                self._embedding_warmup_task = asyncio.create_task(_warm())
        from app.domains.agent.runtime import RuntimeDeps  # noqa: PLC0415

        deps = RuntimeDeps(
            session_factory=self._session_factory,
            settings=self._settings,
            gateway=self._gateway,
            checkpointer=checkpointer,
            pool=pool,
            model_handle=handle,
            model=handle.model,
        )
        self._runtime = AgentRuntime(deps)
        await self._cleanup_expired_checkpoints()
        logger.info(
            "worker %s started (provider=%s model=%s)",
            self._worker_id,
            handle.provider,
            handle.model_name,
        )

    async def shutdown(self) -> None:
        self._stop.set()
        await self._engine.dispose()
        pool = getattr(self, "_pool", None)
        if pool is not None:
            await pool.close()

    async def run_forever(self) -> None:
        try:
            await self.startup()
        except Exception:  # noqa: BLE001 — 启动失败必须可见地终止（配置错误不能悬挂进程）
            logger.exception("worker startup failed; exiting")
            await self.shutdown()
            return
        try:
            while not self._stop.is_set():
                row = await claim.claim_next_run(
                    self._session_factory,
                    worker_id=self._worker_id,
                    lease_ttl_seconds=self._settings.worker_lease_ttl_seconds,
                )
                if row is None:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._stop.wait(),
                            timeout=self._settings.worker_poll_interval_seconds,
                        )
                    continue
                try:
                    await self._execute_claimed(row)
                except Exception:  # noqa: BLE001 — 单 run 失败不拖垮 Worker
                    logger.exception("run %s crashed", row["id"])
        finally:
            await self.shutdown()

    # ---- 单 run 执行 ----

    async def _execute_claimed(self, row: dict[str, Any]) -> None:
        run_id = str(row["id"])
        fence_token = int(row["fence_token"] or 0)
        run = InvestigationRun(**dict(row))
        async with self._session_factory() as session:
            incident = await session.get(Incident, run.incident_id)
            asset = await session.get(Asset, incident.primary_asset_id) if incident else None
        if incident is None or asset is None:
            logger.error("run %s missing incident/asset; marking failed", run_id)
            await claim.finish_run(
                self._session_factory,
                run_id=run_id,
                fence_token=fence_token,
                status=RunStatus.FAILED.value,
                stop_reason="error",
                usage={},
            )
            return

        assert self._runtime is not None
        # D2.1 fencing：fence 绑定所有业务写入通道（事件/证据/假设/报告）
        lease_fence = LeaseFence(
            session_factory=self._session_factory,
            run_id=run_id,
            fence_token=fence_token,
            worker_id=self._worker_id,
        )
        events = await RunEventWriter.create(self._session_factory, run_id, lease_fence=lease_fence)

        # D4 安全修复③：实际执行快照（Worker 认领时落库，可审计——
        # 不能只依赖 run 创建时的 model_config；fencing 校验防止旧 Worker 写入）
        handle = self._model_handle
        if handle is not None:
            await claim.record_execution_snapshot(
                self._session_factory,
                run_id=run_id,
                fence_token=fence_token,
                snapshot={
                    "provider": handle.provider,
                    "model": handle.model_name,
                    "prompt_version": self._settings.agent_prompt_version,
                    "rag_mode": self._settings.rag_mode,
                    "worker_id": self._worker_id,
                    "fence_token": fence_token,
                },
            )

        # 后台心跳：与模型调用解耦续租（长推理不再受 lease TTL 限制）；
        # 续租失败（fence 失效/终态）→ lease_lost 置位 → 步进循环尽快中止
        lease_lost = asyncio.Event()

        async def heartbeat_loop() -> None:
            interval = self._settings.worker_heartbeat_interval_seconds
            while True:
                await asyncio.sleep(interval)
                alive = await claim.heartbeat(
                    self._session_factory,
                    run_id=run_id,
                    fence_token=fence_token,
                    lease_ttl_seconds=self._settings.worker_lease_ttl_seconds,
                )
                if not alive:
                    logger.warning("run %s lease lost (fence=%s); aborting", run_id, fence_token)
                    lease_lost.set()
                    return

        hb_task = asyncio.create_task(heartbeat_loop())

        async def cancel_checker() -> bool:
            """租约健康（后台心跳判定）+ 取消标志合并检查。"""
            if lease_lost.is_set():
                return True
            async with self._session_factory() as session:
                current = await session.get(InvestigationRun, run_id)
                return bool(current is None or current.cancel_requested)

        execution = RunExecution(
            run=run,
            incident=incident,
            asset=asset,
            cancel_checker=cancel_checker,
            events=events,
            lease_fence=lease_fence,
        )
        try:
            outcome = await self._runtime.execute_run(execution)
        finally:
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb_task
        await self._finalize(run_id, fence_token, execution, outcome)

    async def _finalize(
        self,
        run_id: str,
        fence_token: int,
        execution: RunExecution,
        outcome: RunOutcome,
    ) -> None:
        """fencing 回写终态；成功后执行 incident 侧裁决。"""
        finished = await claim.finish_run(
            self._session_factory,
            run_id=run_id,
            fence_token=fence_token,
            status=outcome.status.value,
            stop_reason=outcome.stop_reason.value,
            usage=outcome.usage,
        )
        if not finished:
            logger.warning(
                "run %s fence mismatch (token=%s); discarding result", run_id, fence_token
            )
            return
        with contextlib.suppress(LeaseLostError):
            await execution.events.append(
                RunEventType.RUN_FINISHED,
                {"status": outcome.status.value, "stop_reason": outcome.stop_reason.value},
            )
        # 非正常完成的预算类终态补 unknown 报告快照
        if outcome.report is None and outcome.status is not RunStatus.CANCELED:
            reason = {
                RunStopReason.BUDGET_EXHAUSTED: "预算耗尽（服务端强制终止）",
                RunStopReason.NO_NEW_EVIDENCE: "连续多轮无新证据",
            }.get(outcome.stop_reason, "诊断未完成")
            await self._runtime.write_fallback_report(execution, reason=reason)  # type: ignore[union-attr]

        # incident 裁决：unknown 退出 → NEEDS_HUMAN（状态机 INVESTIGATING → NEEDS_HUMAN）
        if outcome.status is RunStatus.NEEDS_HUMAN:
            async with self._session_factory() as session:
                incident = await session.get(Incident, execution.incident.id)
                if incident is not None:
                    current = IncidentStatus(str(incident.status))
                    require_transition(current, IncidentStatus.NEEDS_HUMAN)
                    incident.status = IncidentStatus.NEEDS_HUMAN.value
                    session.add(incident)
                    await audit_service.record(
                        session,
                        actor_type=ActorType.AGENT.value,
                        actor_id=f"run:{run_id}",
                        action="incident.needs_human",
                        resource_type="incident",
                        resource_id=incident.id,
                        environment=str(incident.environment),
                        detail={"stop_reason": outcome.stop_reason.value},
                    )
                    await session.commit()
        logger.info(
            "run %s finished status=%s stop_reason=%s usage=%s",
            run_id,
            outcome.status.value,
            outcome.stop_reason.value,
            outcome.usage,
        )

    # ---- checkpoint 保留清理（DATA_MODEL §5：随 run 终态保留 N 天）----

    async def _cleanup_expired_checkpoints(self) -> None:
        days = self._settings.checkpoint_retention_days
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    text(
                        f"""
                        DELETE FROM langgraph.checkpoints
                        WHERE thread_id IN (
                            SELECT id FROM investigation_runs
                            WHERE status IN ('completed','needs_human','failed','canceled')
                              AND finished_at < now() - ({days} || ' days')::interval
                        )
                        """
                    )
                )
                await conn.execute(
                    text(
                        """
                        DELETE FROM langgraph.checkpoint_blobs
                        WHERE thread_id NOT IN (SELECT thread_id FROM langgraph.checkpoints)
                        """
                    )
                )
                await conn.execute(
                    text(
                        """
                        DELETE FROM langgraph.checkpoint_writes
                        WHERE thread_id NOT IN (SELECT thread_id FROM langgraph.checkpoints)
                        """
                    )
                )
        except Exception:  # noqa: BLE001 — 表未建/清理失败不阻塞启动
            logger.warning("checkpoint cleanup skipped", exc_info=True)

    async def list_active_run_ids(self) -> list[str]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(InvestigationRun.id).where(
                        InvestigationRun.status.notin_([s.value for s in RUN_TERMINAL_STATUSES])
                    )
                )
            ).scalars()
            return list(rows)

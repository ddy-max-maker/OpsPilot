"""ops-action 入口（D5）：独立受控执行进程。

进程结构：
1. 启动时执行崩溃收敛扫描（幂等账本 claimed 超时 → unknown）；
2. 并发运行：
   - outbox dispatcher：Outbox 事件 → RabbitMQ（轮询，退避重试）；
   - RabbitMQ consumer：消费执行请求（手动 ack + 幂等 + 执行前复核 + 恢复验证）；
   - 收敛扫描（周期）：claimed 超时兜底；
3. SIGTERM/SIGINT 优雅退出。

本进程是唯一允许对演示容器发起受控重启的组件；不与 docker-ro-proxy
共享代码路径，权限面限定在 allowlist 动作内。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

import aio_pika
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.action import convergence
from app.action import outbox as outbox_mod
from app.action.consumer import run_consumer
from app.action.executor import ActionExecutor
from app.core.config import Settings, get_settings
from app.models import ActionExecution, ActionExecutionStatus

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ops-action")


def _build_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    """独立引擎/会话工厂（进程自管生命周期，退出时统一释放）。"""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _pending_executions_exist(
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """存在 pending/running 执行时收敛扫描更频繁（启动追赶）。"""
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ActionExecution.id).where(
                        ActionExecution.status.in_(
                            [
                                ActionExecutionStatus.PENDING,
                                ActionExecutionStatus.RUNNING,
                            ]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        return bool(rows)


async def _convergence_loop(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """周期收敛扫描（间隔随活跃执行数量自适应）。"""
    executor_id = settings.action_executor_id or "ops-action"
    while True:
        try:
            async with session_factory() as session:
                results = await convergence.converge_stale_claims(
                    session,
                    session_factory,
                    settings=settings,
                    executor_id=executor_id,
                )
                # 场景 E：动作成功但验证未完成的执行 → 只读验证恢复（不重发动作）
                resumed = await convergence.resume_pending_verifications(
                    session,
                    session_factory,
                    settings=settings,
                    executor_id=executor_id,
                )
            for r in results + resumed:
                logger.warning("crash convergence: %s -> %s", r["execution_id"], r["action"])
        except Exception:  # noqa: BLE001 — 收敛失败不终止进程
            logger.exception("convergence scan failed")
        active = await _pending_executions_exist(session_factory)
        await asyncio.sleep(30 if active else 120)


async def _outbox_loop(
    session_factory: async_sessionmaker[AsyncSession],
    channel: aio_pika.abc.AbstractChannel,
    settings: Settings,
) -> None:
    """Outbox 轮询发布循环。"""
    while True:
        try:
            dispatched = await outbox_mod.dispatch_batch(session_factory, channel, settings)
            if dispatched:
                logger.info("outbox dispatcher published %s events", dispatched)
        except Exception:  # noqa: BLE001 — 发布失败退避后重试
            logger.exception("outbox dispatch failed")
        await asyncio.sleep(settings.outbox_poll_interval_seconds)


async def main() -> None:
    settings = get_settings()
    session_factory = _build_session_factory(settings.database_url)
    executor_id = settings.action_executor_id or "ops-action"
    logger.info("ops-action starting (executor=%s)", executor_id)

    # ---- 启动收敛扫描（崩溃后恢复：claimed 超时 → unknown；验证缺失 → 只读恢复）----
    async with session_factory() as session:
        results = await convergence.converge_stale_claims(
            session, session_factory, settings=settings, executor_id=executor_id
        )
        resumed = await convergence.resume_pending_verifications(
            session, session_factory, settings=settings, executor_id=executor_id
        )
    for r in results + resumed:
        logger.warning("startup convergence: %s -> %s", r["execution_id"], r["action"])

    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    channel = await connection.channel()
    await outbox_mod.declare_topology(channel)

    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover — Windows 开发环境
            signal.signal(sig, lambda *_: stop.set())

    consumer_task = asyncio.create_task(
        run_consumer(
            session_factory,
            rabbitmq_url=settings.rabbitmq_url,
            settings=settings,
            executor=ActionExecutor(container_name_prefix=settings.action_container_name_prefix),
            executor_id=executor_id,
        )
    )
    outbox_task = asyncio.create_task(_outbox_loop(session_factory, channel, settings))
    convergence_task = asyncio.create_task(_convergence_loop(session_factory, settings))

    logger.info("ops-action ready (consumer/outbox/convergence running)")
    await stop.wait()
    consumer_task.cancel()
    outbox_task.cancel()
    convergence_task.cancel()
    await asyncio.gather(consumer_task, outbox_task, convergence_task, return_exceptions=True)
    await connection.close()
    engine = session_factory.get_bind()  # type: ignore[attr-defined]
    await engine.dispose()
    logger.info("ops-action stopped")


if __name__ == "__main__":
    asyncio.run(main())

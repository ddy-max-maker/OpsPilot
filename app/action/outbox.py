"""Outbox Dispatcher（D5）：Outbox 表 → RabbitMQ（Transactional Outbox 模式）。

保证（用户指令 §5）：
- 业务变更与 Outbox 写入同一 PostgreSQL 事务（由 execute_proposal 保证）；
- 本进程独立轮询 pending 事件并发布，绝不绕过 Outbox 直接 publish；
- 发布失败退避重试（指数退避，上限 max_attempts → failed，可人工介入）；
- 进程崩溃后重启可继续投递（以数据库状态为准，消息幂等由消费侧兜底）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import aio_pika
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.enums import OutboxStatus
from app.models import OutboxEvent

logger = logging.getLogger("ops-action.outbox")

EXCHANGE_NAME = "opspilot.events"
ROUTING_KEY_EXECUTE = "action.execute_requested"

# 事件类型 → routing key（消费侧按 routing key 绑定队列）
ROUTING_KEYS: dict[str, str] = {
    "action.execute_requested": ROUTING_KEY_EXECUTE,
}


_EXCHANGE: aio_pika.abc.AbstractExchange | None = None


async def _topic_exchange(channel: aio_pika.abc.AbstractChannel) -> aio_pika.abc.AbstractExchange:
    """惰性声明主题 exchange（幂等声明；进程内缓存）。"""
    global _EXCHANGE
    if _EXCHANGE is None:
        _EXCHANGE = await channel.declare_exchange(
            EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
        )
    return _EXCHANGE


async def publish_event(
    channel: aio_pika.abc.AbstractChannel,
    event: OutboxEvent,
) -> None:
    """单条事件发布（持久化消息；消费侧按 event_id 幂等）。"""
    routing_key = ROUTING_KEYS.get(event.event_type)
    if routing_key is None:
        raise ValueError(f"no routing key mapped for event type '{event.event_type}'")
    exchange = await _topic_exchange(channel)
    message = aio_pika.Message(
        body=json.dumps(event.payload, ensure_ascii=False).encode(),
        headers={"event_id": event.id, "event_type": event.event_type},
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
    )
    await exchange.publish(message, routing_key=routing_key)


async def dispatch_batch(
    session_factory: async_sessionmaker[AsyncSession],
    channel: aio_pika.abc.AbstractChannel,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> int:
    """扫描 pending 事件并发布（单批；逐条独立事务，失败退避）。

    返回成功发布的条数。FOR UPDATE SKIP LOCKED 防多 dispatcher 重复消费。
    """
    now = now or datetime.now(UTC)
    dispatched = 0
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.status == OutboxStatus.PENDING.value,
                        (OutboxEvent.next_attempt_at.is_(None))
                        | (OutboxEvent.next_attempt_at <= now),
                    )
                    .order_by(OutboxEvent.id)
                    .limit(settings.outbox_batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for event in rows:
            try:
                await publish_event(channel, event)
            except Exception as exc:  # noqa: BLE001 — 单条失败不阻塞整批
                event.attempts += 1
                if event.attempts >= settings.outbox_max_attempts:
                    event.status = OutboxStatus.FAILED.value
                    logger.error(
                        "outbox event %s permanently failed after %s attempts: %s",
                        event.id,
                        event.attempts,
                        exc,
                    )
                else:
                    backoff = settings.outbox_retry_backoff_seconds * (2 ** min(event.attempts, 5))
                    event.next_attempt_at = datetime.now(UTC) + timedelta(seconds=backoff)
                    logger.warning(
                        "outbox event %s dispatch failed (attempt %s): %s",
                        event.id,
                        event.attempts,
                        exc,
                    )
            else:
                event.status = OutboxStatus.DISPATCHED.value
                event.dispatched_at = datetime.now(UTC)
                dispatched += 1
            await session.commit()
    return dispatched


async def declare_topology(channel: aio_pika.abc.AbstractChannel) -> Any:
    """声明 exchange / 队列 / DLQ（幂等；durable 保证 Broker 重启后拓扑仍在）。"""
    exchange = await channel.declare_exchange(
        EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
    )
    dlx = await channel.declare_exchange("opspilot.dlx", aio_pika.ExchangeType.TOPIC, durable=True)
    dlq = await channel.declare_queue("ops.action.requests.dlq", durable=True)
    await dlq.bind(dlx, routing_key=ROUTING_KEY_EXECUTE)
    queue = await channel.declare_queue(
        "ops.action.requests",
        durable=True,
        arguments={
            "x-dead-letter-exchange": "opspilot.dlx",
            "x-dead-letter-routing-key": ROUTING_KEY_EXECUTE,
        },
    )
    await queue.bind(exchange, routing_key=ROUTING_KEY_EXECUTE)
    return exchange, queue

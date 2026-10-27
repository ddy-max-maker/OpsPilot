"""API 幂等服务（CONTRACTS §1.3）。

语义：
- 隔离维度 = (user_id, endpoint, key) 三元组；不适用于未认证端点；
- 相同 key + 相同请求体哈希 → 重放返回首次响应（调用方附 Idempotency-Replayed: true）；
- 相同 key + 不同请求体 → 409 IDEMPOTENCY_KEY_MISMATCH；
- 并发同 key：唯一约束（PK）抢占兜底，后到者短暂等待首次完成后重放，
  超时则 409 CONFLICT_STATE（客户端可安全重试）；
- 仅 2xx 响应入库：业务失败整体回滚（幂等记录随事务消失），key 不被烧掉；
- 保留 24h（settings.idempotency_ttl_hours），过期残留行由下一次抢占回收。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import IdempotencyState
from app.core.errors import conflict_state, idempotency_key_mismatch, validation_error
from app.models import ApiIdempotencyKey

if TYPE_CHECKING:
    from app.core.config import Settings

# 并发等待首次完成的最大时长（单事件循环内足够；跨进程场景由 409 + 客户端重试兜底）
_WAIT_INTERVAL_SECONDS = 0.15
_WAIT_MAX_ATTEMPTS = 13


def hash_request_body(raw_body: bytes) -> str:
    """请求体哈希（字节级：语义相同但序列化不同的请求体视为不同）。"""
    return hashlib.sha256(raw_body).hexdigest()


def endpoint_tag(method: str, path: str) -> str:
    """端点标识（隔离维度之一），形如 "POST /api/v1/incidents"。"""
    return f"{method.upper()} {path}"


async def _get_key_row(
    session: AsyncSession, *, key: str, endpoint: str, user_id: str
) -> ApiIdempotencyKey | None:
    stmt = select(ApiIdempotencyKey).where(
        ApiIdempotencyKey.key == key,
        ApiIdempotencyKey.endpoint == endpoint,
        ApiIdempotencyKey.user_id == user_id,
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def _replay_completed(
    session: AsyncSession, row: ApiIdempotencyKey, request_hash: str
) -> tuple[int, dict[str, Any], bool]:
    """已完成记录校验哈希后重放；哈希不匹配抛 409。"""
    if row.request_hash != request_hash:
        raise idempotency_key_mismatch()
    body = row.response_body
    if row.response_status is None or body is None:
        raise conflict_state("idempotency record is corrupted")
    return int(row.response_status), dict(body), True


async def run_idempotent(
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: str,
    endpoint: str,
    key: str,
    request_hash: str,
    execute: Callable[[], Awaitable[tuple[int, dict[str, Any]]]],
) -> tuple[int, dict[str, Any], bool]:
    """幂等执行器：抢占 (user, endpoint, key) → 执行业务 → 存响应 → 单事务提交。

    execute() 不得自行提交（业务服务以 commit=False 运行）；
    execute() 抛出 ApiError 时整个事务回滚，幂等记录一并消失（key 不被烧掉）。

    返回 (status_code, response_body, replayed)。
    """
    if not (1 <= len(key) <= 200):
        raise validation_error("Idempotency-Key must be 1..200 characters")

    row: ApiIdempotencyKey | None = None
    while True:
        row = ApiIdempotencyKey(
            key=key,
            endpoint=endpoint,
            user_id=user_id,
            request_hash=request_hash,
            state=IdempotencyState.RECEIVED.value,
            expires_at=datetime.now(UTC) + timedelta(hours=settings.idempotency_ttl_hours),
        )
        session.add(row)
        try:
            await session.flush()
            break  # 抢占成功
        except IntegrityError:
            await session.rollback()
            existing = await _get_key_row(session, key=key, endpoint=endpoint, user_id=user_id)
            if existing is not None and existing.expires_at > datetime.now(UTC):
                if existing.state == IdempotencyState.COMPLETED.value:
                    return await _replay_completed(session, existing, request_hash)
                # received（并发进行中）：短暂等待首次完成
                for _ in range(_WAIT_MAX_ATTEMPTS):
                    await asyncio.sleep(_WAIT_INTERVAL_SECONDS)
                    await session.refresh(existing)
                    if existing.state == IdempotencyState.COMPLETED.value:
                        return await _replay_completed(session, existing, request_hash)
                raise conflict_state(
                    "idempotent request is still in progress, retry later"
                ) from None
            # 过期残留：回收后重新抢占
            if existing is not None:
                await session.delete(existing)
                await session.commit()
                continue
            raise conflict_state("idempotency key race detected, retry request") from None

    status_code, body = await execute()
    assert row is not None
    row.state = IdempotencyState.COMPLETED.value
    row.response_status = status_code
    row.response_body = body
    await session.commit()
    return status_code, body, False

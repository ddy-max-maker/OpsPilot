"""ops-worker 任务认领（ADR-4/ADR-12：DB 行级认领 + 租约 + fence_token）。

要点：
- 认领：`FOR UPDATE SKIP LOCKED` 原子抢占 queued（或租约过期的 running）行，
  fence_token 单调 +1；重复任务不可能同时拥有两个有效执行者；
- 所有 run 行回写必须携带 fence_token（WHERE fence_token = :token），
  旧 Worker 租约过期复活后回写被拒绝（rowcount=0）；
- 心跳续租与取消检查合并为一次轮询（heartbeat_or_cancel）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_CLAIM_SQL = text(
    """
    UPDATE investigation_runs AS r
    SET status = 'running',
        lease_owner = :worker_id,
        fence_token = COALESCE(r.fence_token, 0) + 1,
        claim_expires_at = :claim_expires_at,
        started_at = COALESCE(r.started_at, :started_at),
        updated_at = now()
    WHERE r.id = (
        SELECT c.id
        FROM investigation_runs AS c
        WHERE c.status = 'queued'
           OR (c.status = 'running' AND c.claim_expires_at < now())
        ORDER BY c.created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING r.id, r.incident_id, r.fence_token, r.budgets, r.usage,
              r.authorization_snapshot, r.model_config AS model_config_json,
              r.langgraph_thread_id, r.status, r.started_at, r.finished_at,
              r.created_at, r.updated_at, r.triggered_by, r.stop_reason,
              r.cancel_requested, r.lease_owner, r.claim_expires_at
    """
)

_FENCED_UPDATE_SQL = text(
    """
    UPDATE investigation_runs
    SET status = :status,
        stop_reason = :stop_reason,
        usage = CAST(:usage AS jsonb),
        finished_at = COALESCE(:finished_at, finished_at),
        cancel_requested = CASE WHEN :status = 'canceled' THEN false ELSE cancel_requested END,
        updated_at = now()
    WHERE id = :run_id AND fence_token = :fence_token
    """
)

_CANCEL_FLAG_SQL = text(
    """
    UPDATE investigation_runs
    SET cancel_requested = true, updated_at = now()
    WHERE id = :run_id AND cancel_requested = false
    RETURNING id
    """
)


async def claim_next_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    worker_id: str,
    lease_ttl_seconds: int,
) -> dict[str, Any] | None:
    """原子认领一个可执行 run（无任务返回 None）。"""
    now = datetime.now(UTC)
    async with session_factory() as session:
        result = await session.execute(
            _CLAIM_SQL,
            {
                "worker_id": worker_id,
                "claim_expires_at": now + timedelta(seconds=lease_ttl_seconds),
                "started_at": now,
            },
        )
        row = result.mappings().first()
        await session.commit()
    return dict(row) if row else None


async def finish_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: str,
    fence_token: int,
    status: str,
    stop_reason: str | None,
    usage: dict[str, Any],
) -> bool:
    """fencing 回写终态（旧 Worker 回写返回 False 且不影响状态）。"""
    async with session_factory() as session:
        result = cast(
            CursorResult[Any],
            await session.execute(
                _FENCED_UPDATE_SQL,
                {
                    "run_id": run_id,
                    "fence_token": fence_token,
                    "status": status,
                    "stop_reason": stop_reason,
                    "usage": _to_json(usage),
                    "finished_at": datetime.now(UTC),
                },
            ),
        )
        await session.commit()
    return result.rowcount == 1


async def heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: str,
    fence_token: int,
    lease_ttl_seconds: int,
) -> bool:
    """续租（fencing 校验；租约丢失返回 False，Worker 应立即停止）。"""
    async with session_factory() as session:
        result = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    """
                    UPDATE investigation_runs
                    SET claim_expires_at = :expires, updated_at = now()
                    WHERE id = :run_id AND fence_token = :fence_token
                      AND status = 'running'
                    """
                ),
                {
                    "run_id": run_id,
                    "fence_token": fence_token,
                    "expires": datetime.now(UTC) + timedelta(seconds=lease_ttl_seconds),
                },
            ),
        )
        await session.commit()
    return result.rowcount == 1


async def request_cancel_flag(
    session_factory: async_sessionmaker[AsyncSession], run_id: str
) -> bool:
    """取消标志置位（幂等；返回是否本次置位）。"""
    async with session_factory() as session:
        result = cast(
            CursorResult[Any],
            await session.execute(_CANCEL_FLAG_SQL, {"run_id": run_id}),
        )
        await session.commit()
    return result.rowcount == 1


async def record_execution_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: str,
    fence_token: int,
    snapshot: dict[str, Any],
) -> bool:
    """D4 安全修复③：认领后写入实际执行快照（fencing 校验，可审计）。"""
    async with session_factory() as session:
        result = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    """
                    UPDATE investigation_runs
                    SET execution_snapshot = CAST(:snapshot AS jsonb), updated_at = now()
                    WHERE id = :run_id AND fence_token = :fence_token
                    """
                ),
                {
                    "run_id": run_id,
                    "fence_token": fence_token,
                    "snapshot": _to_json(snapshot),
                },
            ),
        )
        await session.commit()
    return result.rowcount == 1


def _to_json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)

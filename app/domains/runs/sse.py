"""SSE 实时诊断事件流（D6，PRD §12 / CONTRACTS §2.5）：

仅当客户端显式发送 Accept: text/event-stream 时由 API 层启用；默认 JSON 分页
向后兼容（D2 契约不变）。语义约定：

- 事件源为 PostgreSQL run_events（真实持久化数据，不伪造）；
- SSE 帧 id = run_events.seq（run 内单调持久化序号），Last-Event-ID 断线重放；
- 心跳为传输层注释行（": ping"），不产生任何伪造诊断事件；
- Run 进入终态且事件全部推送后发送 end 帧并正常关闭；
- 订阅前的登录态与资产读取权限校验在 API 层完成（越权 404 伪装）。

每轮轮询使用独立短事务，避免长期占用连接池会话；断线后客户端携带
Last-Event-ID 重连，服务端按 seq 严格续传——不丢事件、不重复推送。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.enums import RUN_TERMINAL_STATUSES, RunStatus
from app.domains.runs import repository
from app.models import RunEvent

# 统一事件名；具体诊断事件类型放 data.type（前端按 type 分发渲染）
_SSE_EVENT_NAME = "run_event"
_POLL_BATCH = 200


def sse_frame(*, event_id: int | None, event: str, data: dict[str, Any]) -> str:
    """构造一条 SSE 帧（以空行结尾）。"""
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


def _event_payload(event: RunEvent) -> dict[str, Any]:
    return {
        "run_id": event.run_id,
        "seq": int(event.seq),
        "type": str(event.type),
        "payload": dict(event.payload or {}),
        "created_at": event.created_at.isoformat(),
    }


async def stream_run_events(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    run_id: str,
    *,
    last_seq: int,
) -> AsyncIterator[str]:
    """run_events SSE 生成器：轮询 → 补发 → 终态结束。

    last_seq 为起始游标（客户端 Last-Event-ID 或 after_seq），只推送 seq 严格
    大于该游标的事件——重连重放既不丢也不重复。
    """
    yield "retry: 3000\n\n"
    cursor = last_seq
    last_output = time.monotonic()
    started = last_output
    while True:
        async with session_factory() as session:
            run = await repository.get_run(session, run_id)
            rows = (
                await repository.list_events(session, run_id, after_seq=cursor, limit=_POLL_BATCH)
                if run is not None
                else []
            )
        if rows:
            for event in rows:
                cursor = int(event.seq)
                yield sse_frame(
                    event_id=cursor, event=_SSE_EVENT_NAME, data=_event_payload(event)
                )
            last_output = time.monotonic()
            continue  # 可能仍有积压，立即续拉（不带 sleep）
        if run is None:
            yield sse_frame(event_id=None, event="end", data={"reason": "run_gone"})
            return
        if RunStatus(str(run.status)) in RUN_TERMINAL_STATUSES:
            yield sse_frame(
                event_id=None,
                event="end",
                data={"reason": "run_finished", "status": str(run.status)},
            )
            return
        now = time.monotonic()
        if now - last_output >= settings.sse_heartbeat_seconds:
            yield ": ping\n\n"  # 传输层心跳注释，非业务事件
            last_output = now
        if now - started >= settings.sse_max_stream_seconds:
            # 服务端最长连接保护：客户端凭 Last-Event-ID 无损重连续传
            yield sse_frame(event_id=None, event="end", data={"reason": "server_timeout"})
            return
        await asyncio.sleep(settings.sse_poll_interval_seconds)

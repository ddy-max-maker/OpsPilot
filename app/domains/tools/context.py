"""Run 执行上下文：预算计数、取消检查、run_events 追加（服务端硬限制落点）。

预算/取消/事件写入只存在于本模块与 Worker 编排层（ADR-D2）：
- max_tool_calls：Gateway 每次调用前递增并校验（超限 → ToolBudgetExhausted）；
- max_iterations / deadline / max_tokens：Runtime 步进循环校验；
- cancel_requested：DB 标志，工具调用前与步进间轮询（合作式取消）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.enums import RunEventType
from app.models import RunEvent

CancelChecker = Callable[[], Awaitable[bool]]


class ToolBudgetExhausted(RuntimeError):
    """max_tool_calls 耗尽（服务端强制，模型无法通过 Prompt 绕过）。"""


class RunCanceled(RuntimeError):
    """用户请求取消（合作式停止）。"""


@dataclass
class RunBudget:
    """服务端预算状态（Worker 持有，落库到 investigation_runs.usage）。"""

    max_tool_calls: int
    max_iterations: int
    max_tokens: int
    deadline_at: datetime
    tool_calls_used: int = 0
    iterations_used: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    def as_usage(self) -> dict[str, int]:
        return {
            "tool_calls_used": self.tool_calls_used,
            "iterations_used": self.iterations_used,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
        }

    def deadline_exceeded(self, now: datetime) -> bool:
        return now >= self.deadline_at


@dataclass
class RunToolContext:
    """单次 run 执行期间工具层共享的可变上下文。"""

    run_id: str
    environment: str
    primary_asset_id: str
    is_admin: bool
    authorized_asset_ids: frozenset[str]
    authorized_environments: frozenset[str]
    budget: RunBudget
    cancel_checker: CancelChecker
    connector_configs: dict[str, dict[str, Any]]  # connector_code -> config
    all_assets: bool = False
    rate_state: dict[str, float] = field(default_factory=dict)
    _seq: int = 0
    _seq_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def asset_authorized(self, asset_id: str, environment: str) -> bool:
        """授权快照判定（默认拒绝；Admin/all_assets 不受限）。"""
        if self.is_admin or self.all_assets:
            return True
        if asset_id in self.authorized_asset_ids:
            return True
        return environment in self.authorized_environments

    async def check_tool_budget(self) -> None:
        """工具调用计数 +1（超限抛 ToolBudgetExhausted；取消则抛 RunCanceled）。"""
        if self.budget.tool_calls_used >= self.budget.max_tool_calls:
            raise ToolBudgetExhausted(f"max_tool_calls={self.budget.max_tool_calls} exhausted")
        if await self.cancel_checker():
            raise RunCanceled("cancel requested")
        self.budget.tool_calls_used += 1

    def consume_connector_rate(self, connector_code: str, max_calls: int) -> None:
        """按连接器限流（max_calls_per_run；超限按预算耗尽语义拒绝）。"""
        used = self.rate_state.get(connector_code, 0.0)
        if used >= max_calls:
            raise ToolBudgetExhausted(
                f"connector '{connector_code}' rate limit ({max_calls} per run) exhausted"
            )
        self.rate_state[connector_code] = used + 1


class RunEventWriter:
    """run_events 追加器：run 内单调 seq，事务独立提交（UI 轮询可见）。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        run_id: str,
        *,
        start_seq: int = 0,
    ) -> None:
        self._session_factory = session_factory
        self._run_id = run_id
        self._seq = start_seq
        self._seq_lock = asyncio.Lock()

    @classmethod
    async def create(
        cls, session_factory: async_sessionmaker[AsyncSession], run_id: str
    ) -> RunEventWriter:
        async with session_factory() as session:
            current = await session.scalar(
                select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)
            )
        return cls(session_factory, run_id, start_seq=int(current or 0))

    @property
    def last_seq(self) -> int:
        return self._seq

    async def append(
        self,
        event_type: RunEventType,
        payload: dict[str, Any] | None = None,
    ) -> int:
        async with self._seq_lock:
            self._seq += 1
            seq = self._seq
        async with self._session_factory() as session:
            session.add(
                RunEvent(run_id=self._run_id, seq=seq, type=event_type.value, payload=payload)
            )
            await session.commit()
        return seq


def default_deadline(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)

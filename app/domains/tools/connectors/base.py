"""只读连接器基础类型。

连接器只做：给定目标配置 + 受控参数 → 原始结果或失败信息。
Evidence 归一化、脱敏、落库、审计全部由 Tool Gateway 负责（职责边界见 ADR-D2）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.enums import EvidenceStatus


@dataclass(frozen=True)
class ConnectorResult:
    """连接器原始返回：成功携带 payload，失败携带 status 与 error_code。

    失败不伪装为空数据（PRD §5）：status ∈ error/timeout/unavailable 时
    payload 为失败上下文（如 HTTP 状态码、连接错误类型）。
    """

    status: EvidenceStatus
    payload: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is EvidenceStatus.OK

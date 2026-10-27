"""Incident 状态机（CONTRACTS §2.3，PRD §9 冻结）：服务端强制，非法转换 409。

D1-B 仅暴露 acknowledge（OPEN→ACKNOWLEDGED）；
其余转换由后续阶段触发（D2 run 启动、D5 执行/验证、人工关单），
但转换表在此完整定义并可单元测试。
"""

from __future__ import annotations

from app.core.enums import IncidentStatus
from app.core.errors import conflict_state

ALLOWED_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.OPEN: frozenset({IncidentStatus.ACKNOWLEDGED, IncidentStatus.NEEDS_HUMAN}),
    IncidentStatus.ACKNOWLEDGED: frozenset({IncidentStatus.INVESTIGATING}),
    IncidentStatus.INVESTIGATING: frozenset(
        {IncidentStatus.MITIGATING, IncidentStatus.NEEDS_HUMAN}
    ),
    IncidentStatus.MITIGATING: frozenset({IncidentStatus.MONITORING, IncidentStatus.NEEDS_HUMAN}),
    IncidentStatus.MONITORING: frozenset({IncidentStatus.RESOLVED, IncidentStatus.INVESTIGATING}),
    IncidentStatus.NEEDS_HUMAN: frozenset({IncidentStatus.INVESTIGATING, IncidentStatus.CLOSED}),
    # RESOLVED 只能由恢复验证 PASSED 触达（PRD S9："命令成功"不是解决）
    IncidentStatus.RESOLVED: frozenset({IncidentStatus.CLOSED}),
    IncidentStatus.CLOSED: frozenset(),
}


def can_transition(current: IncidentStatus, target: IncidentStatus) -> bool:
    """判定转换合法性（纯函数，可单元测试）。"""
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def require_transition(current: IncidentStatus, target: IncidentStatus) -> None:
    """非法转换一律 409 CONFLICT_STATE（CONTRACTS §2.3）。"""
    if not can_transition(current, target):
        raise conflict_state(
            "incident status transition is not allowed",
            details={"from": str(current.value), "to": str(target.value)},
        )

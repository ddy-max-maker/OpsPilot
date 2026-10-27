"""Incident 状态机单元测试（CONTRACTS §2.3 冻结转换表）。"""

from __future__ import annotations

import pytest

from app.core.enums import IncidentStatus
from app.core.errors import ApiError
from app.domains.incidents.state_machine import (
    ALLOWED_TRANSITIONS,
    can_transition,
    require_transition,
)


def test_all_eight_statuses_covered() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(IncidentStatus)


def test_forward_path_transitions() -> None:
    assert can_transition(IncidentStatus.OPEN, IncidentStatus.ACKNOWLEDGED)
    assert can_transition(IncidentStatus.ACKNOWLEDGED, IncidentStatus.INVESTIGATING)
    assert can_transition(IncidentStatus.INVESTIGATING, IncidentStatus.MITIGATING)
    assert can_transition(IncidentStatus.MITIGATING, IncidentStatus.MONITORING)
    assert can_transition(IncidentStatus.MONITORING, IncidentStatus.RESOLVED)
    assert can_transition(IncidentStatus.RESOLVED, IncidentStatus.CLOSED)


def test_escalation_and_backout_transitions() -> None:
    # 升级人工通道
    assert can_transition(IncidentStatus.OPEN, IncidentStatus.NEEDS_HUMAN)
    assert can_transition(IncidentStatus.INVESTIGATING, IncidentStatus.NEEDS_HUMAN)
    assert can_transition(IncidentStatus.MITIGATING, IncidentStatus.NEEDS_HUMAN)
    # NEEDS_HUMAN 回到调查或关闭
    assert can_transition(IncidentStatus.NEEDS_HUMAN, IncidentStatus.INVESTIGATING)
    assert can_transition(IncidentStatus.NEEDS_HUMAN, IncidentStatus.CLOSED)
    # 监控未通过回到调查
    assert can_transition(IncidentStatus.MONITORING, IncidentStatus.INVESTIGATING)


def test_closed_is_terminal() -> None:
    assert not any(can_transition(IncidentStatus.CLOSED, target) for target in IncidentStatus)
    assert ALLOWED_TRANSITIONS[IncidentStatus.CLOSED] == frozenset()


def test_self_transition_is_illegal() -> None:
    for status in IncidentStatus:
        assert not can_transition(status, status), f"{status} -> {status} 应非法"


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (IncidentStatus.OPEN, IncidentStatus.INVESTIGATING),  # 必须先确认
        (IncidentStatus.OPEN, IncidentStatus.RESOLVED),
        (IncidentStatus.OPEN, IncidentStatus.CLOSED),
        (IncidentStatus.ACKNOWLEDGED, IncidentStatus.RESOLVED),
        (IncidentStatus.ACKNOWLEDGED, IncidentStatus.NEEDS_HUMAN),
        (IncidentStatus.INVESTIGATING, IncidentStatus.RESOLVED),
        (IncidentStatus.INVESTIGATING, IncidentStatus.ACKNOWLEDGED),  # 状态不可回退
        (IncidentStatus.MITIGATING, IncidentStatus.RESOLVED),  # 必须经过 monitoring
        (IncidentStatus.MONITORING, IncidentStatus.CLOSED),  # 必须先 resolved
        (IncidentStatus.RESOLVED, IncidentStatus.OPEN),  # 终局前不可回退
        (IncidentStatus.NEEDS_HUMAN, IncidentStatus.RESOLVED),
    ],
)
def test_illegal_transitions(current: IncidentStatus, target: IncidentStatus) -> None:
    assert not can_transition(current, target)


def test_require_transition_raises_conflict_with_details() -> None:
    with pytest.raises(ApiError) as excinfo:
        require_transition(IncidentStatus.OPEN, IncidentStatus.CLOSED)
    assert excinfo.value.status_code == 409
    assert excinfo.value.code.value == "CONFLICT_STATE"
    assert excinfo.value.details == {"from": "open", "to": "closed"}


def test_require_transition_passes_on_legal_move() -> None:
    require_transition(IncidentStatus.OPEN, IncidentStatus.ACKNOWLEDGED)  # 不抛出

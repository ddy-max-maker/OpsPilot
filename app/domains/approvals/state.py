"""Proposal 状态机强制（CONTRACTS §2.11）：非法转换一律 409。"""

from __future__ import annotations

from app.core.errors import conflict_state
from app.models import ProposalStatus


def require_proposal_transition(current: str, target: str) -> None:
    """目标必须是当前状态的合法去向（转换表见 models.execution.ProposalStatus）。"""
    allowed = ProposalStatus.TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise conflict_state(
            "proposal status transition is not allowed",
            details={"from": current, "to": target},
        )

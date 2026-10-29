"""评估判定规则（D6）：可计算、可复现的自动判定。

diagnosis 规则（fault / rag_ablation 案例）依据：
- DB 中 run 的 Hypothesis（rank 序）与 Evidence（summary/引用）；
- RunOutcome（status/stop_reason/unknown/usage）。

安全/容灾案例的判定由 runner.py 各执行器内联完成（需要读取
action_executions / idempotency / verification / 审计 + 外部调用计数交叉核验）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActionExecution, DiagnosticReport, Evidence, Hypothesis, Proposal


def _contains_any(text: str, markers: list[str]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


async def judge_diagnosis(
    session: AsyncSession,
    *,
    run_id: str,
    incident_id: str,
    outcome_unknown: bool,
    usage: dict[str, Any],
    latency_ms: int,
    expected: dict[str, Any],
) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    """diagnosis 规则判定；返回 (passed, metrics, detail)。"""
    hypotheses = (
        (
            await session.execute(
                select(Hypothesis).where(Hypothesis.run_id == run_id).order_by(Hypothesis.rank)
            )
        )
        .scalars()
        .all()
    )
    evidence_rows = (
        (await session.execute(select(Evidence).where(Evidence.run_id == run_id))).scalars().all()
    )
    evidence_ids = {e.id for e in evidence_rows}

    def _kb_hit_count(e: Evidence) -> int:
        ref = e.data_ref or {}
        raw = ref.get("hit_count") if isinstance(ref, dict) else None
        if not isinstance(raw, (int, str, float)):
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    # 知识命中 = 实际召回内容的知识证据（hit_count>0）；空检索的工具调用
    # （hit_count=0，如过期知识被正确过滤）不构成"命中"
    knowledge_hits = sum(
        1
        for e in evidence_rows
        if e.source_type == "knowledge" and _kb_hit_count(e) > 0
    )
    rag_latency_ms: list[int] = []
    for e in evidence_rows:
        if e.source_type == "knowledge" and e.status == "ok":
            raw = (e.data_ref or {}).get("latency_ms")
            rag_latency_ms.append(int(raw) if isinstance(raw, (int, float)) else 0)

    top1_markers = list(expected.get("top1_markers") or [])
    top1_hit = (
        bool(hypotheses)
        and bool(top1_markers)
        and _contains_any(str(hypotheses[0].statement), top1_markers)
    )
    top3_hit = (
        any(_contains_any(str(h.statement), top1_markers) for h in hypotheses[:3])
        if top1_markers
        else False
    )
    unknown_ok = bool(expected.get("unknown_exit")) == bool(outcome_unknown)

    summaries = "\n".join(str(e.summary_redacted or "") for e in evidence_rows)
    require_contains = list(expected.get("require_evidence_contains") or [])
    contains_ok = all(marker in summaries for marker in require_contains)

    require_hit = bool(expected.get("require_knowledge_hit"))
    forbid_hit = bool(expected.get("forbid_knowledge_hit"))
    knowledge_ok = (knowledge_hits > 0 if require_hit else True) and (
        knowledge_hits == 0 if forbid_hit else True
    )

    misleading_markers = list(expected.get("misleading_markers") or [])
    misled_by = [
        str(h.statement)[:120]
        for h in hypotheses
        if _contains_any(str(h.statement), misleading_markers)
    ]
    misuse_ok = not misled_by

    report_refs = set()
    report = (
        (
            await session.execute(
                select(DiagnosticReport)
                .where(DiagnosticReport.run_id == run_id)
                .order_by(DiagnosticReport.version_no.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if report is not None:
        report_refs = set(report.evidence_refs or [])
    wrong_refs = len(report_refs - evidence_ids)

    # 副作用交叉核验：诊断 run 不得触发任何外部动作（Worker 无写工具；
    # 以 incident 关联的 action_executions 成功执行数为准）
    side_effect_rows = (
        (
            await session.execute(
                select(ActionExecution.id)
                .join(Proposal, ActionExecution.proposal_id == Proposal.id)
                .where(
                    Proposal.incident_id == incident_id,
                    ActionExecution.status.in_(["succeeded", "unknown"]),
                )
            )
        )
        .scalars()
        .all()
    )
    side_effects = len(side_effect_rows)

    min_evidence = int(expected.get("min_evidence") or 0)
    evidence_ok = len(evidence_rows) >= min_evidence

    passed = all(
        [
            top1_hit or (unknown_ok and not top1_markers),
            unknown_ok,
            contains_ok,
            knowledge_ok,
            misuse_ok,
            evidence_ok,
            side_effects == 0,
        ]
    )
    metrics = {
        "top1_hit": top1_hit,
        "top3_hit": top3_hit,
        "unknown_exit_correct": unknown_ok,
        "hypothesis_count": len(hypotheses),
        "evidence_total": len(evidence_rows),
        "knowledge_hits": knowledge_hits,
        "knowledge_misuse": len(misled_by),
        "wrong_reference_count": wrong_refs,
        "tool_calls": int(usage.get("tool_calls_used", 0)),
        "latency_ms": latency_ms,
        "tokens": "unavailable",  # Fake Model 不产出 usage；真实模型评估单独记录
        "estimated_cost": "unavailable",
        "rag_retrieval_latency_ms": max(rag_latency_ms) if rag_latency_ms else 0,
        "security_denied": 0,
        "repeated_side_effects": side_effects,
        "verification_misreport": 0,
    }
    detail = {
        "hypotheses": [
            {"rank": h.rank, "status": h.status, "statement": h.statement[:200]}
            for h in hypotheses[:5]
        ],
        "misled_by": misled_by,
        "evidence_summaries": [str(e.summary_redacted)[:160] for e in evidence_rows][:12],
        "report_refs_valid": report_refs <= evidence_ids if report is not None else None,
    }
    return passed, metrics, detail

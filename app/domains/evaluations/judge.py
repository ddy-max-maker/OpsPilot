"""评估判定规则（D6 / D6 收尾 judge-v2）。

**版本化**：`judge-v1` 是 D6 首版规则（旧成绩的口径，永久保留、行为不变）；
`judge-v2` 是 D6 收尾引入的新口径（本次真实模型回归使用），差异见下：

| 维度 | judge-v1 | judge-v2 |
|---|---|---|
| Top-1/Top-3 | 只对 `statement` 做标记词包含判断 | 标记词匹配**或**所引用证据的
  `error_code` 命中（证据锚定）；纯文本匹配还须至少引用 1 条本 run 证据 |
| rank 1 的 refuted | 直接判 top1 失败 | 跳过错判否定项，取「最佳未否定假设」 |
| 误导知识 | 任何假设提到误导标记即判采信 | 仅「未否定且（supported 或排在正确
  根因之前）」判采信；refuted / 靠后的 insufficient_data 不算 |
| 引用 | 只报 wrong_reference_count | 引用有效性与证据支持性分列统计 |
| 直接 vs 深层 | 无区分 | 仅网关 5xx 证据却断言具体深层根因 → 计数 |
| 报告生成 | 不区分 | report_status=failed → 判为 structured_output_error |

**未放宽**：v2 要求「有证据支撑」的根因才算命中，且不支持用文本关键词绕过证据；
缺少证据的错误结论依旧不能通过（top1 必须由真实证据锚定）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActionExecution, DiagnosticReport, Evidence, Hypothesis, Proposal

JUDGE_VERSION_V1 = "judge-v1"
JUDGE_VERSION_V2 = "judge-v2"
JUDGE_VERSION = JUDGE_VERSION_V2

# 具体深层根因措辞（仅当全部支撑证据都是网关 5xx 时才算「未证实深层断言」）
_DEEP_CAUSE_MARKERS = (
    "内存泄漏",
    "oom",
    "out of memory",
    "连接池耗尽",
    "线程池耗尽",
    "配置错误",
    "代码缺陷",
    "磁盘写满",
    "部署回滚",
    "内核",
    "kernel",
    "网络分区",
    "dns",
    "payments",
)


def _contains_any(text: str, markers: list[str]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


async def _load_case_state(
    session: AsyncSession, *, run_id: str, incident_id: str
) -> dict[str, Any]:
    """读取判定所需的状态（两个版本共用；只读）。"""
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
    return {
        "hypotheses": list(hypotheses),
        "evidence_rows": list(evidence_rows),
        "report": report,
        "side_effects": len(side_effect_rows),
    }


def _kb_hit_count(e: Evidence) -> int:
    ref = e.data_ref or {}
    raw = ref.get("hit_count") if isinstance(ref, dict) else None
    if not isinstance(raw, (int, str, float)):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _common_metrics(
    state: dict[str, Any], *, usage: dict[str, Any], latency_ms: int
) -> dict[str, Any]:
    evidence_rows: list[Evidence] = state["evidence_rows"]
    knowledge_hits = sum(
        1 for e in evidence_rows if e.source_type == "knowledge" and _kb_hit_count(e) > 0
    )
    rag_latency_ms: list[int] = []
    for e in evidence_rows:
        if e.source_type == "knowledge" and e.status == "ok":
            raw = (e.data_ref or {}).get("latency_ms")
            rag_latency_ms.append(int(raw) if isinstance(raw, (int, float)) else 0)
    tokens_in = int(usage.get("tokens_in") or 0)
    tokens_out = int(usage.get("tokens_out") or 0)
    if tokens_in or tokens_out:
        tokens: Any = {"in": tokens_in, "out": tokens_out, "total": tokens_in + tokens_out}
    else:
        tokens = "unavailable"  # Fake 模型不产出 usage；真实模型由 provider 元数据填充
    return {
        "hypothesis_count": len(state["hypotheses"]),
        "evidence_total": len(evidence_rows),
        "knowledge_hits": knowledge_hits,
        "tool_calls": int(usage.get("tool_calls_used", 0)),
        "latency_ms": latency_ms,
        "tokens": tokens,
        "estimated_cost": "unavailable",
        "rag_retrieval_latency_ms": max(rag_latency_ms) if rag_latency_ms else 0,
        "security_denied": 0,
        "repeated_side_effects": int(state["side_effects"]),
        "verification_misreport": 0,
    }


# ======================================================================
# judge-v1（D6 首版；旧成绩口径，行为冻结，不得修改）
# ======================================================================


async def judge_diagnosis_v1(
    session: AsyncSession,
    *,
    run_id: str,
    incident_id: str,
    outcome_unknown: bool,
    usage: dict[str, Any],
    latency_ms: int,
    expected: dict[str, Any],
) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    """diagnosis 规则判定（v1，冻结）；返回 (passed, metrics, detail)。"""
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

    def _kb_hit_count_v1(e: Evidence) -> int:
        ref = e.data_ref or {}
        raw = ref.get("hit_count") if isinstance(ref, dict) else None
        if not isinstance(raw, (int, str, float)):
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    knowledge_hits = sum(
        1 for e in evidence_rows if e.source_type == "knowledge" and _kb_hit_count_v1(e) > 0
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
        "judge_version": JUDGE_VERSION_V1,
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
        "tokens": "unavailable",  # v1 口径冻结：不读取 provider token 元数据
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


# ======================================================================
# judge-v2（D6 收尾；证据锚定 + 误导采信语义 + 失败分类）
# ======================================================================


def _is_gateway_error_code(code: str | None) -> bool:
    if not code:
        return False
    upper = code.upper()
    return upper.startswith("HTTP_") and upper[5:].isdigit() and int(upper[5:]) >= 500


def decide_diagnosis_v2(
    *,
    hypotheses: list[Any],
    evidence_rows: list[Any],
    report: Any | None,
    side_effects: int,
    outcome_unknown: bool,
    usage: dict[str, Any],
    latency_ms: int,
    expected: dict[str, Any],
    report_status: str | None = None,
    report_failure: str | None = None,
    outcome_error: str | None = None,
) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    """judge-v2 判定核心（纯函数：不读库，便于用构造数据覆盖规则边界）。"""
    evidence_by_id = {e.id: e for e in evidence_rows}
    evidence_ids = set(evidence_by_id)

    top1_markers = [str(m) for m in (expected.get("top1_markers") or [])]
    marker_set = set(top1_markers)

    def _valid_supports(h: Any) -> list[str]:
        return [str(r) for r in (h.supporting_evidence_ids or []) if str(r) in evidence_ids]

    def _support_codes(h: Any) -> set[str]:
        codes: set[str] = set()
        for ref in _valid_supports(h):
            code = evidence_by_id[ref].error_code
            if code:
                codes.add(str(code))
        return codes

    def _matches(h: Any) -> bool:
        """根因命中：证据锚定优先；纯文本匹配必须附有本 run 真实证据引用。"""
        if not top1_markers:
            return False
        if _support_codes(h) & marker_set:
            return True
        return bool(_valid_supports(h)) and _contains_any(str(h.statement), top1_markers)

    def _status(h: Any) -> str:
        return str(h.status or "")

    live = [h for h in hypotheses if _status(h) != "refuted"]
    top1_hit = bool(top1_markers) and bool(live) and _matches(live[0])
    top3_hit = any(_matches(h) for h in live[:3]) if top1_markers else False
    matched = [h for h in live if _matches(h)]

    unknown_ok = bool(expected.get("unknown_exit")) == bool(outcome_unknown)

    summaries = "\n".join(str(e.summary_redacted or "") for e in evidence_rows)
    require_contains = list(expected.get("require_evidence_contains") or [])
    contains_ok = all(marker in summaries for marker in require_contains)

    knowledge_hits = sum(
        1 for e in evidence_rows if e.source_type == "knowledge" and _kb_hit_count(e) > 0
    )
    require_hit = bool(expected.get("require_knowledge_hit"))
    forbid_hit = bool(expected.get("forbid_knowledge_hit"))
    knowledge_ok = (knowledge_hits > 0 if require_hit else True) and (
        knowledge_hits == 0 if forbid_hit else True
    )

    # ---- 误导性历史知识：只有「被采信」才算误用 ----
    misleading_markers = list(expected.get("misleading_markers") or [])
    correct_index = next((i for i, h in enumerate(live) if _matches(h)), None)
    misled_by: list[str] = []
    for index, h in enumerate(live):
        if not misleading_markers or not _contains_any(str(h.statement), misleading_markers):
            continue
        adopted = _status(h) == "supported" or correct_index is None or index < correct_index
        if adopted:
            misled_by.append(str(h.statement)[:120])

    # ---- 证据引用有效性 / 支持性（分列统计） ----
    total_refs = 0
    valid_refs = 0
    for h in hypotheses:
        for ref in (h.supporting_evidence_ids or []) + (h.contradicting_evidence_ids or []):
            total_refs += 1
            if ref in evidence_ids:
                valid_refs += 1
    report_refs = set(report.evidence_refs or []) if report is not None else set()
    wrong_refs = len(report_refs - evidence_ids)
    reference_validity = (valid_refs / total_refs) if total_refs else 1.0

    supported = [h for h in hypotheses if _status(h) == "supported"]
    # 支持性 = supported 假设中「至少引用 1 条本 run 真实证据」的比例
    supported_backed = [h for h in supported if _valid_supports(h)]
    evidence_support_rate = (len(supported_backed) / len(supported)) if supported else 1.0

    # ---- 直接原因 vs 未证实的深层根因 ----
    unverified_deep: list[str] = []
    for h in supported:
        supports = _valid_supports(h)
        # 「仅凭网关 5xx 证据」的严格口径：每一条支撑证据都必须是网关错误码；
        # 有容器 inspect / 日志 / 数据库诊断等非网关证据时不算（那是另一层证据）
        if not supports:
            continue
        if not all(_is_gateway_error_code(evidence_by_id[ref].error_code) for ref in supports):
            continue
        if _contains_any(str(h.statement), list(_DEEP_CAUSE_MARKERS)):
            unverified_deep.append(str(h.statement)[:120])

    min_evidence = int(expected.get("min_evidence") or 0)
    evidence_ok = len(evidence_rows) >= min_evidence

    report_ok = (report_status or "ok") != "failed"

    passed = all(
        [
            report_ok,
            top1_hit or (unknown_ok and not top1_markers),
            unknown_ok,
            contains_ok,
            knowledge_ok,
            not misled_by,
            evidence_ok,
            side_effects == 0,
        ]
    )

    failure_class = _classify_failure(
        passed=passed,
        report_ok=report_ok,
        misled=bool(misled_by),
        unknown_ok=unknown_ok,
        expected=expected,
        top1_hit=top1_hit,
        evidence_rows=evidence_rows,
        contains_ok=contains_ok,
        report_failure=report_failure,
        outcome_error=outcome_error,
    )

    metrics = {
        **_common_metrics(
            {
                "hypotheses": hypotheses,
                "evidence_rows": evidence_rows,
                "side_effects": side_effects,
            },
            usage=usage,
            latency_ms=latency_ms,
        ),
        "judge_version": JUDGE_VERSION_V2,
        "top1_hit": top1_hit,
        "top3_hit": top3_hit,
        "top1_evidence_backed": bool(matched) and bool(_valid_supports(matched[0])),
        "unknown_exit_correct": unknown_ok,
        "knowledge_misuse": len(misled_by),
        "wrong_reference_count": wrong_refs,
        "reference_validity": round(reference_validity, 4),
        "evidence_support_rate": round(evidence_support_rate, 4),
        "unverified_deep_cause": len(unverified_deep),
        "report_status": report_status or "unknown",
        "report_failure_kind": report_failure,
        "failure_class": failure_class,
    }
    detail = {
        "hypotheses": [
            {
                "rank": h.rank,
                "status": h.status,
                "statement": h.statement[:200],
                "supporting_evidence_ids": (h.supporting_evidence_ids or [])[:5],
            }
            for h in hypotheses[:5]
        ],
        "misled_by": misled_by,
        "unverified_deep_cause": unverified_deep,
        "evidence_summaries": [str(e.summary_redacted)[:160] for e in evidence_rows][:12],
        "report_refs_valid": report_refs <= evidence_ids if report is not None else None,
        "judge_version": JUDGE_VERSION_V2,
    }
    return passed, metrics, detail


async def judge_diagnosis_v2(
    session: AsyncSession,
    *,
    run_id: str,
    incident_id: str,
    outcome_unknown: bool,
    usage: dict[str, Any],
    latency_ms: int,
    expected: dict[str, Any],
    report_status: str | None = None,
    report_failure: str | None = None,
    outcome_error: str | None = None,
) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    """diagnosis 判定（v2：证据锚定）；读库后交给纯函数裁决。"""
    state = await _load_case_state(session, run_id=run_id, incident_id=incident_id)
    return decide_diagnosis_v2(
        hypotheses=state["hypotheses"],
        evidence_rows=state["evidence_rows"],
        report=state["report"],
        side_effects=int(state["side_effects"]),
        outcome_unknown=outcome_unknown,
        usage=usage,
        latency_ms=latency_ms,
        expected=expected,
        report_status=report_status,
        report_failure=report_failure,
        outcome_error=outcome_error,
    )


def _classify_failure(
    *,
    passed: bool,
    report_ok: bool,
    misled: bool,
    unknown_ok: bool,
    expected: dict[str, Any],
    top1_hit: bool,
    evidence_rows: list[Evidence],
    contains_ok: bool,
    report_failure: str | None,
    outcome_error: str | None = None,
) -> str:
    """失败分类（与验收报告口径一致；通过时为 none）。"""
    if passed:
        return "none"
    if outcome_error:
        # run 本身失败（provider/连接器/框架异常）——不是模型推理错误
        return f"environment_error:{outcome_error[:120]}"
    if not report_ok:
        return f"structured_output_error:{report_failure or 'failed'}"
    if misled:
        return "reasoning_error:adopted_misleading_history"
    if not evidence_rows:
        return "evidence_not_collected"
    if not contains_ok:
        return "missing_key_evidence"
    if not unknown_ok:
        return "failed_to_abstain" if expected.get("unknown_exit") else "premature_abstention"
    if not top1_hit and expected.get("top1_markers"):
        collection_only = all(str(e.status or "") != "ok" for e in evidence_rows)
        return "insufficient_evidence" if collection_only else "reasoning_error:unproven_root_cause"
    return "unknown_failure"


async def judge_diagnosis(
    session: AsyncSession,
    *,
    run_id: str,
    incident_id: str,
    outcome_unknown: bool,
    usage: dict[str, Any],
    latency_ms: int,
    expected: dict[str, Any],
    judge_version: str = JUDGE_VERSION,
    report_status: str | None = None,
    report_failure: str | None = None,
    outcome_error: str | None = None,
) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    """判定入口：默认 judge-v2；`judge_version=judge-v1` 复现旧分数（不改写历史）。"""
    if judge_version == JUDGE_VERSION_V1:
        return await judge_diagnosis_v1(
            session,
            run_id=run_id,
            incident_id=incident_id,
            outcome_unknown=outcome_unknown,
            usage=usage,
            latency_ms=latency_ms,
            expected=expected,
        )
    passed, metrics, detail = await judge_diagnosis_v2(
        session,
        run_id=run_id,
        incident_id=incident_id,
        outcome_unknown=outcome_unknown,
        usage=usage,
        latency_ms=latency_ms,
        expected=expected,
        report_status=report_status,
        report_failure=report_failure,
        outcome_error=outcome_error,
    )
    # 并排口径：同一轨迹的 v1 判定随结果一同返回（用于说明评估规则变化）
    v1_passed, v1_metrics, _ = await judge_diagnosis_v1(
        session,
        run_id=run_id,
        incident_id=incident_id,
        outcome_unknown=outcome_unknown,
        usage=usage,
        latency_ms=latency_ms,
        expected=expected,
    )
    detail["legacy_judge_v1"] = {"passed": v1_passed, "metrics": v1_metrics}
    return passed, metrics, detail

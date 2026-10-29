"""judge-v2 判定规则的单元测试（纯函数 decide_diagnosis_v2）。

覆盖用户在 D6 收尾提出的六条修正：
1. 仅提及误导性历史根因不判采信；2. refuted 的历史假设不算采信；
3. Top-1 要求有证据支撑（不允许纯关键词）；4. 证据锚定（error_code 命中）；
5. 引用有效性与支持性分列；6. 直接原因 vs 未证实深层根因。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.domains.evaluations.judge import JUDGE_VERSION_V1, JUDGE_VERSION_V2, decide_diagnosis_v2

USAGE: dict[str, Any] = {"tool_calls_used": 4, "tokens_in": 1200, "tokens_out": 300}


def _ev(
    evidence_id: str,
    *,
    error_code: str | None,
    status: str = "error",
    source_type: str = "http",
    summary: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=evidence_id,
        source_type=source_type,
        status=status,
        error_code=error_code,
        data_ref={"hit_count": 1} if source_type == "knowledge" else {},
        summary_redacted=summary or f"{source_type} {error_code}",
    )


def _hyp(
    rank: int,
    statement: str,
    *,
    status: str,
    supports: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        rank=rank,
        statement=statement,
        status=status,
        supporting_evidence_ids=supports or [],
        contradicting_evidence_ids=[],
        missing_information=[],
        confidence="medium",
    )


def _decide(
    hypotheses: list[Any], evidence: list[Any], expected: dict[str, Any], **kwargs: Any
) -> tuple[bool, dict[str, Any]]:
    passed, metrics, _detail = decide_diagnosis_v2(
        hypotheses=hypotheses,
        evidence_rows=evidence,
        report=None,
        side_effects=0,
        outcome_unknown=kwargs.pop("outcome_unknown", False),
        usage=USAGE,
        latency_ms=10,
        expected=expected,
        **kwargs,
    )
    return passed, metrics


def test_evidence_anchored_root_cause_without_literal_code_passes() -> None:
    """根因措辞是中文、未出现 error_code 字面量，但引用了对应证据 → 命中。"""
    evidence = [_ev("ev1", error_code="REDIS_UNAVAILABLE", source_type="redis")]
    hypotheses = [_hyp(1, "缓存实例连接被拒绝，服务不可达", status="supported", supports=["ev1"])]
    passed, metrics = _decide(
        hypotheses, evidence, {"top1_markers": ["REDIS_UNAVAILABLE"], "unknown_exit": False}
    )
    assert metrics["top1_hit"] is True
    assert metrics["top1_evidence_backed"] is True
    assert passed is True


def test_text_marker_without_any_evidence_is_not_counted() -> None:
    """只在文本里喊出标记词、没有任何真实证据引用 → 不算命中（不放宽）。"""
    evidence = [_ev("ev1", error_code="HTTP_500")]
    hypotheses = [_hyp(1, "根因是 REDIS_UNAVAILABLE", status="supported", supports=[])]
    passed, metrics = _decide(
        hypotheses, evidence, {"top1_markers": ["REDIS_UNAVAILABLE"], "unknown_exit": False}
    )
    assert metrics["top1_hit"] is False
    assert passed is False


def test_refuted_misleading_hypothesis_is_not_adoption() -> None:
    """历史误导被明确 refuted（并列在报告里说明已排除）→ 不算采信。"""
    evidence = [_ev("ev1", error_code="HTTP_500")]
    hypotheses = [
        _hyp(1, "orders 自身返回 500（直接原因）", status="supported", supports=["ev1"]),
        _hyp(2, "历史上 payments 上游故障（本次已被证据否定）", status="refuted"),
    ]
    passed, metrics = _decide(
        hypotheses,
        evidence,
        {
            "top1_markers": ["HTTP_500"],
            "unknown_exit": False,
            "misleading_markers": ["payments"],
        },
    )
    assert metrics["knowledge_misuse"] == 0
    assert passed is True


def test_lower_ranked_insufficient_mention_is_not_adoption() -> None:
    """正确根因排第一、误导线索引在第二位且标 insufficient_data → 不算采信。"""
    evidence = [_ev("ev1", error_code="HTTP_500")]
    hypotheses = [
        _hyp(1, "orders 健康端点直接返回 500", status="supported", supports=["ev1"]),
        _hyp(2, "也可能是 payments 上游不可用（未证实）", status="insufficient_data"),
    ]
    passed, metrics = _decide(
        hypotheses,
        evidence,
        {
            "top1_markers": ["HTTP_500"],
            "unknown_exit": False,
            "misleading_markers": ["payments"],
        },
    )
    assert metrics["knowledge_misuse"] == 0
    assert passed is True


def test_supported_misleading_leading_hypothesis_is_adoption() -> None:
    """把误导性历史根因当成 supported 主结论 → 判为采信（且整体失败）。"""
    evidence = [_ev("ev1", error_code="HTTP_500")]
    hypotheses = [
        _hyp(1, "payments 上游依赖故障导致 500", status="supported", supports=["ev1"]),
        _hyp(2, "orders 自身返回 500", status="insufficient_data", supports=["ev1"]),
    ]
    passed, metrics = _decide(
        hypotheses,
        evidence,
        {
            "top1_markers": ["HTTP_500"],
            "unknown_exit": False,
            "misleading_markers": ["payments"],
        },
    )
    assert metrics["knowledge_misuse"] == 1
    assert metrics["failure_class"] == "reasoning_error:adopted_misleading_history"
    assert passed is False


def test_refuted_rank1_does_not_hide_correct_rank2() -> None:
    """rank1 被自己否定，rank2 是正确的证据支撑根因 → top1 判给 rank2。"""
    evidence = [_ev("ev1", error_code="MYSQL_CONNECT_ERROR", source_type="mysql")]
    hypotheses = [
        _hyp(1, "可能是网络抖动", status="refuted"),
        _hyp(2, "MySQL 端口拒绝连接", status="supported", supports=["ev1"]),
    ]
    passed, metrics = _decide(
        hypotheses, evidence, {"top1_markers": ["MYSQL_CONNECT_ERROR"], "unknown_exit": False}
    )
    assert metrics["top1_hit"] is True
    assert passed is True


def test_report_generation_failure_is_structured_output_error() -> None:
    """报告生成失败必须单独归类，不能与「诊断 unknown」混同。"""
    evidence = [_ev("ev1", error_code="REDIS_UNAVAILABLE", source_type="redis")]
    passed, metrics = _decide(
        [],
        evidence,
        {"top1_markers": ["REDIS_UNAVAILABLE"], "unknown_exit": False},
        report_status="failed",
        report_failure="truncated",
    )
    assert passed is False
    assert metrics["failure_class"] == "structured_output_error:truncated"


def test_reference_validity_and_support_rate_are_separate() -> None:
    """引用有效性（id 是否存在）与支持性（supported 是否真引用了证据）分列。"""
    evidence = [_ev("ev1", error_code="HTTP_500")]
    hypotheses = [
        _hyp(1, "健康端点 500", status="supported", supports=["ev1", "ev_missing"]),
        _hyp(2, "另一个没有证据的 supported 断言", status="supported", supports=["ev_missing"]),
    ]
    _passed, metrics = _decide(
        hypotheses, evidence, {"top1_markers": ["HTTP_500"], "unknown_exit": False}
    )
    assert metrics["reference_validity"] == 0.3333  # 1/3 引用有效
    assert metrics["evidence_support_rate"] == 0.5  # 2 个 supported 中 1 个有真实证据


def test_unverified_deep_cause_is_counted() -> None:
    """仅凭网关 5xx 证据却断言具体深层根因 → 计入 unverified_deep_cause。"""
    evidence = [_ev("ev1", error_code="HTTP_504", summary="http.health_check 504")]
    hypotheses = [
        _hyp(1, "上游 payments 连接池耗尽导致网关超时", status="supported", supports=["ev1"])
    ]
    _passed, metrics = _decide(
        hypotheses, evidence, {"top1_markers": ["HTTP_504"], "unknown_exit": False}
    )
    assert metrics["unverified_deep_cause"] == 1


def test_deep_cause_with_non_gateway_evidence_is_not_flagged() -> None:
    """深层断言若还有容器 inspect 等非网关证据支撑，则不算「仅凭网关 5xx」。"""
    evidence = [
        _ev("ev1", error_code="HTTP_500"),
        _ev("ev2", error_code=None, status="ok", source_type="infra", summary="oom_killed=True"),
    ]
    hypotheses = [
        _hyp(1, "容器被 OOM killer 终止（exit 137）", status="supported", supports=["ev1", "ev2"])
    ]
    _passed, metrics = _decide(
        hypotheses, evidence, {"top1_markers": ["HTTP_500"], "unknown_exit": False}
    )
    assert metrics["unverified_deep_cause"] == 0


def test_unknown_case_requires_abstention() -> None:
    """证据不足场景：正确弃答（is_unknown=true）且无 supported 根因 → 通过。"""
    passed, metrics = _decide(
        [], [], {"top1_markers": [], "unknown_exit": True}, outcome_unknown=True
    )
    assert passed is True
    assert metrics["unknown_exit_correct"] is True
    assert metrics["failure_class"] == "none"


def test_tokens_from_provider_usage_metadata() -> None:
    """真实 provider 的 usage 元数据进入指标（Fake 时才标 unavailable）。"""
    _passed, metrics = _decide(
        [], [], {"top1_markers": [], "unknown_exit": True}, outcome_unknown=True
    )
    assert metrics["tokens"] == {"in": 1200, "out": 300, "total": 1500}
    assert metrics["judge_version"] == JUDGE_VERSION_V2
    assert JUDGE_VERSION_V1 == "judge-v1"
